"""
PortfolioIQ backend — FastAPI app on Postgres (Neon), spec section 16.

Every endpoint reads/writes through the DB-backed services built this
session (ingestion, portfolio_service, gains_service_db, snapshot_service,
benchmark_service, exposure_service, config_service) rather than the old
JSON-file storage — see README/git history for the migration rationale
(Render's ephemeral disk wiping cas_data.json/config.json/gains_data.json/
enrichment_cache.json on every redeploy).

Core rule enforced throughout (spec section 4, 10, 22): CAS is the
source of truth for ownership/transactions; MFAPI-resolved NAV is the
source of truth for ANY valuation. scheme.valuation.value/nav from the
CAS is never read for current or historical value anywhere in this file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from casparser import read_cas_pdf
from casparser.enums import TransactionType
from casparser.exceptions import CASParseError, ParserException
from casparser.types import CASData, NSDLCASData

import benchmark_service
import config_service
import db
import enrichment_bridge
import exposure_service
import gains_service_db
import ingestion
import portfolio_service
import snapshot_service
from models import CasUpload, EnrichmentCache, Folio, Holding, IngestJob, Scheme, Transaction

CALCULATION_VERSION = "2.0.0"  # bumped on any change to a calculation rule (spec 22)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# A "processing" ingest_jobs row older than this is treated as abandoned
# (the process that owned it crashed or the container restarted mid-
# ingest) rather than genuinely still running, so it can't permanently
# block every future upload. Generous even for a large real statement's
# worth of sequential mfapi.in resolution calls — the largest measured
# this session was ~4 minutes.
STALE_JOB_THRESHOLD = timedelta(minutes=15)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("portfolioiq")

app = FastAPI(title="PortfolioIQ")

_default_origins = "http://localhost:5173"
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_methods=["*"], allow_headers=["*"])

# This app has real personal financial data behind it (holdings, PAN,
# transaction history) and, until this was added, zero authentication —
# every route including DELETE /api/all-data was reachable by anyone who
# found the (public) GitHub repo or just the live URL. That's not
# hypothetical: a real upload's data was wiped in production between two
# checks minutes apart, with nothing in this app's own code or UI capable
# of having caused it — the only thing that fits is an unauthenticated
# caller hitting the endpoint directly. A shared API key isn't a full
# auth system (there's no per-user login, and a key embedded in the
# built frontend bundle is visible to anyone who inspects that bundle's
# own network requests), but it closes the actual exposure this
# incident came from: a stranger or bot finding the endpoint from the
# public source and calling it directly, without ever loading the app.
API_KEY = os.environ.get("API_KEY")
_UNAUTHENTICATED_PATHS = {"/api/health"}


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in _UNAUTHENTICATED_PATHS:
        return await call_next(request)
    if not API_KEY:
        return JSONResponse(status_code=500, content={"detail": "Server misconfigured: API_KEY is not set."})
    supplied = request.headers.get("x-api-key", "")
    if not secrets.compare_digest(supplied, API_KEY):
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid API key."})
    return await call_next(request)


@app.on_event("startup")
def _on_startup() -> None:
    db.init_db()


def get_session() -> Session:
    with db.get_session() as session:
        yield session


def _fix_segregation_classification(parsed) -> None:
    """Runtime correction for a real upstream casparser bug: its
    transaction classifier only checks for "segregat" in the description
    on the *positive*-units leg of a segregation event. The matching
    negative-units leg falls through to REDEMPTION, generating a phantom
    taxable gain for units reclassified, not sold."""
    for folio in parsed.folios:
        for scheme in folio.schemes:
            for txn in scheme.transactions:
                if txn.type == TransactionType.REDEMPTION and "segregat" in (txn.description or "").lower():
                    txn.type = TransactionType.SEGREGATION


# ---------------------------------------------------------------- scope ----

def _scope_holding_ids(
    session: Session, level: Optional[str], group_name: Optional[str],
    investor_name: Optional[str], arn: Optional[str],
) -> Optional[list[int]]:
    """None means "no filter" (all holdings). Group/Investor/Advisor
    filters resolve through config_service's ARN attribution, same
    contract the frontend's LevelSelector already expects."""
    if level in (None, "", "all"):
        return None
    config = config_service.load_config(session)
    arns_in_scope: set[str] = set()
    if level == "arn" and arn:
        arns_in_scope = {arn}
    elif level == "investor" and investor_name:
        for group in config.get("groups", []):
            for investor in group.get("investors", []):
                if investor.get("investor_name") == investor_name:
                    arns_in_scope.update(investor.get("arns", []))
    elif level == "group" and group_name:
        for group in config.get("groups", []):
            if group.get("group_name") == group_name:
                for investor in group.get("investors", []):
                    arns_in_scope.update(investor.get("arns", []))
    if not arns_in_scope:
        return []
    holding_ids = list(session.execute(
        select(Holding.holding_id).where(Holding.advisor_arn.in_(arns_in_scope))
    ).scalars())
    return holding_ids


def _all_holding_ids(session: Session) -> list[int]:
    return list(session.execute(select(Holding.holding_id)).scalars())


def _holdings_coverage_through(session: Session) -> Optional[str]:
    latest = session.execute(select(CasUpload.period_to).order_by(CasUpload.period_to.desc()).limit(1)).scalar_one_or_none()
    return latest.isoformat() if latest else None


def _holdings_coverage_from(session: Session) -> Optional[str]:
    """Earliest period_from across every accumulated upload — multiple
    statements can now coexist (that's the point of the Neon migration),
    so this is a min() across all of them, not one CAS's own period."""
    earliest = session.execute(select(CasUpload.period_from).order_by(CasUpload.period_from.asc()).limit(1)).scalar_one_or_none()
    return earliest.isoformat() if earliest else None


