"""Run with python worker.py; jobs survive web-server restarts."""

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
from sqlalchemy import select, text
from casparser.types import CASData
import auth, db, jobs, provider, ingestion, enrichment_bridge, benchmark_service
from models import (
    IngestJob,
    CasUpload,
    Scheme,
    Holding,
    Folio,
    Transaction,
    PurchaseLot,
    DisposalAllocation,
)
from scheme_resolution import prepare_schemes

log = logging.getLogger("portfolioiq.worker")


def stage(job_id, name, **fields):
    with db.get_session() as session:
        jobs.set_stage(session, job_id, name, **fields)


def parse(payload):
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("parse_worker.py"))],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=130,
    )
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        raise RuntimeError(
            "The parser process stopped. Try a smaller statement or check the server logs."
        )
    if result.get("error"):
        raise RuntimeError(result["error"])
    return CASData.model_validate(result["data"])


def replace_statement(parsed, content, prepared, job_id=None, attempt=None):
    """Short atomic replacement: prior data remains readable until commit."""
    with db.get_session() as session:
        if job_id is not None:
            job = jobs.assert_owner(session, job_id, attempt)
        for model in (
            DisposalAllocation,
            PurchaseLot,
            Transaction,
            Holding,
            Folio,
            CasUpload,
        ):
            session.query(model).delete(synchronize_session=False)
        session.flush()

        async def write():
            async with provider.Client() as client:
                return await ingestion.ingest_cas(
                    session, client, parsed, content, prepared=prepared
                )

        result = asyncio.run(write())
        if job_id is not None:
            job.ready = True
            job.dataset_id = result.upload_id
            job.payload = None
            job.result_json = {
                "investor_name": parsed.investor_info.name,
                "warnings": result.warnings,
            }
        return result


def execute(job_id, attempt):
    def update(name, **fields):
        return stage(job_id, name, attempt=attempt, **fields)

    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(5):
            try:
                with db.get_session() as session:
                    job = session.get(IngestJob, job_id)
                    if job and job.attempts == attempt:
                        job.heartbeat_at = datetime.now(timezone.utc).replace(
                            tzinfo=None
                        )
            except Exception:
                log.exception("Heartbeat failed for %s", job_id)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with db.get_session() as session:
            job = session.get(IngestJob, job_id)
            kind, ready, payload = job.kind, job.ready, job.payload
            dataset_id = job.dataset_id
        if kind == "upload" and not ready:
            value = auth.unseal(payload)
            if "parsed" in value:
                parsed = CASData.model_validate(value["parsed"])
            else:
                update("parsing")
                parsed = parse(value)
                value = {
                    "parsed": parsed.model_dump(mode="json", by_alias=True),
                    "content": value["content"],
                }
                update("resolving funds", payload=auth.seal(value))
            with db.get_session() as session:
                known = {
                    s.isin: s.identity_confirmed
                    for s in session.scalars(select(Scheme))
                    if s.isin
                }

            async def prepare():
                async with provider.Client() as client:
                    return await prepare_schemes(client, parsed, known)

            prepared = asyncio.run(prepare())
            update("saving statement")
            result = replace_statement(
                parsed, base64.b64decode(value["content"]), prepared, job_id, attempt
            )
            dataset_id = result.upload_id
            update(
                "portfolio ready",
                ready=True,
                dataset_id=dataset_id,
                payload=None,
                result_json={
                    "investor_name": parsed.investor_info.name,
                    "statement_period": {
                        "from": str(parsed.statement_period.from_),
                        "to": str(parsed.statement_period.to),
                    },
                    "total_holdings": len(result.holdings),
                    "warnings": result.warnings,
                    "holdings_needing_review": sum(
                        h.status != "reconciled" for h in result.holdings
                    ),
                },
            )
        update("refreshing market data")
        with db.get_session() as session:
            current = session.scalar(select(CasUpload.upload_id).limit(1))
            if current != dataset_id:
                raise RuntimeError(
                    "The statement has changed. This refresh has been superseded."
                )

        async def refresh():
            async with provider.Client() as client:
                with db.get_session() as session:
                    await benchmark_service.refresh_nifty50_proxy_nav(session, client)
            with db.get_session() as session:
                schemes = list(
                    session.scalars(
                        select(Scheme).where(
                            Scheme.scheme_id.in_(select(Holding.scheme_id))
                        )
                    )
                )
                await enrichment_bridge.refresh_enrichment(
                    session,
                    schemes,
                    on_stage=update,
                    on_before_commit=lambda tx: jobs.assert_owner(tx, job_id, attempt),
                )

        asyncio.run(refresh())
        update(
            "finished",
            status="ok",
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            payload=None,
        )
    except Exception as exc:
        log.exception("Job %s failed", job_id)
        with db.get_session() as session:
            job = session.get(IngestJob, job_id)
            if job and job.attempts == attempt:
                job.status = "cancelled" if job.cancel_requested else "error"
                job.error_detail = str(exc)[:500]
                job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                job.payload = None
    finally:
        stopped.set()
        thread.join(timeout=1)


def schedule_refresh():
    from models import EnrichmentCache
    from datetime import timedelta

    with db.get_session() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": jobs.CONTROL_LOCK}
        )
        if jobs.active_job(session) or not session.scalar(
            select(CasUpload.upload_id).limit(1)
        ):
            return
        latest = session.scalar(
            select(IngestJob).order_by(IngestJob.job_id.desc()).limit(1)
        )
        if latest and datetime.now(timezone.utc).replace(tzinfo=None) - (
            latest.completed_at or latest.created_at
        ) < timedelta(hours=6):
            return
        schemes = list(session.scalars(select(Holding.scheme_id).distinct()))
        cache = enrichment_bridge.get_cached_enrichments(session, schemes)
        if any(
            not cache.get(sid) or cache[sid]["stale"] or cache[sid]["status"] != "ok"
            for sid in schemes
        ):
            jobs.enqueue(session, "refresh")


def run_once():
    # A dedicated lock transaction also works with transaction-pooled Postgres.
    # Attempt checks fence writes from a worker whose lock connection was lost.
    with db.engine.connect() as lock:
        acquired = lock.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": jobs.WORKER_LOCK}
        )
        if not acquired:
            return False
        try:
            with db.get_session() as session:
                job = jobs.active_job(session)
                if job is None:
                    return False
                if job.cancel_requested or job.attempts >= 3:
                    job.status = "cancelled" if job.cancel_requested else "error"
                    job.error_detail = "Job cancelled or worker retry limit reached. Please upload again."
                    job.payload = None
                    return True
                job.status = "processing"
                job.attempts += 1
                job.heartbeat_at = datetime.now(timezone.utc).replace(tzinfo=None)
                job_id = job.job_id
                attempt = job.attempts
            execute(job_id, attempt)
            return True
        finally:
            lock.rollback()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    auth.secret()
    db.init_db()
    while True:
        try:
            if not run_once():
                schedule_refresh()
                time.sleep(2)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Worker loop failed")
            time.sleep(5)
