"""Authenticated single-owner portfolio API. Import jobs run in worker.py."""

from __future__ import annotations
import base64
import hashlib
import re
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional, Any
from dotenv import load_dotenv

load_dotenv()
from fastapi import (
    FastAPI,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
import auth, db, jobs, ledger, config_service, portfolio_service, snapshot_service, exposure_service, enrichment_bridge, gains_service_db, benchmark_service
from models import (
    CasUpload,
    EnrichmentCache,
    Folio,
    Holding,
    IngestJob,
    Scheme,
    Transaction,
)

CALCULATION_VERSION = "3.0.1"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(app):
    auth.secret()
    auth.access_mode()
    db.init_db()
    yield


app = FastAPI(title="PortfolioIQ", lifespan=lifespan)
_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Requested-With"],
)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    path = request.url.path
    local_mode = auth.access_mode() == "local"
    if local_mode and path != "/api/health" and not auth.local_request(request):
        return JSONResponse(
            status_code=403,
            content={"detail": "This installation only accepts local access."},
        )
    if request.method != "OPTIONS" and path not in {
        "/api/health",
        "/api/session",
        "/api/login",
    }:
        if not local_mode and not auth.valid_session(request.cookies.get(auth.COOKIE)):
            return JSONResponse(
                status_code=401,
                content={"detail": "Please sign in to access your portfolio."},
            )
    if (
        request.method in {"POST", "DELETE"}
        and request.headers.get("x-requested-with") != "PortfolioIQ"
    ):
        return JSONResponse(
            status_code=403, content={"detail": "Invalid request origin."}
        )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class Login(BaseModel):
    password: str = Field(max_length=1024)


_failures = defaultdict(deque)


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    key = request.client.host if request.client else "unknown"
    attempts = _failures[key]
    now = time.monotonic()
    while attempts and attempts[0] < now - 300:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(
            429, "Too many sign-in attempts. Try again in five minutes."
        )
    if not auth.password_matches(body.password):
        attempts.append(now)
        raise HTTPException(401, "Incorrect owner password.")
    attempts.clear()
    response.set_cookie(
        auth.COOKIE,
        auth.make_session(),
        max_age=auth.MAX_AGE,
        httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
        samesite="strict",
        path="/",
    )
    return {"authenticated": True}


@app.get("/api/session")
def session_status(request: Request):
    required = auth.access_mode() != "local"
    return {
        "authenticated": not required
        or auth.valid_session(request.cookies.get(auth.COOKIE)),
        "password_required": required,
    }


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"status": "ok"}


@app.get("/api/health")
def health():
    return {"status": "ok"}


def get_session():
    # Each response observes one committed dataset even if import completes
    # between its individual batched queries.
    with db.get_session(consistent=True) as session:
        yield session


def write_session():
    with db.get_session() as session:
        yield session


def _response_meta(session, warnings=None, data_quality="OK", valuation_date=None):
    upload = session.scalar(select(CasUpload).limit(1))
    warnings = list(
        dict.fromkeys([*(upload.warnings or [] if upload else []), *(warnings or [])])
    )
    if warnings:
        data_quality = "PARTIAL"
    return {
        "requested_valuation_date": (valuation_date or date.today()).isoformat(),
        "holdings_coverage_through": upload.period_to.isoformat()
        if upload and upload.period_to
        else None,
        "dataset_id": upload.upload_id if upload else None,
        "nav_policy": "ON_OR_BEFORE",
        "calculation_version": CALCULATION_VERSION,
        "warnings": warnings or [],
        "data_quality": data_quality,
    }