def _response_meta(session: Session, warnings: Optional[list[str]] = None, data_quality: str = "OK") -> dict:
    return {
        "requested_valuation_date": date.today().isoformat(),
        "holdings_coverage_through": _holdings_coverage_through(session),
        "nav_policy": "ON_OR_BEFORE",
        "calculation_version": CALCULATION_VERSION,
        "warnings": warnings or [],
        "data_quality": data_quality,
    }


def _metrics_to_dict(m: portfolio_service.HoldingMetrics, config: dict) -> dict:
    group_name, investor_name = (
        config_service.find_owner_for_arn(config, m.advisor_arn) if m.advisor_arn else (None, None)
    )
    return {
        "holding_id": m.holding_id, "folio": m.folio, "amc": m.amc, "scheme_name": m.scheme_name,
        "isin": m.isin, "asset_class": m.asset_class, "advisor": m.advisor_arn,
        "advisor_label": config_service.find_arn_label(config, m.advisor_arn) if m.advisor_arn else None,
        "group_name": group_name, "investor_name": investor_name,
        "balance_units": m.balance_units, "weighted_purchase_nav": m.weighted_purchase_nav,
        "current_nav": m.current_nav, "current_nav_date": m.current_nav_date.isoformat() if m.current_nav_date else None,
        "net_invested_value": m.remaining_purchase_value, "current_value": m.current_value,
        "absolute_gain": m.gain, "absolute_gain_pct": m.absolute_return_pct,
        "weighted_days_held": m.weighted_days_held, "xirr": m.xirr_pct,
        "reconciliation_status": m.reconciliation_status,
        "flags": [{"code": f.code, "detail": f.detail} for f in m.flags],
    }


# --------------------------------------------------------------- health ----

