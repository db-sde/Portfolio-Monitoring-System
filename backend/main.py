"""
PortfolioIQ backend — FastAPI app, all route definitions (spec section 6).

Runs locally (optionally behind an ngrok tunnel) rather than on a cloud
host, per the spec's own deployment target — see README for why, and
enrichment.py's docstring for the reachability caveat that decision is
partly driven by.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from casparser import read_cas_pdf
from casparser.exceptions import CASParseError, ParserException
from casparser.types import NSDLCASData

import calculations as calc
import config_manager as cfgm
import enrichment
import gains_service
import portfolio as pf

app = FastAPI(title="PortfolioIQ")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB — real CAS PDFs are a few MB at most

_default_origins = "http://localhost:5173"
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory only — a restart re-triggers enrichment on startup (mostly
# cache hits, so it's quick) rather than persisting this separately from
# enrichment_cache.json itself.
_enrichment_map: dict[str, dict] = {}
_enrichment_state: dict[str, Any] = {
    "total_schemes": 0, "enriched": 0, "failed": 0, "pending": 0, "last_run": None,
}


def _config() -> dict:
    return cfgm.load_config()


def _require_cas_data() -> dict:
    cas_data = pf.load_cas_data()
    if cas_data is None:
        raise HTTPException(404, "No CAS data uploaded yet — POST a file to /api/upload-cas first.")
    return cas_data


def _records() -> list[dict]:
    cas_data = _require_cas_data()
    return pf.build_scheme_records(cas_data, _config(), _enrichment_map)


def _public_scheme(r: dict) -> dict:
    d = dict(r)
    d.pop("_nav_history", None)
    return d


async def _run_enrichment() -> None:
    global _enrichment_map
    cas_data = pf.load_cas_data()
    if cas_data is None:
        return
    targets = pf.enrichment_targets(cas_data)
    _enrichment_state.update({
        "total_schemes": len(targets), "enriched": 0, "failed": 0, "pending": len(targets),
    })
    _enrichment_map = await enrichment.enrich_schemes(targets)
    enriched_count = sum(1 for d in _enrichment_map.values() if d.get("enrichment_source") != "failed")
    _enrichment_state.update({
        "enriched": enriched_count,
        "failed": len(_enrichment_map) - enriched_count,
        "pending": 0,
        "last_run": datetime.now(timezone.utc).isoformat(),
    })


@app.on_event("startup")
async def _on_startup() -> None:
    # Re-populate in-memory enrichment state from cas_data.json + the
    # on-disk cache on every restart, so /api/portfolio isn't blank right
    # after a server restart just because it's a fresh process.
    if pf.load_cas_data() is not None:
        await _run_enrichment()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/upload-cas")
async def upload_cas(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    password: str = Form(default=""),
):
    """Accepts the CAS PDF directly (the primary, intended flow — this is
    the "just upload your statement" experience) and parses it in-process
    via casparser, the same library casparser-web wraps. A pre-parsed CAS
    JSON is still accepted too (useful for testing, or if you already have
    one), detected purely by file extension."""
    filename = (file.filename or "").lower()
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That file is larger than we accept (20MB max).")

    if filename.endswith(".pdf"):
        with tempfile.TemporaryDirectory(prefix="portfolioiq-") as tmp:
            pdf_path = Path(tmp) / "statement.pdf"
            pdf_path.write_bytes(content)
            del content  # don't keep a second copy of the statement in memory
            try:
                parsed = await run_in_threadpool(read_cas_pdf, str(pdf_path), password)
            except CASParseError as exc:
                message = str(exc)
                if "password" in message.lower():
                    raise HTTPException(401, "That password didn't work. Double check it and try again.")
                raise HTTPException(
                    422,
                    "We couldn't read this as a CAS statement. Make sure it's the unmodified "
                    "PDF from CAMS or KFintech.",
                )
            except ParserException:
                raise HTTPException(422, "This statement couldn't be parsed.")

        if isinstance(parsed, NSDLCASData):
            raise HTTPException(
                422,
                "This looks like an NSDL/CDSL demat statement. PortfolioIQ currently analyses "
                "CAMS/KFintech mutual-fund statements only — casparser-web can still parse this "
                "one for you, just not with portfolio analytics on top.",
            )
        cas_data = parsed.model_dump(mode="json", by_alias=True)
        gains_error = gains_service.compute_and_save_gains(parsed, cas_data)
    elif filename.endswith(".json"):
        try:
            cas_data = json.loads(content)
        except json.JSONDecodeError:
            raise HTTPException(400, "That doesn't look like valid JSON.")
        # No live pydantic object to compute gains from, and any gains
        # persisted from a previous PDF upload would now describe the
        # wrong statement.
        gains_service.clear_gains()
        gains_error = None
    else:
        raise HTTPException(400, "Upload the CAS PDF from CAMS/KFintech (or a previously-parsed CAS JSON file).")

    pf.save_cas_data(cas_data)
    records = pf.build_scheme_records(cas_data, _config(), {})
    active = sum(1 for r in records if r["current_value"] > 0)
    advisors = sorted({r["advisor"] for r in records if r.get("advisor")})

    background_tasks.add_task(_run_enrichment)

    return {
        "status": "ok",
        "investor_name": (cas_data.get("investor_info") or {}).get("name"),
        "statement_period": cas_data.get("statement_period"),
        "total_schemes": len(records),
        "active_schemes": active,
        "zero_value_schemes": len(records) - active,
        "advisors_detected": advisors,
        "gains_error": gains_error,
    }


@app.get("/api/portfolio")
def get_portfolio(
    include_zero_value: bool = Query(False),
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
):
    cas_data = _require_cas_data()
    filtered = pf.filter_schemes(_records(), include_zero_value, level, group_name, investor_name, arn)
    return {
        "investor_info": {"name": (cas_data.get("investor_info") or {}).get("name")},
        "statement_period": cas_data.get("statement_period"),
        "last_enriched": _enrichment_state["last_run"],
        "schemes": [_public_scheme(r) for r in filtered],
    }


@app.get("/api/portfolio/snapshot")
def get_snapshot(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
):
    _require_cas_data()
    filtered = pf.filter_schemes(_records(), True, level, group_name, investor_name, arn)
    end = end_date or datetime.now().date().isoformat()
    nav_history_by_amfi = {r["amfi"]: r["_nav_history"] for r in filtered if r.get("amfi")}

    return {
        "given_period": calc.calculate_snapshot(filtered, start_date, end, nav_history_by_amfi),
        "since_inception": calc.calculate_snapshot(filtered, None, end, nav_history_by_amfi),
    }


@app.get("/api/portfolio/summary")
def get_portfolio_summary():
    _require_cas_data()
    return pf.build_portfolio_summary(_records(), _config())


@app.get("/api/portfolio/fund-summary")
def get_fund_summary(
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
    include_zero_value: bool = Query(False),
):
    _require_cas_data()
    filtered = pf.filter_schemes(_records(), include_zero_value, level, group_name, investor_name, arn)
    return pf.build_fund_summary(filtered)


@app.get("/api/portfolio/exposure")
def get_exposure():
    _require_cas_data()
    return pf.build_exposure(_records())


@app.get("/api/transactions")
def get_transactions(
    include_zero_value: bool = Query(True),
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
):
    # Zero-value (fully redeemed) schemes default to included here — their
    # transaction history is still real and worth seeing, unlike the
    # current-holdings views where they'd just clutter a "what do I own
    # right now" table.
    _require_cas_data()
    filtered = pf.filter_schemes(_records(), include_zero_value, level, group_name, investor_name, arn)
    return {"transactions": pf.build_transactions(filtered)}


def _gain_matches(entry: dict, config: dict, level, group_name, investor_name, arn) -> bool:
    if level in (None, ""):
        return True
    advisor = entry.get("advisor")
    if level == "arn":
        return advisor == arn
    entry_group, entry_investor = cfgm.find_owner_for_arn(config, advisor) if advisor else (None, None)
    if level == "group":
        return entry_group == group_name
    if level == "investor":
        return entry_investor == investor_name
    return True


@app.get("/api/capital-gains")
def get_capital_gains(
    level: Optional[str] = Query(None),
    group_name: Optional[str] = Query(None),
    investor_name: Optional[str] = Query(None),
    arn: Optional[str] = Query(None),
):
    _require_cas_data()
    stored = gains_service.load_gains()
    if stored is None:
        return {"gains": [], "gifts": [], "gains_error": None, "fys": []}

    config = _config()
    gains = [g for g in stored["gains"] if _gain_matches(g, config, level, group_name, investor_name, arn)]
    gifts = [g for g in stored["gifts"] if _gain_matches(g, config, level, group_name, investor_name, arn)]
    for g in gains:
        g["advisor_label"] = cfgm.find_arn_label(config, g.get("advisor")) if g.get("advisor") else None
    fys = sorted({g["fy"] for g in gains}, reverse=True)
    return {"gains": gains, "gifts": gifts, "gains_error": stored.get("gains_error"), "fys": fys}


@app.get("/api/config")
def get_config():
    return _config()


@app.post("/api/config")
def post_config(config: dict):
    cfgm.save_config(config)
    return {"status": "ok"}


@app.get("/api/enrich/status")
def get_enrich_status():
    return _enrichment_state
