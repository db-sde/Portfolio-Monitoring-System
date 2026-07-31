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

import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
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
from models import CasUpload, EnrichmentCache, Folio, Holding, Scheme, Transaction

CALCULATION_VERSION = "2.0.0"  # bumped on any change to a calculation rule (spec 22)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("portfolioiq")

app = FastAPI(title="PortfolioIQ")

_default_origins = "http://localhost:5173"
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_methods=["*"], allow_headers=["*"])


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

async def _run_enrichment_task(scheme_ids: list[int]) -> None:
    """Two separate sessions/transactions on purpose: benchmark_service
    already retries internally and never raises, but if it somehow did,
    sharing one transaction with the per-scheme enrichment below would
    roll back every scheme's freshly-fetched NAV/risk data along with
    it — caught live in testing (a raised ConnectTimeout in the
    benchmark call discarded an already-successful scheme NAV
    population in the same transaction). Isolating them means a failure
    in one never costs the other its work."""
    async with httpx.AsyncClient() as client:
        with db.get_session() as session:
            await benchmark_service.refresh_nifty50_proxy_nav(session, client)
        with db.get_session() as session:
            schemes = [session.get(Scheme, sid) for sid in scheme_ids]
            schemes = [s for s in schemes if s is not None]
            await enrichment_bridge.refresh_enrichment(session, schemes)


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

    try:
        async with httpx.AsyncClient() as client:
            result = await ingestion.ingest_cas(session, client, parsed, content, investor_id=None)
    except Exception:
        logger.exception("ingest_cas failed after a successful PDF parse")
        raise HTTPException(500, "The statement parsed, but importing it failed. This is a bug — the server log has the details.")

    if result.duplicate:
        return {"status": "duplicate", "message": "This exact statement has already been imported.", "upload_id": result.upload_id}

    scheme_ids = list({
        session.get(Holding, h.holding_id).scheme_id for h in result.holdings
    })
    background_tasks.add_task(_run_enrichment_task, scheme_ids)

    active = sum(1 for h in result.holdings if h.status != "review_required")
    return {
        "status": "ok",
        "investor_name": parsed.investor_info.name,
        "statement_period": {"from": parsed.statement_period.from_, "to": parsed.statement_period.to},
        "total_holdings": len(result.holdings),
        "holdings_needing_review": sum(1 for h in result.holdings if h.status != "reconciled"),
        "warnings": result.warnings,
        "holding_notes": [{"scheme_name": h.scheme_name, "status": h.status, "detail": h.detail} for h in result.holdings],
    }


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

    # Multiple statements can now accumulate (that's the whole point of
    # the Neon migration — no single upload overwrites the last one), so
    # there's no longer one CAS-embedded "investor_info.name" to show in
    # the header. investor_names lists everyone with at least one holding
    # in scope, from config.json's own investor labels.
    investor_names = sorted({
        config_service.find_owner_for_arn(config, m.advisor_arn)[1]
        for m in all_metrics if m.advisor_arn
    } - {None})

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
def get_exposure(session: Session = Depends(get_session)):
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
    total = session.execute(select(Scheme.scheme_id)).scalars().all()
    enriched = session.execute(select(EnrichmentCache.scheme_id).where(EnrichmentCache.status == "ok")).scalars().all()
    last_run = session.execute(select(EnrichmentCache.fetched_at).order_by(EnrichmentCache.fetched_at.desc()).limit(1)).scalar_one_or_none()
    return {
        "total_schemes": len(total), "enriched": len(set(enriched)),
        "failed": len(set(total) - set(enriched)), "pending": 0,
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
    )
    for model in (
        DisposalAllocation, PurchaseLot, Transaction, Holding, SchemeAlias, Scheme, Folio, CasUpload,
        NavCache, EnrichmentCache, BenchmarkPoint, BenchmarkDefinition,
        ConfigInvestorArn, ConfigInvestor, ConfigGroup, Preference,
    ):
        session.query(model).delete()
    return {"status": "ok", "message": "All statements, holdings, gains, config, and cached market data deleted."}