@app.post("/api/upload-cas", status_code=202)
def upload_cas(
    file: UploadFile = File(...),
    password: str = Form(default=""),
    session: Session = Depends(write_session),
):
    filename = (file.filename or "").lower()
    if not filename.endswith((".pdf", ".json")):
        raise HTTPException(400, "Upload a CAMS/KFintech PDF or parsed CAS JSON.")
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(400, "The file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Maximum upload size is 20 MB.")
    if jobs.active_job(session):
        raise HTTPException(409, "An import or refresh is already running.")
    digest = hashlib.sha256(content).hexdigest()
    existing = session.scalar(
        select(CasUpload).where(
            CasUpload.file_hash == digest, CasUpload.parse_status == CALCULATION_VERSION
        )
    )
    if existing:
        return {
            "status": "duplicate",
            "dataset_id": existing.upload_id,
            "investor_name": (existing.raw_parsed_json.get("investor_info") or {}).get(
                "name"
            ),
            "statement_period": {
                "from": str(existing.period_from),
                "to": str(existing.period_to),
            },
        }
    job = jobs.enqueue(
        session,
        "upload",
        {
            "content": base64.b64encode(content).decode(),
            "filename": filename,
            "password": password,
        },
    )
    return jobs.public(job)


@app.get("/api/upload-status/{job_id}")
def upload_status(job_id: int, session: Session = Depends(get_session)):
    job = session.get(IngestJob, job_id)
    if job is None:
        raise HTTPException(404, "This import is no longer available.")
    return jobs.public(job)


@app.get("/api/jobs/current")
def current_job(session: Session = Depends(get_session)):
    job = jobs.active_job(session)
    return jobs.public(job) if job else None


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, session: Session = Depends(write_session)):
    job = session.scalar(
        select(IngestJob).where(IngestJob.job_id == job_id).with_for_update()
    )
    if not job:
        raise HTTPException(404, "Job not found.")
    job.cancel_requested = True
    if job.status == "queued":
        job.status = "cancelled"
        job.payload = None
    return {"status": "cancellation_requested"}


@app.get("/api/statement")
def statement(session: Session = Depends(get_session)):
    upload = session.scalar(select(CasUpload).limit(1))
    return {
        **_response_meta(session),
        "investor_name": (upload.raw_parsed_json.get("investor_info") or {}).get("name")
        if upload
        else None,
        "statement_period": {
            "from": str(upload.period_from),
            "to": str(upload.period_to),
        }
        if upload
        else None,
    }


@app.get("/api/enrich/status")
def enrich_status(session: Session = Depends(get_session)):
    held = set(session.scalars(select(Holding.scheme_id).distinct()))
    rows = list(
        session.scalars(
            select(EnrichmentCache).where(EnrichmentCache.scheme_id.in_(held))
        )
    )
    successful = {r.scheme_id for r in rows if r.status == "ok"}
    attempted = {r.scheme_id for r in rows}
    stale = sum(not enrichment_bridge._is_fresh(r.fetched_at) for r in rows)
    job = jobs.active_job(session)
    latest_job = session.scalar(
        select(IngestJob).order_by(IngestJob.job_id.desc()).limit(1)
    )
    return {
        "last_job": jobs.public(latest_job) if latest_job else None,
        "total_schemes": len(held),
        "enriched": len(successful),
        "failed": len(attempted - successful),
        "pending": len(held - attempted),
        "stale": stale,
        "active_job": jobs.public(job) if job else None,
        "last_run": (max(r.fetched_at for r in rows if r.fetched_at).isoformat() + "Z")
        if any(r.fetched_at for r in rows)
        else None,
    }


@app.post("/api/enrich/retry", status_code=202)
def retry_enrichment(session: Session = Depends(write_session)):
    if not session.scalar(select(CasUpload.upload_id).limit(1)):
        raise HTTPException(409, "Import a statement first.")
    return jobs.public(jobs.enqueue(session, "refresh"))


@app.delete("/api/all-data")
def delete_all_data(session: Session = Depends(write_session)):
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": jobs.CONTROL_LOCK}
    )
    if jobs.active_job(session):
        raise HTTPException(
            409, "Cancel the active job and wait for it to stop before resetting."
        )
    from models import (
        DisposalAllocation,
        PurchaseLot,
        SchemeAlias,
        NavCache,
        SchemeBenchmarkMap,
        BenchmarkPoint,
        BenchmarkDefinition,
        ConfigInvestorArn,
        ConfigInvestor,
        ConfigGroup,
        Preference,
    )

    for model in (
        DisposalAllocation,
        PurchaseLot,
        Transaction,
        Holding,
        Folio,
        CasUpload,
        SchemeAlias,
        NavCache,
        EnrichmentCache,
        SchemeBenchmarkMap,
        Scheme,
        BenchmarkPoint,
        BenchmarkDefinition,
        ConfigInvestorArn,
        ConfigInvestor,
        ConfigGroup,
        Preference,
        IngestJob,
    ):
        session.query(model).delete(synchronize_session=False)
    return {"status": "ok"}


