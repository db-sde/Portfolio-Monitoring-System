"""Progressive enrichment with replayable, atomic NAV + analytics writes."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select
import enrichment
import nav_service
import provider
from models import EnrichmentCache, Scheme

logger = logging.getLogger("portfolioiq")
CACHE_TTL_HOURS = 24
PROVIDER_KEY = "mfapi.in+captnemo"
ENRICH_BATCH_SIZE = 25
COMMIT_CHUNK_SIZE = 5


def _is_fresh(value):
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - value < timedelta(hours=CACHE_TTL_HOURS)


def get_cached_enrichments(session, scheme_ids):
    rows = session.scalars(
        select(EnrichmentCache).where(
            EnrichmentCache.scheme_id.in_(scheme_ids),
            EnrichmentCache.provider == PROVIDER_KEY,
        )
    )
    return {
        r.scheme_id: {
            **(r.payload or {}),
            "stale": not _is_fresh(r.fetched_at)
            or (r.payload or {}).get("nav_status") == "stale",
            "status": r.status,
        }
        for r in rows
    }


def get_cached_enrichment(session, scheme_id):
    return get_cached_enrichments(session, [scheme_id]).get(scheme_id)


def _persist_chunk(session, records, on_before_commit=None):
    """A retry replays NAV points as well as the payload after rollback."""

    def write(record):
        sid, payload, points, summary = record
        nav_service.store_nav_points(session, sid, points, stored_summary=summary)
        row = session.scalar(
            select(EnrichmentCache).where(
                EnrichmentCache.scheme_id == sid,
                EnrichmentCache.provider == PROVIDER_KEY,
            )
        )
        if row is None:
            row = EnrichmentCache(scheme_id=sid, provider=PROVIDER_KEY)
            session.add(row)
        if payload.get("nav_status") not in {"ok", "stale"} and row.payload:
            # A provider outage never destroys last-known valid analytics.
            row.payload = {
                **row.payload,
                "refresh_error": payload.get("reason", "Provider unavailable"),
            }
            row.status = "partial"
        else:
            row.payload = payload
            row.status = (
                "ok"
                if all(
                    payload.get(k) == "ok"
                    for k in ("nav_status", "metadata_status", "analytics_status")
                )
                else "unavailable"
                if payload.get("nav_status") not in {"ok", "stale"}
                else "partial"
            )
            row.data_as_of = (
                date.fromisoformat(payload["nav_as_of"])
                if payload.get("nav_as_of")
                else None
            )
            row.fetched_at = datetime.now(timezone.utc)

    try:
        if on_before_commit:
            on_before_commit(session)
        for record in records:
            write(record)
        session.commit()
    except Exception:
        session.rollback()
        for record in records:
            try:
                if on_before_commit:
                    on_before_commit(session)
                write(record)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception(
                    "Could not persist enrichment for scheme %s", record[0]
                )
                raise


async def refresh_enrichment(session, schemes, on_stage=None, on_before_commit=None):
    cached = get_cached_enrichments(session, [s.scheme_id for s in schemes])
    targets = [
        s
        for s in schemes
        if not (
            cached.get(s.scheme_id, {}).get("status") == "ok"
            and not cached[s.scheme_id]["stale"]
        )
    ]
    summaries = nav_service.get_stored_nav_summary(
        session, [s.scheme_id for s in targets]
    )
    session.commit()
    completed = 0
    async with provider.Client() as client:
        benchmark = session.scalar(
            select(Scheme)
            .where(Scheme.amfi_code == enrichment.BENCHMARK_AMFI_CODE)
            .limit(1)
        )
        benchmark_history = (
            nav_service.get_nav_history(session, benchmark.scheme_id)
            if benchmark
            else []
        )
        session.commit()
        if not benchmark_history:
            benchmark_history = await enrichment._get_benchmark_nav_history(client, {})
        semaphore = asyncio.Semaphore(8)
        for start in range(0, len(targets), ENRICH_BATCH_SIZE):
            batch = targets[start : start + ENRICH_BATCH_SIZE]
            histories = nav_service.get_nav_histories(
                session, [s.scheme_id for s in batch]
            )
            session.commit()

            async def fetch(s):
                if not s.amfi_code or not s.identity_confirmed:
                    return s, {
                        "enrichment_source": "failed",
                        "nav_status": "unavailable",
                        "reason": "Fund identity needs review",
                    }
                try:
                    async with semaphore:
                        value = await asyncio.wait_for(
                            enrichment._enrich_one(
                                client,
                                s.amfi_code,
                                s.isin,
                                s.name,
                                benchmark_history,
                                histories.get(s.scheme_id),
                                cached.get(s.scheme_id, {}).get("category"),
                            ),
                            90,
                        )
                    return s, value
                except Exception as exc:
                    return s, {
                        "enrichment_source": "failed",
                        "nav_status": "unavailable",
                        "reason": type(exc).__name__,
                    }

            tasks = [asyncio.create_task(fetch(s)) for s in batch]
            pending = []
            try:
                for task in asyncio.as_completed(tasks):
                    scheme, payload = await task
                    points = [
                        (enrichment._parse_nav_date(r["date"]), Decimal(str(r["nav"])))
                        for r in payload.get("_nav_history", [])
                        if enrichment._parse_nav_date(r["date"])
                    ]
                    clean = {k: v for k, v in payload.items() if k != "_nav_history"}
                    pending.append(
                        (
                            scheme.scheme_id,
                            clean,
                            points,
                            summaries.get(scheme.scheme_id),
                        )
                    )
                    completed += 1
                    if len(pending) >= COMMIT_CHUNK_SIZE or completed == len(targets):
                        _persist_chunk(session, pending, on_before_commit)
                        pending = []
                        if on_stage:
                            on_stage(f"enriching:{completed}/{len(targets)}")
                if pending:
                    _persist_chunk(session, pending, on_before_commit)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    return get_cached_enrichments(session, [s.scheme_id for s in schemes])
