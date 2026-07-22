"""
PortfolioIQ — enrichment.py

Enriches a scheme (by AMFI code / ISIN / name) with fund-level analytics:
AUM, cap allocation, benchmark, category, expense ratio, fund manager,
trailing returns, risk ratios, and NAV history.

Source priority (spec section 7):
  1. mfdata.in  - primary; the only source with cap-allocation % and risk
                  ratios (sharpe/alpha/beta/std dev).
  2. mfapi.in   - fallback for NAV history + basic scheme metadata.
  3. captnemo   - fallback for category + scheme rules, by ISIN.

IMPORTANT CAVEAT: while building this, mfdata.in was unreachable from
every network path tried (direct request, docs page, different User-
Agents) — not a 404 or a clean error, a flat connection failure/403
consistent with bot-protection on datacenter IPs. mfapi.in and captnemo
were both reachable and worked as documented. Since this app's own spec
has the backend running on your own machine (with an optional ngrok
tunnel) rather than a cloud host, mfdata.in may well work fine for you —
residential IPs are usually not what that kind of protection targets —
but it could not be verified here, and the exact field names below are
taken from the spec's own documented schema (section 7), not from a
response this code actually saw. If your real responses use different
field names, `_extract_mfdata_fields` is the one place to adjust; every
field defaults to None rather than raising, so a schema mismatch just
means less enrichment, not a crash.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

CACHE_TTL_HOURS = float(os.environ.get("CACHE_TTL_HOURS", "24"))
MFDATA_BASE = "https://mfdata.in/api/v1"
MFAPI_BASE = "https://api.mfapi.in/mf"
CAPTNEMO_BASE = "https://mf.captnemo.in/kuvera"
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

ENRICHED_FIELD_DEFAULTS = {
    "corpus_cr": None, "largecap_pct": None, "midcap_pct": None, "smallcap_pct": None,
    "benchmark": None, "category": None, "expense_ratio": None, "fund_manager": None,
    "returns": {"1m": None, "3m": None, "6m": None, "1y": None, "2y": None, "3y": None},
    "risk": {"std_dev": None, "sharpe": None, "alpha": None, "beta": None},
}


# ---------------------------------------------------------------- cache ----

_DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / "enrichment_cache.json"


def _cache_path() -> Path:
    # See config_manager._config_path — same cwd-independence reasoning.
    return Path(os.environ.get("CACHE_PATH", str(_DEFAULT_CACHE_PATH)))


def _load_cache() -> dict:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cache, fp, indent=2, ensure_ascii=False)


def _is_fresh(entry: dict) -> bool:
    try:
        cached_at = datetime.fromisoformat(entry["cached_at"])
    except (KeyError, ValueError, TypeError):
        return False
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    ttl = entry.get("ttl_hours", CACHE_TTL_HOURS)
    return datetime.now(timezone.utc) - cached_at < timedelta(hours=ttl)


# ------------------------------------------------------------ fetchers ----

async def _fetch_json(client: httpx.AsyncClient, url: str, **kwargs) -> Optional[Any]:
    try:
        resp = await client.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_mfdata_fields(raw: dict) -> dict:
    """Field names per spec section 7. See module docstring: unverified
    against a live response, defensive .get() throughout."""
    alloc = raw.get("cap_allocation") or raw
    ratios = raw.get("risk") or raw.get("ratios") or {}
    returns = raw.get("returns") or {}
    return {
        "corpus_cr": _num(raw.get("corpus_cr") or raw.get("aum_cr") or raw.get("aum")),
        "largecap_pct": _num(alloc.get("largecap_pct") or alloc.get("large_cap_pct")),
        "midcap_pct": _num(alloc.get("midcap_pct") or alloc.get("mid_cap_pct")),
        "smallcap_pct": _num(alloc.get("smallcap_pct") or alloc.get("small_cap_pct")),
        "benchmark": raw.get("benchmark") or raw.get("benchmark_name"),
        "category": raw.get("category") or raw.get("scheme_category"),
        "expense_ratio": _num(raw.get("expense_ratio")),
        "fund_manager": raw.get("fund_manager") or raw.get("manager"),
        "returns": {
            period: _num(returns.get(period))
            for period in ("1m", "3m", "6m", "1y", "2y", "3y")
        },
        "risk": {
            "std_dev": _num(ratios.get("std_dev") or ratios.get("standard_deviation")),
            "sharpe": _num(ratios.get("sharpe") or ratios.get("sharpe_ratio")),
            "alpha": _num(ratios.get("alpha")),
            "beta": _num(ratios.get("beta")),
        },
    }


def _extract_mfapi_nav_history(raw: dict) -> list[dict]:
    """mfapi.in dates are DD-MM-YYYY; normalise to ISO for the rest of
    the app (calculations.py expects YYYY-MM-DD / DD-MM-YYYY / DD-Mon-YYYY,
    all of which _parse_date already handles, so this is mostly passthrough
    plus dropping unparseable rows)."""
    out = []
    for row in raw.get("data", []):
        nav = _num(row.get("nav"))
        date_str = row.get("date")
        if nav is not None and date_str:
            out.append({"date": date_str, "nav": nav})
    return out


async def _enrich_one(client: httpx.AsyncClient, amfi_code: str, isin: Optional[str], scheme_name: str) -> dict:
    sources_used = []

    mfdata_raw = await _fetch_json(client, f"{MFDATA_BASE}/schemes/{amfi_code}")
    fields = dict(ENRICHED_FIELD_DEFAULTS)
    fields["returns"] = dict(ENRICHED_FIELD_DEFAULTS["returns"])
    fields["risk"] = dict(ENRICHED_FIELD_DEFAULTS["risk"])
    if mfdata_raw:
        fields.update(_extract_mfdata_fields(mfdata_raw))
        sources_used.append("mfdata.in")

    mfapi_raw = await _fetch_json(client, f"{MFAPI_BASE}/{amfi_code}")
    nav_history: list[dict] = []
    if mfapi_raw:
        meta = mfapi_raw.get("meta", {})
        if not fields.get("category"):
            fields["category"] = meta.get("scheme_category")
        nav_history = _extract_mfapi_nav_history(mfapi_raw)
        sources_used.append("mfapi.in")

    if not fields.get("category") and isin:
        cn_raw = await _fetch_json(client, f"{CAPTNEMO_BASE}/{isin}", follow_redirects=True)
        cn_entry = cn_raw[0] if isinstance(cn_raw, list) and cn_raw else cn_raw
        if isinstance(cn_entry, dict):
            fields["category"] = fields.get("category") or cn_entry.get("category")
            fields["expense_ratio"] = fields.get("expense_ratio") or _num(cn_entry.get("expense_ratio"))
            fields["corpus_cr"] = fields.get("corpus_cr") or _num(cn_entry.get("aum"))
            sources_used.append("captnemo")

    fields["enriched_at"] = datetime.now(timezone.utc).isoformat()
    fields["enrichment_source"] = "+".join(sources_used) if sources_used else "failed"
    fields["_nav_history"] = nav_history  # not part of the public `enriched` shape; consumed by portfolio.py
    return fields


async def enrich_schemes(schemes: list[dict]) -> dict[str, dict]:
    """schemes: list of {"amfi": str, "isin": str, "scheme": str}. Returns
    {amfi_code: enriched_fields_dict}, using the on-disk cache wherever
    it's still fresh, and updating it with anything newly fetched."""
    cache = _load_cache()
    results: dict[str, dict] = {}
    to_fetch = []

    for scheme in schemes:
        amfi = scheme.get("amfi")
        if not amfi:
            continue
        entry = cache.get(amfi)
        if entry and _is_fresh(entry):
            results[amfi] = entry["data"]
        else:
            to_fetch.append(scheme)

    if to_fetch:
        async with httpx.AsyncClient() as client:
            fetched = await asyncio.gather(*[
                _enrich_one(client, s["amfi"], s.get("isin"), s.get("scheme", ""))
                for s in to_fetch
            ])
        for scheme, data in zip(to_fetch, fetched):
            amfi = scheme["amfi"]
            results[amfi] = data
            cache[amfi] = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "ttl_hours": CACHE_TTL_HOURS,
                "data": data,
            }
        _save_cache(cache)

    return results
