"""
PortfolioIQ — enrichment.py

Enriches a scheme (by AMFI code / ISIN / name) with fund-level analytics:
AUM, cap allocation, benchmark, category, expense ratio, fund manager,
trailing returns, risk ratios, and NAV history.

Source priority (spec section 7), re-verified live and adjusted below:
  1. mfdata.in  - would be primary (the only source with cap-allocation %
                  and sharpe/alpha/beta), but see the caveat below: it is
                  not just "occasionally down", it is actively
                  unreachable, so nothing here depends on it succeeding.
  2. mfapi.in   - NAV history + basic scheme metadata, AND (since mfdata
                  never returns the pre-computed returns the spec expects)
                  the source of every trailing-return figure this module
                  produces: computed here directly from the NAV history,
                  point-to-point for <1y windows and CAGR for 1y/2y/3y —
                  see RETURN_PERIODS / _compute_trailing_returns.
  3. captnemo   - queried unconditionally (by ISIN), not just as a
                  category fallback: it actually carries expense ratio,
                  fund manager(s), and a volatility figure per scheme, on
                  top of category. Its own AUM field is deliberately not
                  used — see the corpus_cr comment in _enrich_one.

IMPORTANT CAVEAT, confirmed with a live connectivity test (not just
inference from failed requests): mfdata.in's DNS resolves fine (it's
behind Cloudflare) and a TCP+TLS handshake gets partway through — client
hello, server hello, certificate — before going silent, no response, no
clean TLS alert. That pattern (as opposed to a fast 403) is consistent
with fingerprint-based bot mitigation (e.g. JA3) rather than a simple
IP block, which means retrying with different headers/User-Agents won't
help, and it will almost certainly behave the same from Render's
datacenter IPs as it did from this sandbox. Cap-allocation % and
sharpe/alpha/beta specifically have NO other free source currently wired
in here — mfapi.in doesn't have them and neither does captnemo — so
those fields staying null is an honest gap, not a bug; getting them for
real would need a paid data vendor. Everything else this module claims
to provide (returns, expense ratio, fund manager, category, a volatility
figure standing in for std_dev) now comes from mfapi.in/captnemo, both
confirmed reachable, and works regardless of mfdata.in's availability.
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
    plus dropping unparseable rows). Sorted oldest-first so
    _compute_trailing_returns can treat the last entry as "latest" — by
    *parsed* date, not the raw DD-MM-YYYY string: e.g. "05-01-2024" sorts
    before "28-06-2020" lexicographically even though it's nearly 4 years
    later, which would have silently pinned every trailing-return
    calculation to whatever date happened to have the lexicographically
    largest string (in practice, the latest 31-Dec in the whole history)
    instead of the actual most recent NAV.
    """
    out = []
    for row in raw.get("data", []):
        nav = _num(row.get("nav"))
        date_str = row.get("date")
        if nav is not None and date_str and _parse_nav_date(date_str) is not None:
            out.append({"date": date_str, "nav": nav})
    out.sort(key=lambda r: _parse_nav_date(r["date"]))
    return out


# Period -> (days-back, whether to annualise as CAGR). Point-to-point %
# for sub-1-year windows, CAGR for 1y+, matching the convention every
# mainstream fund tracker (Value Research, Groww, ET Money) uses — a raw
# 3-year point-to-point % would read as roughly 3x too big next to a 1y
# figure on the same table.
RETURN_PERIODS: dict[str, tuple[int, bool]] = {
    "1m": (30, False), "3m": (91, False), "6m": (182, False),
    "1y": (365, True), "2y": (730, True), "3y": (1095, True),
}


def _parse_nav_date(s: str):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _nav_on_or_before(nav_history: list[dict], target) -> Optional[tuple[Any, float]]:
    """Latest (date, nav) at or before the `target` date object."""
    best = None
    for row in nav_history:
        d = _parse_nav_date(row["date"])
        if d is not None and d <= target and (best is None or d > best[0]):
            best = (d, row["nav"])
    return best


def _compute_trailing_returns(nav_history: list[dict]) -> dict[str, Optional[float]]:
    """Real trailing returns computed directly from mfapi.in's own NAV
    history — reachable and correct, unlike mfdata.in (see module
    docstring) or trying to reuse a third party's own return figures
    which may use different period boundaries or rounding."""
    out: dict[str, Optional[float]] = {k: None for k in RETURN_PERIODS}
    if not nav_history:
        return out
    latest = nav_history[-1]
    latest_date = _parse_nav_date(latest["date"])
    if latest_date is None or not latest["nav"]:
        return out
    for period, (days_back, annualize) in RETURN_PERIODS.items():
        target = latest_date - timedelta(days=days_back)
        past = _nav_on_or_before(nav_history, target)
        if not past or not past[1]:
            continue
        past_date, past_nav = past
        actual_days = (latest_date - past_date).days
        if actual_days <= 0:
            continue
        ratio = latest["nav"] / past_nav
        if annualize:
            out[period] = round((ratio ** (365.0 / actual_days) - 1) * 100, 2)
        else:
            out[period] = round((ratio - 1) * 100, 2)
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
        # mfdata.in is the only source with pre-computed returns and it is
        # unreachable in practice (see module docstring) — compute our own
        # from the NAV history we just fetched rather than leave every
        # fund summary row blank.
        if not any(fields["returns"].values()):
            fields["returns"] = _compute_trailing_returns(nav_history)

    # captnemo (Kuvera's backing API) turns out to carry real per-scheme
    # analytics keyed by ISIN — category, expense ratio, fund manager(s),
    # and a volatility figure — that this module previously only used as
    # a last-resort category fallback. Queried unconditionally (not just
    # when mfdata failed) since mfdata practically never succeeds. AUM is
    # deliberately NOT taken from here: its units couldn't be confirmed
    # against a known fund's real AUM, and a wrong number dressed up as
    # "corpus_cr" is worse than a blank field.
    if isin:
        cn_raw = await _fetch_json(client, f"{CAPTNEMO_BASE}/{isin}", follow_redirects=True)
        cn_entry = cn_raw[0] if isinstance(cn_raw, list) and cn_raw else cn_raw
        if isinstance(cn_entry, dict):
            fields["category"] = fields.get("category") or cn_entry.get("fund_category") or cn_entry.get("category")
            fields["expense_ratio"] = fields.get("expense_ratio") or _num(cn_entry.get("expense_ratio"))
            fields["fund_manager"] = fields.get("fund_manager") or cn_entry.get("fund_manager")
            if fields["risk"].get("std_dev") is None:
                fields["risk"]["std_dev"] = _num(cn_entry.get("volatility"))
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