def _scope_holding_ids(
    session: Session,
    level: Optional[str],
    group_name: Optional[str],
    investor_name: Optional[str],
    arn: Optional[str],
) -> Optional[list[int]]:
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
    holding_ids = list(
        session.execute(
            select(Holding.holding_id).where(Holding.advisor_arn.in_(arns_in_scope))
        ).scalars()
    )
    return holding_ids


def _all_holding_ids(session: Session) -> list[int]:
    return list(session.execute(select(Holding.holding_id)).scalars())


def _holdings_coverage_through(session: Session) -> Optional[str]:
    latest = session.execute(
        select(CasUpload.period_to).order_by(CasUpload.period_to.desc()).limit(1)
    ).scalar_one_or_none()
    return latest.isoformat() if latest else None


def _holdings_coverage_from(session: Session) -> Optional[str]:
    earliest = session.execute(
        select(CasUpload.period_from).order_by(CasUpload.period_from.asc()).limit(1)
    ).scalar_one_or_none()
    return earliest.isoformat() if earliest else None


def _metrics_to_dict(m: portfolio_service.HoldingMetrics, config: dict) -> dict:
    group_name, investor_name = (
        config_service.find_owner_for_arn(config, m.advisor_arn)
        if m.advisor_arn
        else (None, None)
    )
    return {
        "holding_id": m.holding_id,
        "folio": m.folio,
        "amc": m.amc,
        "scheme_name": m.scheme_name,
        "isin": m.isin,
        "asset_class": m.asset_class,
        "advisor": m.advisor_arn,
        "advisor_label": config_service.find_arn_label(config, m.advisor_arn)
        if m.advisor_arn
        else None,
        "group_name": group_name,
        "investor_name": investor_name,
        "balance_units": m.balance_units,
        "weighted_purchase_nav": m.weighted_purchase_nav,
        "current_nav": m.current_nav,
        "current_nav_date": m.current_nav_date.isoformat()
        if m.current_nav_date
        else None,
        "net_invested_value": m.remaining_purchase_value,
        "current_value": m.current_value,
        "absolute_gain": m.gain,
        "absolute_gain_pct": m.absolute_return_pct,
        "weighted_days_held": m.weighted_days_held,
        "xirr": m.xirr_pct,
        "reconciliation_status": m.reconciliation_status,
        "flags": [{"code": f.code, "detail": f.detail} for f in m.flags],
    }