@app.get("/api/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------------------- upload ----

def _mark_stage(job_id: Optional[int], stage: str) -> None:
    """Temporary diagnostic checkpoint — see IngestJob.debug_stage's own
    docstring. A fresh, tiny, isolated session/commit per call so this
    can never itself be the thing that hangs or loses work; safe to call
    from anywhere, including right before something that might fail."""
    if job_id is None:
        return
    try:
        with db.get_session() as session:
            job = session.get(IngestJob, job_id)
            if job:
                job.debug_stage = stage
    except Exception:
        logger.exception("_mark_stage failed for job_id=%s stage=%s", job_id, stage)


async def _run_enrichment_task_async(scheme_ids: list[int], job_id: Optional[int] = None) -> None:
    """Two separate sessions/transactions on purpose: benchmark_service
    already retries internally and never raises, but if it somehow did,
    sharing one transaction with the per-scheme enrichment below would
    roll back every scheme's freshly-fetched NAV/risk data along with
    it — caught live in testing (a raised ConnectTimeout in the
    benchmark call discarded an already-successful scheme NAV
    population in the same transaction). Isolating them means a failure
    in one never costs the other its work.

    This whole body is wrapped in try/except: it runs as a FastAPI
    BackgroundTask, after the HTTP response has already been sent, so
    there is no request/response cycle left to surface an exception
    through — an uncaught one here fails completely silently to the
    client and, depending on the ASGI server, may not even reach the
    log. A real production upload's enrichment came back with zero
    NAV/enrichment rows for every real scheme with no visible error;
    logging explicitly here is what would have made that diagnosable
    from Render's log viewer instead of requiring a live DB query to
    even notice it happened.

    _mark_stage calls throughout are a temporary diagnostic for a real,
    still-unexplained incident: enrichment wrote zero rows for 7+
    minutes on a 54-scheme portfolio, no exception logged, nothing —
    meaning it's stuck somewhere in here rather than failing outright.
    These checkpoints make that visible via GET /api/upload-status
    without server log access."""
    _mark_stage(job_id, "started")
    try:
        async with httpx.AsyncClient() as client:
            _mark_stage(job_id, "before_benchmark_nav")
            with db.get_session() as session:
                await benchmark_service.refresh_nifty50_proxy_nav(session, client)
            _mark_stage(job_id, "before_refresh_enrichment")
            with db.get_session() as session:
                schemes = [session.get(Scheme, sid) for sid in scheme_ids]
                schemes = [s for s in schemes if s is not None]
                await enrichment_bridge.refresh_enrichment(session, schemes)
            _mark_stage(job_id, "finished_ok")
    except Exception as exc:
        logger.exception("_run_enrichment_task failed for scheme_ids=%s", scheme_ids)
        _mark_stage(job_id, f"exception: {type(exc).__name__}: {exc}")


def _run_enrichment_task(scheme_ids: list[int], job_id: Optional[int] = None) -> None:
    """Deliberately a plain sync function, even though the real work
    (_run_enrichment_task_async) is async — this is the entire fix for a
    live production incident: enrichment_bridge.refresh_enrichment and
    nav_service.store_nav_points do their SQLAlchemy work directly
    (session.execute/add/flush), unwrapped, inside async functions. When
    this ran as a coroutine passed to BackgroundTasks, Starlette awaits
    async background tasks directly on the MAIN event loop — so every
    one of those blocking DB round-trips (measured live at 200-370+
    seconds total for a large real portfolio) blocked the ENTIRE app,
    including totally unrelated requests like GET /api/health, for the
    whole run. Confirmed live: the production site hung completely
    (health check itself timing out) for the duration of one of these
    runs, right after an upload.

    Starlette's BackgroundTask dispatch runs a *sync* callable via
    starlette.concurrency.run_in_threadpool automatically, off the main
    event loop entirely. asyncio.run() here gives that thread its own
    fresh event loop to run the real async work on — completely
    isolated from the loop serving live requests, so enrichment can take
    as long as it needs without freezing anything else."""
    asyncio.run(_run_enrichment_task_async(scheme_ids, job_id))


def _replace_and_ingest_sync(content: bytes, parsed) -> ingestion.IngestResult:
    """The wipe + full ingest, run entirely off the main event loop in its
    own thread with its own database session — the fix for two real,
    separately-caught bugs:

    1. ingest_cas's own DB writes (session.execute/add/flush across
       _persist_transactions, _rebuild_fifo, scheme_resolution) are plain
       synchronous SQLAlchemy calls. Running them directly inside
       upload_cas's async def body used to block the main event loop for
       the request's entire ingest duration — confirmed live: GET
       /api/health hung for 127 seconds during one real upload.
    2. Fixing that wasn't enough on its own — re-testing live afterward,
       /api/health stayed responsive throughout, yet the write still
       silently never persisted (proven by re-uploading the identical
       file and getting a fresh "ok" ingest again instead of
       "duplicate"). The cause was reusing the FastAPI-request-scoped
       session (created on the main thread by Depends(get_session)) from
       inside this worker thread — SQLAlchemy's own docs are explicit
       that a Session must not be shared across threads at all, even
       used sequentially, never concurrently.

    Both fixed the same way: open and fully own a BRAND NEW session
    entirely within this one worker thread (db.get_session() commits and
    closes it on the way out) rather than being handed one created
    elsewhere, and never let this function's own blocking work run on
    the main event loop.

    Now called from _run_ingest_job_sync as a background job rather than
    awaited directly by upload_cas — see that function's docstring for
    why: a real 50+-scheme statement takes minutes of sequential
    mfapi.in resolution calls, long enough that Render's own reverse
    proxy returned a 502 to the client before this could even finish,
    independent of whether it was correct. asyncio.run() gives this
    thread its own fresh event loop for ingest_cas's httpx calls,
    isolated from the one serving every other concurrent request. A
    fresh httpx.AsyncClient is created inside this new loop rather than
    reusing one from the caller's loop — an AsyncClient's connection
    pool is bound to the loop that created it and isn't valid on a
    different one."""
    with db.get_session() as session:
        from models import DisposalAllocation, EnrichmentCache, NavCache, PurchaseLot, SchemeAlias, SchemeBenchmarkMap
        # Real child-before-parent order, not a guess: the previous version
        # here still had Scheme deleted before NavCache/EnrichmentCache
        # (which both FK into it) despite already having fixed the
        # SchemeBenchmarkMap gap — caught live via a real 500 on this exact
        # endpoint, reproduced directly against a copy of the real data to
        # get the actual IntegrityError (psycopg reported
        # nav_cache_scheme_id_fkey specifically) rather than guessing again.
        # Traced the complete FK graph in models.py this time instead of
        # patching one violation at a time.
        for model in (
            DisposalAllocation, PurchaseLot, Transaction, SchemeAlias,
            NavCache, EnrichmentCache, SchemeBenchmarkMap, Holding, Scheme, Folio, CasUpload,
        ):
            session.query(model).delete()
        session.flush()

        async def _ingest() -> ingestion.IngestResult:
            async with httpx.AsyncClient() as client:
                return await ingestion.ingest_cas(session, client, parsed, content, investor_id=None)

        result = asyncio.run(_ingest())
        # Returning here, still inside the `with` block, is deliberate:
        # db.get_session()'s __exit__ is what commits — it must run
        # before this function returns, or the session closes on a
        # rollback (no exception occurred, but nothing was ever
        # explicitly committed either) instead of persisting the ingest.
        return result


def _run_ingest_job_sync(job_id: int, content: bytes, parsed) -> None:
    """The actual background work behind an upload, run after upload_cas
    has already returned "processing" to the client. Dispatched as a
    plain BackgroundTasks callable — Starlette runs a sync one via
    run_in_threadpool automatically, off the main event loop, same as
    _run_enrichment_task — so it's free to take however long a real
    statement's sequential mfapi.in resolution needs without anyone
    holding an HTTP connection open waiting on it.

    This is the actual fix for the 502s a real ~50-scheme upload was
    hitting: previously upload_cas awaited the equivalent of this
    function directly, meaning the browser (and Render's own reverse
    proxy in front of it) had to keep one HTTP request alive for the
    entire multi-minute ingest — long enough that the proxy gave up and
    returned a 502 before the ingest even finished, regardless of
    whether it was correct. Now the HTTP round-trip is just the fast
    part (parse + duplicate check); this runs after, unconstrained by
    any request timeout, and the frontend polls GET
    /api/upload-status/{job_id} until it's done."""
    # Everything after a successful ingest — including marking the job
    # "ok" — is inside this SAME try/except on purpose. A first version
    # split these into two separate try blocks, and the second one (job
    # status update + scheme_id lookup) had a bug that raised inside
    # `with db.get_session() as session:` — db.get_session()'s own
    # except-and-rollback swallowed that exception's effect on the DB
    # (the "ok" write never committed) while the bare exception still
    # propagated out of a BackgroundTasks callable with nowhere to
    # surface it, so the job sat at "processing" forever despite the
    # ingest itself having genuinely succeeded (confirmed live: a
    # re-upload of the same file correctly came back "duplicate",
    # proving the data really did commit — only the job row's own status
    # update was silently lost). One try/except around the whole
    # post-ingest sequence means ANY failure here — expected or not —
    # reliably lands as job.status="error" with the real exception
    # message, never a silent stuck-forever "processing".
    try:
        result = _replace_and_ingest_sync(content, parsed)
        result_json = {
            "investor_name": parsed.investor_info.name,
            "statement_period": {"from": str(parsed.statement_period.from_), "to": str(parsed.statement_period.to)},
            "total_holdings": len(result.holdings),
            "holdings_needing_review": sum(1 for h in result.holdings if h.status != "reconciled"),
            "warnings": result.warnings,
            "holding_notes": [{"scheme_name": h.scheme_name, "status": h.status, "detail": h.detail} for h in result.holdings],
        }
        with db.get_session() as session:
            scheme_ids = list({
                session.get(Holding, h.holding_id).scheme_id for h in result.holdings
            })
            job = session.get(IngestJob, job_id)
            if job:
                job.status = "ok"
                job.result_json = result_json
                job.completed_at = datetime.now(timezone.utc)
    except Exception as exc:
        logger.exception("Background ingest failed for job_id=%s", job_id)
        try:
            with db.get_session() as session:
                job = session.get(IngestJob, job_id)
                if job:
                    job.status = "error"
                    job.error_detail = f"{type(exc).__name__}: {exc}"
                    job.completed_at = datetime.now(timezone.utc)
        except Exception:
            logger.exception("Also failed to record the error status for job_id=%s", job_id)
        return

    # Direct call, not background_tasks.add_task — there's no live
    # request to hang this off anymore by the time this runs (this
    # function IS already the background work). _run_enrichment_task is
    # its own isolated thread/event-loop unit regardless of how it's
    # invoked, so calling it here, sequentially, after ingest finishes,
    # is exactly equivalent to how upload_cas used to schedule it.
    _run_enrichment_task(scheme_ids, job_id)


@app.post("/api/upload-cas")
async def upload_cas(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    password: str = Form(default=""),
    session: Session = Depends(get_session),
):
    filename = (file.filename or "").lower()
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That file is larger than we accept (20MB max).")
    if not (filename.endswith(".pdf") or filename.endswith(".json")):
        raise HTTPException(400, "Upload the CAS PDF from CAMS/KFintech (or a previously-parsed CAS JSON file).")

    # Reject a second upload while one's still running, checked here,
    # before any parsing — a real incident: a client-side timeout on one
    # upload didn't stop it running server-side, and a retry moments
    # later raced against it, both wiping the same tables concurrently,
    # one hitting a foreign-key violation mid-delete. Checking this
    # early means a concurrent attempt fails in milliseconds instead of
    # wasting a real PDF parse (measured up to ~40s for a large
    # statement) only to be rejected at the end anyway. This alone
    # doesn't fully close the race, though — two requests can both pass
    # this check before either has written its own job row — so it's
    # paired with a real database constraint below (IngestJob's partial
    # unique index) that's the actual guarantee.
    existing_job = session.execute(select(IngestJob).order_by(IngestJob.job_id.desc()).limit(1)).scalar_one_or_none()
    if existing_job and existing_job.status == "processing":
        age = datetime.utcnow() - existing_job.created_at
        if age < STALE_JOB_THRESHOLD:
            raise HTTPException(409, "An import is already in progress. Wait for it to finish (see the status bar) before starting another.")
        # Older than the threshold: whatever process owned this job is
        # gone (crashed, container restarted mid-ingest) — treat it as
        # abandoned rather than let a dead job block every future
        # upload forever. Deleted explicitly, here, rather than by the
        # generic "clear finished jobs" delete below — that one
        # deliberately excludes status="processing" rows (see its own
        # comment for why), so a stale-but-still-"processing" row like
        # this one needs its own removal or nothing would ever clear it.
        session.delete(existing_job)
        session.flush()

    if filename.endswith(".json"):
        # A previously-parsed CAS JSON — useful for testing, or if
        # someone already has one from elsewhere. Re-validated through
        # the same CASData pydantic model read_cas_pdf itself returns
        # (Decimal/date fields coerce correctly from JSON strings/numbers
        # via pydantic, same as any other CASData construction), so
        # everything downstream sees an identical shape either way.
        try:
            raw_dict = json.loads(content)
        except json.JSONDecodeError:
            raise HTTPException(400, "That doesn't look like valid JSON.")
        try:
            parsed = CASData.model_validate(raw_dict)
        except Exception as exc:
            raise HTTPException(422, f"That JSON doesn't match the expected CAS data shape: {exc}")
    else:
        with tempfile.TemporaryDirectory(prefix="portfolioiq-") as tmp:
            pdf_path = Path(tmp) / "statement.pdf"
            pdf_path.write_bytes(content)
            try:
                parsed = await run_in_threadpool(read_cas_pdf, str(pdf_path), password)
            except CASParseError as exc:
                message = str(exc)
                if "password" in message.lower():
                    raise HTTPException(401, "That password didn't work. Double check it and try again.")
                raise HTTPException(422, "We couldn't read this as a CAS statement. Make sure it's the unmodified PDF from CAMS or KFintech.")
            except ParserException:
                raise HTTPException(422, "This statement couldn't be parsed.")
            except Exception:
                # Anything else — a casparser internal error on a real-world
                # PDF shape the CASParseError/ParserException catches above
                # don't cover — must not surface as a bare, undiagnosable 500.
                # Logged with the full traceback (visible in Render's log
                # viewer) so a report of "upload failed" is actually
                # debuggable instead of a dead end.
                logger.exception("read_cas_pdf failed on an uncategorised exception")
                raise HTTPException(422, "This statement couldn't be parsed. If this keeps happening, it's a bug — the server log has the details.")

    if isinstance(parsed, NSDLCASData):
        raise HTTPException(
            422,
            "This looks like an NSDL/CDSL demat statement. PortfolioIQ currently analyses "
            "CAMS/KFintech mutual-fund statements only.",
        )
    _fix_segregation_classification(parsed)

    # Replace-on-upload, not accumulate: this is meant to be a
    # one-statement-at-a-time analyser, not an ever-growing multi-investor
    # portfolio — confirmed directly after real confusion from two
    # unrelated people's holdings silently combining into one total on
    # every upload. A byte-identical re-upload is still short-circuited as
    # a no-op "duplicate" (checked here, before anything is touched, using
    # the same file_hash ingest_cas itself would compute) so re-uploading
    # the same file twice doesn't wipe and immediately reimport identical
    # data. Any other file — even a newer statement for the same person —
    # replaces everything: config/preferences/groups survive (they're app
    # settings, not statement data), but every prior statement, holding,
    # transaction, and cached NAV/enrichment value is gone. This check
    # itself is fast (one SELECT) and stays synchronous, right here —
    # only the wipe+ingest that follows needs to be backgrounded.
    file_hash = hashlib.sha256(content).hexdigest()
    existing_upload = session.execute(select(CasUpload).where(CasUpload.file_hash == file_hash)).scalar_one_or_none()
    if existing_upload:
        return {
            "status": "duplicate",
            "message": "This exact statement has already been imported.",
            "upload_id": existing_upload.upload_id,
        }

    # The wipe+ingest itself now runs entirely in the background (see
    # _run_ingest_job_sync) instead of upload_cas awaiting it directly.
    # Real reason: a real ~50-scheme statement takes minutes of
    # sequential mfapi.in resolution calls (each call's own latency is
    # highly variable, 1-15s, with retries on failure) — reproduced
    # live as a 502 from Render's own reverse proxy, which gave up
    # waiting on the response before the ingest even finished. Holding
    # one HTTP connection open for however long that happens to take was
    # never going to be reliable regardless of how correct the ingest
    # logic itself is. ingest_jobs tracks only the most recent attempt
    # (this is a single-investor tool — one statement in flight at a
    # time), so any previous FINISHED row is cleared before creating
    # this one — status != "processing" is deliberate, not incidental:
    # an earlier version deleted unconditionally here, which silently
    # defeated the whole concurrent-upload lock below. A second request
    # that raced past the early check above would reach this exact line
    # and delete the FIRST request's still-running "processing" row
    # right before inserting its own, so the unique index never even
    # got a chance to reject anything — both requests "succeeded" with
    # different job_ids, confirmed live. Excluding "processing" here
    # means a still-running job is never touched by this cleanup, so if
    # two requests really do race to this point, the second's INSERT
    # collides with the first's still-present row and the index catches it.
    session.query(IngestJob).filter(IngestJob.status != "processing").delete()
    job = IngestJob(status="processing")
    session.add(job)
    try:
        session.flush()  # assigns job.job_id
        # Explicit commit here, not left to Depends(get_session)'s own
        # end-of-request cleanup — caught live: the background task (and
        # the client's own very first poll, moments later) both need
        # this row to already be durably visible, and relying on
        # FastAPI's implicit post-response commit timing was NOT
        # reliable enough in practice — reproduced twice, the response
        # correctly showed a fresh job_id every time, but the row was
        # simply absent from a direct DB check made right after.
        # Committing explicitly here removes any doubt about exactly
        # when this specific write becomes visible.
        session.commit()
    except IntegrityError:
        # The early "is one already processing?" check above can't
        # fully close the race on its own — two requests can both pass
        # it before either has written its own row. This is what
        # actually does: IngestJob's partial unique index lets Postgres
        # reject a second concurrent "processing" row outright, and this
        # is that rejection landing as a clean 409 instead of a bare 500.
        session.rollback()
        raise HTTPException(409, "An import is already in progress. Wait for it to finish (see the status bar) before starting another.")

    background_tasks.add_task(_run_ingest_job_sync, job.job_id, content, parsed)

    return {
        "status": "processing",
        "job_id": job.job_id,
        "investor_name": parsed.investor_info.name,
        "statement_period": {"from": parsed.statement_period.from_, "to": parsed.statement_period.to},
    }


@app.get("/api/upload-status/{job_id}")
def get_upload_status(job_id: int, session: Session = Depends(get_session)):
    job = session.get(IngestJob, job_id)
    if job is None:
        # Not a 404-as-"never existed" — ingest_jobs only ever keeps the
        # latest row, so this means a newer upload started (and cleared
        # this one) since the client last had a job_id. The frontend
        # only ever polls a job_id it just got from its own upload, so
        # this should be rare in practice — a second upload started
        # before the first's poll loop noticed it was superseded.
        raise HTTPException(404, "This upload isn't being tracked anymore — a newer upload may have started since.")
    if job.status == "processing":
        return {"status": "processing"}
    # debug_stage is the ingest job's OWN status; enrichment keeps
    # running after it — included here too (temporary diagnostic, see
    # IngestJob.debug_stage) so it's visible on the same poll.
    if job.status == "error":
        return {"status": "error", "message": job.error_detail}
    return {"status": "ok", "debug_stage": job.debug_stage, **(job.result_json or {})}


# ------------------------------------------------------------- portfolio ----

@app.get("/api/portfolio")
def get_portfolio(
    include_zero_value: bool = Query(False),
    level: Optional[str] = Query(None), group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None), arn: Optional[str] = Query(None),
    valuation_date: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    val_date = date.fromisoformat(valuation_date) if valuation_date else date.today()
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    all_metrics = [portfolio_service.compute_holding_metrics(session, hid, val_date) for hid in holding_ids]
    if not include_zero_value:
        all_metrics = [m for m in all_metrics if m.balance_units > 0]

    quality = "OK"
    if any(m.reconciliation_status != "reconciled" for m in all_metrics):
        quality = "PARTIAL"

    config = config_service.load_config(session)

    # Asset-class subtotals + grand total (spec 9.6): XIRR recalculated
    # from each bucket's own consolidated cash flows, never averaged from
    # the member holdings' individual XIRRs — portfolio_service.aggregate
    # already does exactly this.
    def _asset_class_bucket(ac: Optional[str]) -> str:
        return ac if ac in ("EQUITY", "DEBT") else "OTHER"

    buckets: dict[str, list[portfolio_service.HoldingMetrics]] = {"EQUITY": [], "DEBT": [], "OTHER": []}
    for m in all_metrics:
        buckets[_asset_class_bucket(m.asset_class)].append(m)

    def _agg_dict(metrics: list[portfolio_service.HoldingMetrics]) -> dict:
        agg = portfolio_service.aggregate(session, metrics, val_date)
        return {
            "invested_value": agg.invested_value, "current_value": agg.current_value, "gain": agg.gain,
            "absolute_return_pct": agg.absolute_return_pct, "weighted_days_held": agg.weighted_days_held,
            "xirr": agg.xirr_pct,
        }

    subtotals = {k: _agg_dict(v) for k, v in buckets.items() if v}
    subtotals["total"] = _agg_dict(all_metrics)

    # Uploads replace rather than accumulate now, so there's always at
    # most one CAS statement in the system — its own investor_info.name
    # is the natural zero-config source of truth for the header, and
    # showing it doesn't depend on the user having set anything up in
    # Settings first. Config-derived labels (from Settings' group/
    # investor/ARN mapping) still take priority when configured, since
    # that's how a user re-labels or attributes things beyond the CAS's
    # own name — falling back to the CAS name only when nothing's been
    # configured for these holdings' advisors at all.
    investor_names = sorted({
        config_service.find_owner_for_arn(config, m.advisor_arn)[1]
        for m in all_metrics if m.advisor_arn
    } - {None})
    if not investor_names:
        investor_names = sorted({
            (u.raw_parsed_json.get("investor_info") or {}).get("name")
            for u in session.execute(select(CasUpload)).scalars()
        } - {None, ""})

    return {
        **_response_meta(session, data_quality=quality),
        "investor_names": investor_names,
        "holdings_coverage_from": _holdings_coverage_from(session),
        "schemes": [_metrics_to_dict(m, config) for m in all_metrics],
        "subtotals": subtotals,
    }


@app.get("/api/portfolio/snapshot")
def get_snapshot(
    start_date: Optional[str] = Query(None), end_date: Optional[str] = Query(None),
    level: Optional[str] = Query(None), group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None), arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    end = date.fromisoformat(end_date) if end_date else date.today()
    start = date.fromisoformat(start_date) if start_date else None

    given_period = snapshot_service.compute_snapshot(session, holding_ids, start, end)
    since_inception = snapshot_service.compute_snapshot(session, holding_ids, None, end)

    def _fmt(bucket: dict) -> dict:
        return {k: (str(v) if isinstance(v, type(date.today())) else v) for k, v in bucket.items()}

    return {
        **_response_meta(session),
        "given_period": {"start_date": start.isoformat() if start else None, "end_date": end.isoformat(), **{k: _fmt(v) for k, v in given_period.items()}},
        "since_inception": {"start_date": None, "end_date": end.isoformat(), **{k: _fmt(v) for k, v in since_inception.items()}},
    }


@app.get("/api/portfolio/summary")
def get_portfolio_summary(session: Session = Depends(get_session)):
    config = config_service.load_config(session)
    groups_out = []
    for group in config.get("groups", []):
        investors_out = []
        for investor in group.get("investors", []):
            arn_labels = investor.get("arn_labels", {})
            advisors_out = []
            all_records_holding_ids: list[int] = []
            for arn in investor.get("arns", []):
                holding_ids = list(session.execute(select(Holding.holding_id).where(Holding.advisor_arn == arn)).scalars())
                if not holding_ids:
                    continue
                metrics = [portfolio_service.compute_holding_metrics(session, hid, date.today()) for hid in holding_ids]
                agg = portfolio_service.aggregate(session, metrics, date.today())
                external_flows = _external_cashflows(session, holding_ids)

                nifty50 = benchmark_service.simulate_benchmark_xirr(session, external_flows, date.today(), "Nifty 50")
                nifty500 = benchmark_service.simulate_benchmark_xirr(session, external_flows, date.today(), "Nifty 500")

                advisors_out.append({
                    "arn": arn, "advisor_label": arn_labels.get(arn, arn),
                    "investment_value": agg.invested_value, "current_value": agg.current_value,
                    "absolute_return_pct": agg.absolute_return_pct, "xirr": agg.xirr_pct,
                    "largecap_pct": None, "midcap_pct": None, "smallcap_pct": None,
                    "nifty50_proxy_xirr": nifty50.value, "nifty50_proxy_disclosure": nifty50.proxy_disclosure,
                    "nifty500_xirr": nifty500.value, "nifty500_status": nifty500.status,
                    "fund_respective_xirr": None, "fund_respective_status": "unavailable",
                })
                all_records_holding_ids.extend(holding_ids)

            blended = None
            if all_records_holding_ids:
                all_metrics = [portfolio_service.compute_holding_metrics(session, hid, date.today()) for hid in all_records_holding_ids]
                blended = portfolio_service.aggregate(session, all_metrics, date.today()).xirr_pct

            investors_out.append({
                "investor_name": investor.get("investor_name"),
                "all_advisor_xirr": blended,
                "advisors": advisors_out,
            })
        groups_out.append({"group_name": group.get("group_name"), "investors": investors_out})
    return {**_response_meta(session), "groups": groups_out}


def _external_cashflows(session: Session, holding_ids: list[int]) -> list[tuple[date, Any]]:
    """Real external cash flows ONLY — no terminal current_value appended.
    This is what spec 14.2's benchmark simulation needs ("simulate the
    same external cash amounts on the same dates"): the simulation
    computes its OWN terminal value from the simulated benchmark
    position, so a terminal value already baked into this list would
    double-count as a phantom withdrawal on the valuation date (caught
    live: made the simulated benchmark position go negative, since a
    ~63k "withdrawal" had no matching purchase history in benchmark
    units). portfolio_service.aggregate() is the right place for the
    REAL portfolio's own XIRR — it appends its own terminal value
    correctly already; this function must not also do so."""
    flows = []
    for hid in holding_ids:
        for t in session.execute(select(Transaction).where(Transaction.holding_id == hid)).scalars():
            cf = portfolio_service._txn_cash_flow(t)
            if cf is not None:
                flows.append((t.date, cf))
    return flows


@app.get("/api/portfolio/fund-summary")
def get_fund_summary(
    level: Optional[str] = Query(None), group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None), arn: Optional[str] = Query(None),
    include_zero_value: bool = Query(False),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    seen: dict[int, dict] = {}
    for hid in holding_ids:
        m = portfolio_service.compute_holding_metrics(session, hid, date.today())
        if not include_zero_value and m.balance_units <= 0:
            continue
        holding = session.get(Holding, hid)
        if holding.scheme_id in seen:
            continue
        payload = enrichment_bridge.get_cached_enrichment(session, holding.scheme_id) or {}
        seen[holding.scheme_id] = {
            "scheme_name": m.scheme_name, "amfi": session.get(Scheme, holding.scheme_id).amfi_code,
            "is_held": True, "corpus_cr": payload.get("corpus_cr"),
            "largecap_pct": payload.get("largecap_pct"), "midcap_pct": payload.get("midcap_pct"),
            "smallcap_pct": payload.get("smallcap_pct"),
            "returns": payload.get("returns") or {"1m": None, "3m": None, "6m": None, "1y": None, "2y": None, "3y": None},
            "risk": payload.get("risk") or {"std_dev": None, "sharpe": None, "sortino": None, "max_drawdown": None, "alpha": None, "beta": None},
            "nav_as_of": payload.get("nav_as_of"),
        }
    return {**_response_meta(session), "funds": list(seen.values())}


@app.get("/api/portfolio/exposure")
def get_exposure(
    level: Optional[str] = Query(None), group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None), arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    # This endpoint ignored every filter entirely until now — the page's
    # own Level/Investor/Advisor selector was visibly interactive but had
    # zero effect on what Exposure actually showed, found auditing every
    # page for exactly this kind of silent no-op. Same _scope_holding_ids
    # helper every other filterable endpoint already uses.
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    metrics = [portfolio_service.compute_holding_metrics(session, hid, date.today()) for hid in holding_ids]
    result = exposure_service.compute_exposure(metrics)
    return {
        **_response_meta(session),
        "top_amcs": [{"amc_name": a.amc_name, "current_value": a.current_value, "pct_of_portfolio": a.pct_of_portfolio} for a in result.top_amcs],
        "top_funds": [{"scheme_name": f.scheme_name, "current_value": f.current_value, "pct_of_portfolio": f.pct_of_portfolio} for f in result.top_funds],
        "cap_allocation": {
            "largecap_pct": result.cap_allocation.largecap_pct, "midcap_pct": result.cap_allocation.midcap_pct,
            "smallcap_pct": result.cap_allocation.smallcap_pct, "other_pct": result.cap_allocation.other_pct,
            "status": result.cap_allocation.status,
        },
    }


@app.get("/api/transactions")
def get_transactions(
    include_zero_value: bool = Query(True),
    level: Optional[str] = Query(None), group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None), arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    out = []
    config = config_service.load_config(session)
    for hid in holding_ids:
        holding = session.get(Holding, hid)
        m_balance = sum((
            t.units for t in session.execute(select(Transaction).where(Transaction.holding_id == hid)).scalars()
            if t.units is not None
        ), portfolio_service.ZERO)
        if not include_zero_value and m_balance <= 0:
            continue
        scheme = session.get(Scheme, holding.scheme_id)
        folio = session.get(Folio, holding.folio_id)
        group_name_r, investor_name_r = config_service.find_owner_for_arn(config, holding.advisor_arn) if holding.advisor_arn else (None, None)
        advisor_label = config_service.find_arn_label(config, holding.advisor_arn) if holding.advisor_arn else None
        for t in session.execute(select(Transaction).where(Transaction.holding_id == hid)).scalars():
            out.append({
                "date": t.date.isoformat(), "type": t.type, "description": t.description,
                "amount": t.amount, "units": t.units, "nav": t.nav, "balance": t.balance,
                "folio": folio.normalized_folio, "scheme_name": scheme.name, "isin": scheme.isin,
                "amfi": scheme.amfi_code, "advisor": holding.advisor_arn, "advisor_label": advisor_label,
                "group_name": group_name_r, "investor_name": investor_name_r,
            })
    out.sort(key=lambda x: x["date"], reverse=True)
    return {**_response_meta(session), "transactions": out}


# --------------------------------------------------------- capital gains ----

@app.get("/api/capital-gains")
def get_capital_gains(
    level: Optional[str] = Query(None), group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None), arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    rows, excluded = gains_service_db.realized_gains(session, holding_ids)
    gift_rows = gains_service_db.gifts(session, holding_ids)
    config = config_service.load_config(session)

    def _row(r) -> dict:
        return {
            "fy": r.fy, "scheme": r.scheme_name, "isin": r.isin, "fund_type": r.fund_type,
            "advisor": r.advisor_arn, "advisor_label": config_service.find_arn_label(config, r.advisor_arn) if r.advisor_arn else None,
            "purchase_date": r.acquired_date.isoformat(), "sale_date": r.sold_date.isoformat(),
            "units": r.units, "acquisition_value": r.acquisition_value, "sale_value": r.sale_value,
            "gain": r.gain, "gain_type": r.gain_type, "ltcg": r.ltcg, "stcg": r.stcg,
        }

    fys = gains_service_db.available_fys(rows)
    warnings = []
    if excluded:
        warnings.append(
            f"{len(excluded)} disposal(s) involving gifted-in units excluded from gains — "
            "donor's cost basis/holding period isn't available from a single CAS (Sec 49(1))."
        )
    return {
        **_response_meta(session, warnings=warnings),
        "gains": [_row(r) for r in rows], "gifts": [
            {"fy": g.fy, "scheme": g.scheme_name, "isin": g.isin, "direction": g.direction,
             "date": g.date.isoformat(), "units": g.units, "nav": g.nav, "value": g.value,
             "counterparty_folio": g.counterparty_folio}
            for g in gift_rows
        ],
        "fys": fys,
    }


@app.get("/api/capital-gains/112a.csv")
def get_112a_csv(
    fy: str = Query(...),
    level: Optional[str] = Query(None), group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None), arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    csv_data = gains_service_db.generate_112a_csv(session, fy, holding_ids)
    return Response(content=csv_data, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="capital-gains-112a-{fy}.csv"',
    })