@app.get("/api/portfolio")
def get_portfolio(
    include_zero_value: bool = Query(False),
    include_exposure: bool = Query(False),
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
    valuation_date: Optional[date] = Query(None),
    session: Session = Depends(get_session),
):
    val_date = valuation_date or date.today()
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    _ctx = portfolio_service.build_context(session, holding_ids, val_date)
    all_metrics = [
        portfolio_service.compute_holding_metrics(session, hid, val_date, _ctx)
        for hid in holding_ids
    ]
    quality = "OK"
    if any(f.code in ledger.BLOCKING_CODES for m in all_metrics for f in m.flags):
        quality = "PARTIAL"
    config = config_service.load_config(session)

    def _asset_class_bucket(ac: Optional[str]) -> str:
        return ac if ac in ("EQUITY", "HYBRID", "DEBT") else "OTHER"

    buckets: dict[str, list[portfolio_service.HoldingMetrics]] = {
        "EQUITY": [],
        "HYBRID": [],
        "DEBT": [],
        "OTHER": [],
    }
    for m in all_metrics:
        buckets[_asset_class_bucket(m.asset_class)].append(m)

    def _agg_dict(metrics: list[portfolio_service.HoldingMetrics]) -> dict:
        agg = portfolio_service.aggregate(session, metrics, val_date, _ctx)
        return {
            "invested_value": agg.invested_value,
            "current_value": agg.current_value,
            "gain": agg.gain,
            "absolute_return_pct": agg.absolute_return_pct,
            "weighted_days_held": agg.weighted_days_held,
            "xirr": agg.xirr_pct,
            "known_current_value": agg.known_current_value,
            "valued_holdings": agg.valued_holdings,
            "total_holdings": agg.total_holdings,
            "closed_holdings": agg.closed_holdings,
        }

    subtotals = {k: _agg_dict(v) for k, v in buckets.items() if v}
    subtotals["total"] = _agg_dict(all_metrics)
    investor_names = sorted(
        {
            config_service.find_owner_for_arn(config, m.advisor_arn)[1]
            for m in all_metrics
            if m.advisor_arn
        }
        - {None}
    )
    if not investor_names:
        investor_names = sorted(
            {
                (u.raw_parsed_json.get("investor_info") or {}).get("name")
                for u in session.execute(select(CasUpload)).scalars()
            }
            - {None, ""}
        )
    return {
        **_response_meta(session, data_quality=quality, valuation_date=val_date),
        "investor_names": investor_names,
        "holdings_coverage_from": _holdings_coverage_from(session),
        "schemes": [
            _metrics_to_dict(m, config)
            for m in all_metrics
            if include_zero_value or m.balance_units > 0
        ],
        "subtotals": subtotals,
        **({"exposure": _exposure_payload(all_metrics)} if include_exposure else {}),
    }