# ------------------------------------------------------------- data quality ----

@app.get("/api/data-quality")
def get_data_quality(session: Session = Depends(get_session)):
    holding_ids = _all_holding_ids(session)
    issues = []
    for hid in holding_ids:
        holding = session.get(Holding, hid)
        if holding.reconciliation_status != "reconciled":
            scheme = session.get(Scheme, holding.scheme_id)
            folio = session.get(Folio, holding.folio_id)
            issues.append({
                "holding_id": hid, "scheme_name": scheme.name, "folio": folio.normalized_folio,
                "status": holding.reconciliation_status,
            })
    return {**_response_meta(session), "issues": issues}


# ------------------------------------------------------------------ config ----

@app.get("/api/config")
def get_config(session: Session = Depends(get_session)):
    return config_service.load_config(session)


@app.post("/api/config")
def post_config(config: dict, session: Session = Depends(get_session)):
    config_service.save_config(session, config)
    return {"status": "ok"}


@app.get("/api/enrich/status")
def get_enrich_status(session: Session = Depends(get_session)):
    # "pending" was hardcoded to 0 — meaning the frontend's poll loop
    # (App.jsx's pollEnrichStatus: keep polling while pending > 0, then
    # refresh once) always saw it as already done and stopped after a
    # single check, even while the real background enrichment task was
    # still running for minutes on a large upload. That's very likely
    # the actual reason "upload, see ₹0, needs a manual refresh later"
    # kept recurring this session — not a fresh instance of the
    # background-task bug each time, but this one poll-signal bug
    # making every fix to that task invisible to the UI regardless.
    #
    # A scheme gets an enrichment_cache row the moment it's been
    # attempted, whether it succeeded (status="ok") or genuinely
    # couldn't be resolved (status="unavailable") — so "has any row at
    # all" is "attempted," not "succeeded." pending = total minus
    # attempted, which reaches 0 exactly when the background task has
    # worked through every scheme, whatever the outcome — not stuck
    # forever if a specific scheme can never actually succeed.
    # Scoped to schemes actually HELD (referenced by at least one
    # Holding) — not every row in the schemes table. That table also
    # holds the Nifty 50 benchmark proxy fund (benchmark_service's
    # _get_or_create_proxy_scheme, created the moment any benchmark
    # XIRR is computed), which is real but enriched through a totally
    # separate path (refresh_nifty50_proxy_nav writes straight to
    # nav_cache) and never gets an enrichment_cache row of its own.
    # Counting it here meant "pending" could never reach 0 — reproduced
    # live, stuck at 1 remaining no matter how long enrichment ran,
    # for every single portfolio, since the proxy scheme always exists
    # once any page computes a benchmark comparison.
    held = set(session.execute(select(Holding.scheme_id).distinct()).scalars().all())
    cache_rows = session.execute(
        select(EnrichmentCache.scheme_id, EnrichmentCache.status).where(EnrichmentCache.scheme_id.in_(held))
    ).all()
    attempted = {sid for sid, _status in cache_rows}
    enriched = {sid for sid, status in cache_rows if status == "ok"}
    last_run = session.execute(
        select(EnrichmentCache.fetched_at).where(EnrichmentCache.scheme_id.in_(held))
        .order_by(EnrichmentCache.fetched_at.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "total_schemes": len(held), "enriched": len(enriched),
        "failed": len(attempted - enriched), "pending": len(held - attempted),
        "last_run": last_run.isoformat() if last_run else None,
    }


# -------------------------------------------------------------- deletion ----
# Spec 19: "Provide a deletion workflow for the original PDF and derived
# personal data." The raw CAS PDF itself is never persisted at all (see
# upload_cas: it's read into a TemporaryDirectory and discarded once
# casparser has parsed it) — only the parser's own structured JSON is
# kept, in cas_uploads.raw_parsed_json, for audit. This wipes that and
# every other table so a user can fully reset the personal data this
# tool holds. Full-wipe rather than per-upload deletion: holdings/lots
# can already be shared across multiple accumulated CAS uploads for the
# same folio, so a correct partial delete needs to account for that —
# out of scope for a first pass; noted as a known limitation.

@app.delete("/api/all-data")
def delete_all_data(session: Session = Depends(get_session)):
    from models import (
        BenchmarkPoint, BenchmarkDefinition, ConfigInvestorArn, ConfigInvestor, ConfigGroup,
        DisposalAllocation, EnrichmentCache, NavCache, Preference, PurchaseLot, SchemeAlias,
        SchemeBenchmarkMap,
    )
    # Real child-before-parent order, traced through every ForeignKey in
    # models.py, not assembled by trial and error. The previous version
    # added SchemeBenchmarkMap (which does FK into both schemes and
    # benchmark_definitions) but still deleted Scheme before NavCache and
    # EnrichmentCache — both of which also FK into schemes — so the same
    # class of bug (an uncaught IntegrityError, bare 500, no message,
    # since nothing wraps this endpoint in try/except) was still live.
    # Caught by reproducing the real upload-replace 500 directly against
    # a copy of the actual data and reading psycopg's own error, which
    # named nav_cache_scheme_id_fkey specifically — not a guess this time.
    for model in (
        DisposalAllocation, PurchaseLot, Transaction, SchemeAlias,
        NavCache, EnrichmentCache, SchemeBenchmarkMap, Holding, Scheme, Folio, CasUpload,
        BenchmarkPoint, BenchmarkDefinition,
        ConfigInvestorArn, ConfigInvestor, ConfigGroup, Preference,
        IngestJob,
    ):
        session.query(model).delete()
    return {"status": "ok", "message": "All statements, holdings, gains, config, and cached market data deleted."}