@app.get("/api/portfolio/snapshot")
def get_snapshot(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    end = end_date or date.today()
    start = start_date
    if start and start > end:
        raise HTTPException(422, "Start date must be on or before end date.")
    _ctx = portfolio_service.build_context(session, holding_ids, end)
    given_period = snapshot_service.compute_snapshot(
        session, holding_ids, start, end, _ctx
    )
    since_inception = (
        given_period
        if start is None
        else snapshot_service.compute_snapshot(session, holding_ids, None, end, _ctx)
    )

    def _fmt(bucket: dict) -> dict:
        return {
            k: str(v) if isinstance(v, type(date.today())) else v
            for k, v in bucket.items()
        }

    return {
        **_response_meta(
            session,
            valuation_date=end,
            data_quality="PARTIAL"
            if any(b["data_quality"] == "PARTIAL" for b in given_period.values())
            else "OK",
        ),
        "given_period": {
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat(),
            **{k: _fmt(v) for k, v in given_period.items()},
        },
        "since_inception": {
            "start_date": None,
            "end_date": end.isoformat(),
            **{k: _fmt(v) for k, v in since_inception.items()},
        },
    }


@app.get("/api/portfolio/summary")
def get_portfolio_summary(session: Session = Depends(get_session)):
    config = config_service.load_config(session)
    ids = _all_holding_ids(session)
    shared = portfolio_service.build_context(session, ids, date.today())
    metrics_by_id = {
        hid: portfolio_service.compute_holding_metrics(
            session, hid, date.today(), shared
        )
        for hid in ids
    }
    by_arn = {}
    for hid, h in shared.holdings.items():
        by_arn.setdefault(h.advisor_arn, []).append(hid)
    groups_out = []
    for group in config.get("groups", []):
        investors_out = []
        for investor in group.get("investors", []):
            arn_labels = investor.get("arn_labels", {})
            advisors_out = []
            all_records_holding_ids: list[int] = []
            for arn in investor.get("arns", []):
                holding_ids = by_arn.get(arn, [])
                if not holding_ids:
                    continue
                _ctx = shared
                metrics = [metrics_by_id[hid] for hid in holding_ids]
                agg = portfolio_service.aggregate(session, metrics, date.today(), _ctx)
                external_flows = ledger.cashflows(
                    [
                        t
                        for hid in holding_ids
                        for t in shared.transactions.get(hid, [])
                    ],
                    date.today(),
                )
                nifty50 = benchmark_service.simulate_benchmark_xirr(
                    session, external_flows, date.today(), "Nifty 50"
                )
                nifty500 = benchmark_service.simulate_benchmark_xirr(
                    session, external_flows, date.today(), "Nifty 500"
                )
                advisors_out.append(
                    {
                        "arn": arn,
                        "advisor_label": arn_labels.get(arn, arn),
                        "investment_value": agg.invested_value,
                        "current_value": agg.current_value,
                        "absolute_return_pct": agg.absolute_return_pct,
                        "xirr": agg.xirr_pct,
                        "largecap_pct": None,
                        "midcap_pct": None,
                        "smallcap_pct": None,
                        "nifty50_proxy_xirr": nifty50.value
                        if agg.xirr_pct is not None
                        else None,
                        "nifty50_proxy_disclosure": nifty50.proxy_disclosure,
                        "nifty500_xirr": nifty500.value,
                        "nifty500_status": nifty500.status,
                        "fund_respective_xirr": None,
                        "fund_respective_status": "unavailable",
                    }
                )
                all_records_holding_ids.extend(holding_ids)
            blended = None
            if all_records_holding_ids:
                _all_ctx = shared
                all_metrics = [
                    metrics_by_id[hid] for hid in set(all_records_holding_ids)
                ]
                blended = portfolio_service.aggregate(
                    session, all_metrics, date.today(), _all_ctx
                ).xirr_pct
            investors_out.append(
                {
                    "investor_name": investor.get("investor_name"),
                    "all_advisor_xirr": blended,
                    "advisors": advisors_out,
                }
            )
        groups_out.append(
            {"group_name": group.get("group_name"), "investors": investors_out}
        )
    return {**_response_meta(session), "groups": groups_out}


def _external_cashflows(
    session: Session, holding_ids: list[int]
) -> list[tuple[date, Any]]:
    flows = []
    for hid in holding_ids:
        for t in session.execute(
            select(Transaction).where(Transaction.holding_id == hid)
        ).scalars():
            cf = portfolio_service._txn_cash_flow(t)
            if cf is not None:
                flows.append((t.date, cf))
    return flows


@app.get("/api/portfolio/fund-summary")
def get_fund_summary(
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
    include_zero_value: bool = Query(False),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    seen: dict[int, dict] = {}
    _ctx = portfolio_service.build_context(session, holding_ids, date.today())
    _payloads = enrichment_bridge.get_cached_enrichments(
        session, [h.scheme_id for h in _ctx.holdings.values()]
    )
    for hid in holding_ids:
        m = portfolio_service.compute_holding_metrics(
            session, hid, date.today(), _ctx, calculate_xirr=False
        )
        if not include_zero_value and m.balance_units <= 0:
            continue
        holding = _ctx.holdings.get(hid) or session.get(Holding, hid)
        if holding.scheme_id in seen:
            continue
        payload = _payloads.get(holding.scheme_id) or {}
        scheme_row = _ctx.schemes.get(holding.scheme_id) or session.get(
            Scheme, holding.scheme_id
        )
        seen[holding.scheme_id] = {
            "scheme_name": m.scheme_name,
            "amfi": scheme_row.amfi_code,
            "is_held": True,
            "corpus_cr": payload.get("corpus_cr"),
            "largecap_pct": payload.get("largecap_pct"),
            "midcap_pct": payload.get("midcap_pct"),
            "smallcap_pct": payload.get("smallcap_pct"),
            "returns": payload.get("returns")
            or {"1m": None, "3m": None, "6m": None, "1y": None, "2y": None, "3y": None},
            "risk": payload.get("risk")
            or {
                "std_dev": None,
                "sharpe": None,
                "sortino": None,
                "max_drawdown": None,
                "alpha": None,
                "beta": None,
            },
            "nav_as_of": payload.get("nav_as_of"),
            "stale": payload.get("stale", True),
            "status": payload.get("status", "unavailable"),
            "methodology": payload.get("risk_methodology"),
        }
    return {**_response_meta(session), "funds": list(seen.values())}


@app.get("/api/portfolio/exposure")
def get_exposure(
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if holding_ids is None:
        holding_ids = _all_holding_ids(session)
    _ctx = portfolio_service.build_context(session, holding_ids, date.today())
    metrics = [
        portfolio_service.compute_holding_metrics(
            session, hid, date.today(), _ctx, calculate_xirr=False
        )
        for hid in holding_ids
    ]
    return {
        **_response_meta(
            session,
            data_quality="PARTIAL"
            if any(m.current_value is None for m in metrics)
            else "OK",
        ),
        **_exposure_payload(metrics),
    }


def _exposure_payload(metrics):
    result = exposure_service.compute_exposure(metrics)
    return {
        "top_amcs": [
            {
                "amc_name": a.amc_name,
                "current_value": a.current_value,
                "pct_of_portfolio": a.pct_of_portfolio,
            }
            for a in result.top_amcs
        ],
        "top_funds": [
            {
                "scheme_name": f.scheme_name,
                "current_value": f.current_value,
                "pct_of_portfolio": f.pct_of_portfolio,
            }
            for f in result.top_funds
        ],
        "cap_allocation": {
            "largecap_pct": result.cap_allocation.largecap_pct,
            "midcap_pct": result.cap_allocation.midcap_pct,
            "smallcap_pct": result.cap_allocation.smallcap_pct,
            "other_pct": result.cap_allocation.other_pct,
            "status": result.cap_allocation.status,
        },
    }


@app.get("/api/capital-gains")
def get_capital_gains(
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
    fy: Optional[str] = Query(None, pattern=r"^(?:FY)?\d{4}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=250),
    gift_page: int = Query(1, ge=1),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    rows, excluded = gains_service_db.realized_gains(session, holding_ids)
    gift_rows = gains_service_db.gifts(session, holding_ids)
    config = config_service.load_config(session)

    def _row(r) -> dict:
        return {
            "fy": r.fy,
            "scheme": r.scheme_name,
            "isin": r.isin,
            "fund_type": r.fund_type,
            "advisor": r.advisor_arn,
            "advisor_label": config_service.find_arn_label(config, r.advisor_arn)
            if r.advisor_arn
            else None,
            "purchase_date": r.acquired_date.isoformat(),
            "sale_date": r.sold_date.isoformat(),
            "units": r.units,
            "acquisition_value": r.acquisition_value,
            "sale_value": r.sale_value,
            "gain": r.gain,
            "gain_type": r.gain_type,
            "ltcg": r.ltcg,
            "stcg": r.stcg,
        }

    fys = gains_service_db.available_fys(rows)
    selected_fy = ("FY" + fy.removeprefix("FY")) if fy else (fys[0] if fys else None)
    filtered = [row for row in rows if row.fy == selected_fy]
    totals = gains_service_db.fy_summary(rows, selected_fy)
    warnings = []
    if excluded:
        warnings.append(
            f"{len(excluded)} disposal(s) excluded because cost basis, reconciliation, or tax classification is unverified."
        )
    return {
        **_response_meta(
            session, warnings=warnings, data_quality="PARTIAL" if excluded else "OK"
        ),
        "gains": [_row(r) for r in filtered[(page - 1) * page_size : page * page_size]],
        "gifts": [
            {
                "fy": g.fy,
                "scheme": g.scheme_name,
                "isin": g.isin,
                "direction": g.direction,
                "date": g.date.isoformat(),
                "units": g.units,
                "nav": g.nav,
                "value": g.value,
                "counterparty_folio": g.counterparty_folio,
            }
            for g in gift_rows[(gift_page - 1) * page_size : gift_page * page_size]
        ],
        "fys": fys,
        "selected_fy": selected_fy,
        "total": len(filtered),
        "gift_total": len(gift_rows),
        "summary": {"stcg": totals.stcg, "ltcg": totals.ltcg, "net": totals.net},
        "page": page,
        "page_size": page_size,
        "gift_page": gift_page,
    }


@app.get("/api/capital-gains/112a.csv")
def get_112a_csv(
    fy: str = Query(...),
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    holding_ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    if not re.fullmatch(r"(?:FY)?\d{4}-\d{2}", fy):
        raise HTTPException(422, "Use a financial year such as 2025-26.")
    fy = "FY" + fy.removeprefix("FY")
    if (int(fy[2:6]) + 1) % 100 != int(fy[-2:]):
        raise HTTPException(422, "Financial year must cover consecutive years.")
    try:
        csv_data = gains_service_db.generate_112a_csv(session, fy, holding_ids)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="capital-gains-112a-{fy}.csv"'
        },
    )


@app.get("/api/data-quality")
def get_data_quality(session: Session = Depends(get_session)):
    ids = _all_holding_ids(session)
    ctx = portfolio_service.build_context(session, ids, date.today())
    issues = []
    for hid in ids:
        row = portfolio_service.compute_holding_metrics(
            session, hid, date.today(), ctx, calculate_xirr=False
        )
        if row.flags:
            issues.append(
                {
                    "holding_id": hid,
                    "scheme_name": row.scheme_name,
                    "folio": row.folio,
                    "status": row.reconciliation_status,
                    "flags": [{"code": f.code, "detail": f.detail} for f in row.flags],
                }
            )
    return {
        **_response_meta(session, data_quality="PARTIAL" if issues else "OK"),
        "issues": issues,
    }


@app.get("/api/config")
def get_config(session: Session = Depends(get_session)):
    return config_service.load_config(session)


@app.post("/api/config")
def post_config(
    config: config_service.ConfigInput, session: Session = Depends(write_session)
):
    config_service.save_config(session, config.model_dump())
    return {"status": "ok"}


@app.get("/api/transactions")
def get_transactions(
    level: Optional[str] = None,
    group_name: Optional[str] = None,
    investor_name: Optional[str] = None,
    arn: Optional[str] = None,
    scheme_id: Optional[int] = None,
    types: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=250),
    session: Session = Depends(get_session),
):
    ids = _scope_holding_ids(session, level, group_name, investor_name, arn)
    query = (
        select(Transaction, Holding, Scheme, Folio)
        .join(Holding, Transaction.holding_id == Holding.holding_id)
        .join(Scheme, Holding.scheme_id == Scheme.scheme_id)
        .join(Folio, Holding.folio_id == Folio.folio_id)
    )
    if ids is not None:
        query = query.where(Holding.holding_id.in_(ids))
    option_query = select(Scheme.scheme_id, Scheme.name).join(
        Holding, Holding.scheme_id == Scheme.scheme_id
    )
    if ids is not None:
        option_query = option_query.where(Holding.holding_id.in_(ids))
    options = [
        {"scheme_id": sid, "name": name}
        for sid, name in session.execute(option_query.distinct().order_by(Scheme.name))
    ]
    if scheme_id is not None:
        query = query.where(Scheme.scheme_id == scheme_id)
    if types is not None:
        query = query.where(Transaction.type.in_(types.split(",")))
    count = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.execute(
        query.order_by(
            Transaction.date.desc(),
            Transaction.ledger_position.desc(),
            Transaction.transaction_id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    config = config_service.load_config(session)
    out = []
    for t, h, s, f in rows:
        group, investor = (
            config_service.find_owner_for_arn(config, h.advisor_arn)
            if h.advisor_arn
            else (None, None)
        )
        out.append(
            {
                "transaction_id": t.transaction_id,
                "date": str(t.date),
                "type": t.type,
                "description": t.description,
                "amount": t.amount,
                "units": t.units,
                "nav": t.nav,
                "balance": t.balance,
                "folio": f.normalized_folio,
                "scheme_name": s.name,
                "scheme_id": s.scheme_id,
                "isin": s.isin,
                "amfi": s.amfi_code,
                "advisor": h.advisor_arn,
                "advisor_label": config_service.find_arn_label(config, h.advisor_arn),
                "group_name": group,
                "investor_name": investor,
            }
        )
    return {
        **_response_meta(session),
        "transactions": out,
        "total": count,
        "page": page,
        "page_size": page_size,
        "scheme_options": options,
    }
