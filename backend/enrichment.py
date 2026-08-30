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
                  see RETURN_DAY_PERIODS/RETURN_YEAR_PERIODS / _compute_trailing_returns.
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
datacenter IPs as it did from this sandbox. Re-confirmed again later via
a real browser load: Cloudflare itself returns error 522 ("connection
timed out") — the origin behind Cloudflare is down, not just blocking
bots, so this isn't expected to self-resolve on retry.

Cap-allocation % is still an honest gap — checked directly against
captnemo/Kuvera's own documented OpenAPI schema for fund_schemes.json,
no cap/sector/holdings field exists there either, and no other free
source has it. Getting real cap-allocation would need actual portfolio
holdings (stock-by-stock), which no free API publishes — the only path
would be scraping each AMC's own monthly SEBI-mandated portfolio
disclosure (~40 different sites/formats) or a paid data vendor.

Sharpe/sortino/max-drawdown/volatility/alpha/beta are NOT a gap, as of
this revision: fundlens.lovable.app (an independent NAV-analytics site,
confirmed via its own footer credit to run entirely off mfapi.in) had
its displayed risk figures for two real funds reproduced closely by
computing them straight from NAV history mfapi.in already gives us for
free — see _compute_risk_ratios. Alpha/beta specifically need a
benchmark *index* return series, which no free source publishes raw
either (checked directly against Kuvera's own OpenAPI spec — no index
data field there survives to a public, no-auth endpoint); the workaround
is BENCHMARK_AMFI_CODE, a NIFTY 50 index *fund*'s own NAV history via
the same mfapi.in call already used for everything else — the same trick
fundlens.lovable.app itself offers as a selectable benchmark. Everything
this module provides (returns, expense ratio, fund manager, category,
risk ratios) now comes from mfapi.in/captnemo, both confirmed reachable,
and works regardless of mfdata.in's availability.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger("portfolioiq")

CACHE_TTL_HOURS = float(os.environ.get("CACHE_TTL_HOURS", "24"))
MFDATA_BASE = "https://mfdata.in/api/v1"
MFAPI_BASE = "https://api.mfapi.in/mf"
CAPTNEMO_BASE = "https://mf.captnemo.in/kuvera"
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
# mfdata.in doesn't fail fast — it holds the connection open and goes
# silent rather than erroring (see module docstring: Cloudflare 522,
# origin down), so REQUEST_TIMEOUT's normal 10s is spent waiting on a
# call that has never once succeeded, for every single scheme, on every
# enrichment run. A short, dedicated timeout here just means giving up
# sooner on something already confirmed dead, not treating it as gone
# for good — if mfdata.in ever comes back, a quick successful response
# is still well within 3s.
MFDATA_TIMEOUT = httpx.Timeout(3.0, connect=2.0)
# ...and now not called at all by default. Re-confirmed live from this
# machine, three different scheme codes, every one of them failing to
# connect at all (not a 4xx/5xx — no TCP connection established, the
# full timeout burned each time). It has never once returned data in
# this app's history. Left behind an env flag rather than deleted
# outright: the extraction/merge code for it is written and correct, so
# if the host ever comes back this is a one-variable change rather than
# a rewrite — but it should not cost every scheme on every run a
# multi-second wait for a host confirmed dead. That wait was pure,
# unconditional latency on the critical path of the slowest operation
# in the app.
MFDATA_ENABLED = os.environ.get("MFDATA_ENABLED", "").lower() in ("1", "true", "yes")
# _fetch_json_retrying's own docstring already documented this exact
# failure mode (mfapi.in/captnemo "intermittently fail... when hit
# concurrently for several schemes at once") but only mitigated it with
# retries, not a concurrency limit — reproduced live on a real 54-scheme
# batch: 22 completely ordinary, well-known funds (HDFC Large Cap,
# Aditya Birla Sun Life Flexi Cap, ...) failed BOTH mfapi.in and
# captnemo simultaneously in the same run, while a same-day retry of
# just 2-3 schemes succeeded cleanly every time. Firing all N schemes'
# requests via one unbounded asyncio.gather (up to 3 external hosts
# each, so 3*N simultaneous outbound connections for a 54-scheme
# portfolio) is what a 3-retry policy alone can't fix, since retrying
# into the same overloaded burst just fails again. Capping how many
# schemes are in flight at once is the actual fix.
ENRICH_CONCURRENCY_LIMIT = 8

# Risk-free rate for Sharpe/Sortino. 6% reproduces fundlens.lovable.app's
# displayed Sharpe/Sortino for a real fund (#122639) almost to the second
# decimal place — verified against its live figures, not just picked as a
# plausible G-Sec proxy. Configurable since it's a moving target regardless.
RISK_FREE_RATE = float(os.environ.get("RISK_FREE_RATE_PCT", "6.0")) / 100
TRADING_DAYS_PER_YEAR = 252
# Sharpe/Sortino/volatility use a trailing ~3y window (a flat day count
# is fine here — unlike RETURN_YEAR_PERIODS below, this is an internal
# window size, not a return figure being compared against another
# tracker's exact-calendar-date convention) — long enough to smooth out
# noise, short enough to reflect the fund's current risk profile rather
# than its whole history. Max drawdown deliberately does NOT use this
# window (see _max_drawdown_pct) — a fund's worst-ever decline is the
# useful figure.
RISK_RATIO_WINDOW_DAYS = 1095
MIN_RISK_RATIO_DAYS = 30  # below this, an "annualised" figure is just noise

# Alpha/beta need a benchmark *index* return series, and no free source
# here (mfapi.in, captnemo/Kuvera's own documented fund_schemes response —
# checked directly against its OpenAPI spec, no index/holdings field
# exists) actually publishes raw NIFTY index values for free. The
# workaround, same one fundlens.lovable.app itself offers as a selectable
# benchmark ("Nifty 50 TRI (UTI Nifty 50 Index Direct)"): a NIFTY 50
# *index fund* is itself just another AMFI scheme, so its own NAV history
# is available through the exact same mfapi.in call already used for
# every other fund — no new integration, no new failure mode. It's a
# proxy (tracking error, TER drag) rather than the raw index, but it's
# the only free option that actually exists, and it's the same
# proxy a working reference site relies on. UTI's Direct plan chosen for
# its long history (back to 2013), giving a real trailing window even
# for schemes needing a full 3y lookback.
BENCHMARK_AMFI_CODE = "120716"  # UTI Nifty 50 Index Fund - Direct - Growth
BENCHMARK_LABEL = "Nifty 50 (via UTI Nifty 50 Index Fund - Direct Growth)"

# AMC mergers/renames (HSBC absorbing L&T's schemes in Nov 2022 is a
# confirmed real example — verified live: AMFI code 120069 froze at its
# Nov-2022 NAV while the same fund kept trading under new code 151130)
# routinely leave the OLD AMFI code sitting in mfapi.in's database,
# still returning 200 with real-looking history, just permanently frozen
# on whatever date the recode happened. A CAS statement's own embedded
# AMFI code can point at that frozen code, silently pinning every
# "current" return/risk figure to a multi-year-old snapshot instead of
# today. STALE_NAV_DAYS is deliberately generous (a long weekend plus one
# holiday is ~4 days) so this only fires for a code that's genuinely gone
# quiet, not one that's merely a few days behind a slow mfapi.in update.
STALE_NAV_DAYS = 10
MAX_ALTERNATE_CANDIDATES = 10

ENRICHED_FIELD_DEFAULTS = {
    "corpus_cr": None, "largecap_pct": None, "midcap_pct": None, "smallcap_pct": None,
    "benchmark": None, "category": None, "expense_ratio": None, "fund_manager": None,
    "nav_as_of": None,
    "returns": {"1m": None, "3m": None, "6m": None, "1y": None, "2y": None, "3y": None},
    "risk": {"std_dev": None, "sharpe": None, "sortino": None, "max_drawdown": None, "alpha": None, "beta": None},
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

async def _fetch_json_result(
    client: httpx.AsyncClient, url: str, timeout: httpx.Timeout = REQUEST_TIMEOUT, **kwargs
) -> tuple[Optional[Any], bool, float]:
    """Returns (data, retryable, retry_after_seconds).

    Classifying the failure matters — a bare "returned None" conflates
    three completely different situations that need opposite handling,
    and treating them identically is what made this module's failures
    look random. Measured directly against mfapi.in: under sustained
    load it degrades 200 -> 502 -> refusing TCP connections outright,
    then stays blocked for ~225 SECONDS before recovering. A 404 (this
    scheme code genuinely doesn't exist there) must NOT be retried at
    all; a 429/5xx must be retried, but only after backing off long
    enough to be worth it. The old code retried both identically, 3
    times, 1 second apart — which for a rate-limited host is just three
    more requests into the wall that blocked us."""
    try:
        resp = await client.get(url, timeout=timeout, **kwargs)
        if resp.status_code == 200:
            return resp.json(), False, 0.0
        # Retry-After is the server telling us exactly how long to wait —
        # honouring it is strictly better than any backoff we'd guess.
        retry_after = 0.0
        raw_retry_after = resp.headers.get("retry-after")
        if raw_retry_after:
            try:
                retry_after = float(raw_retry_after)
            except ValueError:
                retry_after = 0.0
        retryable = resp.status_code == 429 or resp.status_code >= 500
        return None, retryable, retry_after
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
        # Transport-level failure — a timeout or a refused/dropped
        # connection is exactly what this host does when it's throttling
        # us, so it's retryable rather than a verdict about the URL.
        return None, True, 0.0
    except (httpx.HTTPError, ValueError):
        # Anything else (including a 200 whose body isn't valid JSON):
        # not obviously transient, don't hammer it.
        return None, False, 0.0


async def _fetch_json(
    client: httpx.AsyncClient, url: str, timeout: httpx.Timeout = REQUEST_TIMEOUT, **kwargs
) -> Optional[Any]:
    data, _retryable, _retry_after = await _fetch_json_result(client, url, timeout=timeout, **kwargs)
    return data


FETCH_RETRY_ATTEMPTS = 3
FETCH_RETRY_BASE_DELAY_SECONDS = 1.0
FETCH_RETRY_MAX_DELAY_SECONDS = 8.0


async def _fetch_json_retrying(client: httpx.AsyncClient, url: str, **kwargs) -> Optional[Any]:
    """Like _fetch_json, but retries transient failures with exponential
    backoff and jitter. For mfapi.in and captnemo specifically — both
    confirmed reachable in general, but measured live to rate-limit hard
    under a whole portfolio's worth of concurrent requests (see
    _fetch_json_result: 502s, then refused connections, then a ~225s
    lockout).

    Both halves of "exponential + jitter" are load-bearing here, for
    different reasons. Exponential: a fixed 1s delay is far too short
    for a host that stays angry for minutes, so all three attempts
    burned inside the same failure window and the call failed anyway.
    Jitter: without it, N schemes that failed together in the same
    concurrent batch all sleep the same 1s and then retry at the same
    instant — the retry burst is as synchronised as the burst that
    caused the throttling, which is what turns one transient blip into
    a batch-wide failure. Randomising each waiter's delay spreads them.

    Deliberately NOT used for the mfdata.in probe in _enrich_one: that
    host is confirmed permanently down (see module docstring), so
    retrying it would just add latency for a call that never succeeds."""
    for attempt in range(FETCH_RETRY_ATTEMPTS):
        data, retryable, retry_after = await _fetch_json_result(client, url, **kwargs)
        if data is not None:
            return data
        if not retryable or attempt == FETCH_RETRY_ATTEMPTS - 1:
            return None
        delay = retry_after or min(
            FETCH_RETRY_BASE_DELAY_SECONDS * (2 ** attempt), FETCH_RETRY_MAX_DELAY_SECONDS
        )
        await asyncio.sleep(delay * (0.5 + random.random()))
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


# Sub-year periods: a fixed day-count approximation, since "1 month" /
# "3 months" / "6 months" doesn't have one true length anyway — this
# already matches every reference tracker checked (cleartax.in, exact to
# the basis point for 1m/3m/6m/1y on two different funds).
RETURN_DAY_PERIODS: dict[str, int] = {"1m": 30, "3m": 91, "6m": 182}
# Year periods: anchored to the *exact same calendar date* N years back,
# not a fixed days count (e.g. 3*365=1095) — those aren't the same thing
# whenever a leap day falls inside the window. Verified live against
# cleartax.in: a 1095-day offset was landing one day later than "3 years
# ago today" whenever Feb 29 fell in between (confirmed for both SBI
# Contra and Nippon India Power & Infra, ~0.2pp off in the direction and
# magnitude the leap-day date-selection alone predicts) — a difference
# invisible at 1y (no leap day in a recent 1-year window) but real at
# 2y/3y, since CAGR's exponent amplifies a shifted start date more the
# longer and larger the compounded return is.
RETURN_YEAR_PERIODS: dict[str, int] = {"1y": 1, "2y": 2, "3y": 3}


def _years_back(d, years: int):
    """Same calendar month/day, `years` years earlier — the convention
    every mainstream tracker (Value Research, Groww, ET Money) actually
    anchors "1Y"/"3Y" returns to, not a fixed days count."""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        # d is Feb 29 and (d.year - years) isn't a leap year — Feb 28 is
        # the standard equivalent for this edge case.
        return d.replace(month=2, day=28, year=d.year - years)


def _parse_nav_date(s: str):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_stale(latest_date_str: Optional[str]) -> bool:
    d = _parse_nav_date(latest_date_str) if latest_date_str else None
    if d is None:
        return True
    return (datetime.now(timezone.utc).date() - d).days > STALE_NAV_DAYS


def _search_query_from_name(scheme_name: str) -> str:
    """Truncate a CAS scheme name down to the fund-family name mfapi.in's
    own search matches against — "HSBC Small Cap Fund - Direct Plan -
    Growth" -> "HSBC Small Cap Fund". Stopping at the first "-"/"(" strips
    every plan/option qualifier (Direct/Regular/Growth/IDCW/...) without
    having to enumerate them all."""
    return re.split(r"\s*-\s*|\(", scheme_name or "")[0].strip()


async def _find_fresh_alternate(
    client: httpx.AsyncClient, scheme_name: str, isin: Optional[str]
) -> Optional[dict]:
    """Recovery path for a scheme whose CAS-embedded AMFI code has gone
    stale (see STALE_NAV_DAYS): search mfapi.in by fund name for
    candidates, then accept only the one whose own ISIN — the one
    identifier that survives an AMC recode — matches what the CAS
    statement actually says for this holding. Returns the full mfapi.in
    payload for the fresh candidate, or None if nothing matched.

    Candidates are fetched concurrently, not one at a time — reproduced
    live as a real incident: a portfolio's enrichment stuck for 7+
    minutes with zero progress, no exception anywhere. Traced to exactly
    this loop running sequentially: a scheme with no fresh replacement
    (an old/retired fund — HDFC Prudence, an LIC FMP series — whose ISIN
    matches none of the search results) used to exhaust up to
    MAX_ALTERNATE_CANDIDATES fetches one at a time, each up to ~32s
    worst case (3 retries * 10s timeout + delays) — up to ~320s for that
    ONE scheme alone. Since this whole function runs inside one
    scheme's coroutine in enrich_schemes's asyncio.gather() over the
    entire batch, and gather() waits for its slowest member regardless
    of how many others already finished, that one scheme was blocking
    every other scheme's already-fetched results from ever being
    persisted. Fetching all candidates at once bounds the worst case to
    roughly one candidate's own timeout instead of the sum of all of them."""
    if not isin:
        return None
    query = _search_query_from_name(scheme_name)
    if not query:
        return None
    candidates = await _fetch_json_retrying(client, f"{MFAPI_BASE}/search", params={"q": query})
    if not candidates:
        return None
    codes = [c.get("schemeCode") for c in candidates[:MAX_ALTERNATE_CANDIDATES] if c.get("schemeCode") is not None]
    if not codes:
        return None
    raws = await asyncio.gather(
        *[_fetch_json_retrying(client, f"{MFAPI_BASE}/{code}") for code in codes],
        return_exceptions=True,
    )
    for raw in raws:
        if not raw or isinstance(raw, BaseException):
            continue
        meta = raw.get("meta", {})
        if isin not in (meta.get("isin_growth"), meta.get("isin_div_reinvestment")):
            continue
        history = _extract_mfapi_nav_history(raw)
        if history and not _is_stale(history[-1]["date"]):
            return raw
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
    out: dict[str, Optional[float]] = {k: None for k in (*RETURN_DAY_PERIODS, *RETURN_YEAR_PERIODS)}
    if not nav_history:
        return out
    latest = nav_history[-1]
    latest_date = _parse_nav_date(latest["date"])
    if latest_date is None or not latest["nav"]:
        return out

    for period, days_back in RETURN_DAY_PERIODS.items():
        target = latest_date - timedelta(days=days_back)
        past = _nav_on_or_before(nav_history, target)
        if not past or not past[1]:
            continue
        past_date, past_nav = past
        if (latest_date - past_date).days <= 0:
            continue
        out[period] = round((latest["nav"] / past_nav - 1) * 100, 2)

    for period, years in RETURN_YEAR_PERIODS.items():
        target = _years_back(latest_date, years)
        past = _nav_on_or_before(nav_history, target)
        if not past or not past[1]:
            continue
        past_date, past_nav = past
        actual_days = (latest_date - past_date).days
        if actual_days <= 0:
            continue
        ratio = latest["nav"] / past_nav
        out[period] = round((ratio ** (365.0 / actual_days) - 1) * 100, 2)

    return out


def _daily_returns(nav_history: list[dict]) -> list[float]:
    """Day-over-day simple returns between consecutive published NAVs
    (already sorted oldest-first). Consecutive-*row* based rather than
    consecutive-*calendar-day*: a gap in the series (weekends, holidays —
    mfapi.in only has rows for days the AMC actually published a NAV) just
    means that pair's return spans a couple of days, same as every other
    fund tracker's daily-return series."""
    returns = []
    prev = None
    for row in nav_history:
        nav = row["nav"]
        if prev and prev > 0 and nav > 0:
            returns.append(nav / prev - 1)
        prev = nav
    return returns


def _std_dev(values: list[float]) -> Optional[float]:
    """Sample standard deviation (n-1 denominator)."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return variance ** 0.5


def _downside_deviation(daily_returns: list[float]) -> Optional[float]:
    """RMS of daily returns below a 0% target, denominator = count of
    negative days only. (Sortino's own original definition divides by the
    total period count instead — the choice here isn't textbook-purest,
    it's "verified to reproduce fundlens.lovable.app's live Sortino
    figure for a real fund almost exactly," which matters more for a
    number users will eyeball next to that site.)"""
    negative = [r for r in daily_returns if r < 0]
    if len(negative) < 2:
        return None
    return (sum(r ** 2 for r in negative) / len(negative)) ** 0.5


def _max_drawdown_pct(nav_history: list[dict]) -> Optional[float]:
    """Largest peak-to-trough decline across the *full* available NAV
    history (since inception), not just the trailing risk-ratio window —
    a fund's worst historical drawdown is the useful figure, and limiting
    it to 3y would hide anything before that."""
    if len(nav_history) < 2:
        return None
    peak = nav_history[0]["nav"]
    worst = 0.0
    for row in nav_history:
        nav = row["nav"]
        if nav > peak:
            peak = nav
        elif peak > 0:
            worst = min(worst, (nav - peak) / peak)
    return round(worst * 100, 2)


def _paired_daily_returns(a_history: list[dict], b_history: list[dict]) -> tuple[list[float], list[float]]:
    """Daily returns for two NAV series, aligned to dates present in BOTH
    (inner join) and kept strictly paired index-for-index — a fund and
    its benchmark index fund don't necessarily publish NAVs on exactly
    the same set of days, and beta/covariance is meaningless if the two
    lists drift out of alignment with each other."""
    a_by_date = {_parse_nav_date(r["date"]): r["nav"] for r in a_history}
    b_by_date = {_parse_nav_date(r["date"]): r["nav"] for r in b_history}
    common_dates = sorted(d for d in a_by_date if d is not None and d in b_by_date)
    a_ret: list[float] = []
    b_ret: list[float] = []
    prev_a = prev_b = None
    for d in common_dates:
        a, b = a_by_date[d], b_by_date[d]
        if prev_a and prev_a > 0 and a > 0 and prev_b and prev_b > 0 and b > 0:
            a_ret.append(a / prev_a - 1)
            b_ret.append(b / prev_b - 1)
        prev_a, prev_b = a, b
    return a_ret, b_ret


def _beta(fund_daily: list[float], bench_daily: list[float]) -> Optional[float]:
    n = len(fund_daily)
    if n < MIN_RISK_RATIO_DAYS or len(bench_daily) != n:
        return None
    mean_f = sum(fund_daily) / n
    mean_b = sum(bench_daily) / n
    covariance = sum((f - mean_f) * (b - mean_b) for f, b in zip(fund_daily, bench_daily)) / (n - 1)
    variance_b = sum((b - mean_b) ** 2 for b in bench_daily) / (n - 1)
    if not variance_b:
        return None
    return covariance / variance_b


def _compute_risk_ratios(
    nav_history: list[dict], benchmark_nav_history: Optional[list[dict]] = None
) -> dict[str, Optional[float]]:
    """Sharpe, Sortino, annualised volatility (std_dev), max drawdown, and
    (when a benchmark series is supplied) beta/alpha — all derived purely
    from NAV history mfapi.in already gives us for free. Sharpe/Sortino/
    volatility approach is the same fundlens.lovable.app uses (confirmed
    via its own "Data from MFAPI.in" footer credit and by reproducing its
    displayed figures for two real funds); beta/alpha use an index *fund*
    as a stand-in benchmark for the same reason (see BENCHMARK_AMFI_CODE).
    No dedicated ratios endpoint exists on any free source, so all of
    this is computed with standard formulas instead of staying null."""
    out: dict[str, Optional[float]] = {
        "std_dev": None, "sharpe": None, "sortino": None, "max_drawdown": None,
        "alpha": None, "beta": None,
    }
    if len(nav_history) < 2:
        return out

    out["max_drawdown"] = _max_drawdown_pct(nav_history)

    latest_date = _parse_nav_date(nav_history[-1]["date"])
    if latest_date is None:
        return out
    window_start = latest_date - timedelta(days=RISK_RATIO_WINDOW_DAYS)
    window = [r for r in nav_history if _parse_nav_date(r["date"]) >= window_start]
    daily = _daily_returns(window)
    if len(daily) < MIN_RISK_RATIO_DAYS:
        return out

    vol = _std_dev(daily)
    if not vol:
        return out
    annual_vol_pct = vol * (TRADING_DAYS_PER_YEAR ** 0.5) * 100
    out["std_dev"] = round(annual_vol_pct, 2)

    # Reuse the 3y CAGR already computed for the `returns` block rather
    # than deriving a second, possibly-inconsistent annualised return
    # figure from the daily series here.
    trailing_return = _compute_trailing_returns(nav_history).get("3y")
    if trailing_return is None:
        return out
    excess = trailing_return / 100 - RISK_FREE_RATE
    out["sharpe"] = round(excess / (annual_vol_pct / 100), 2)

    downside_dev = _downside_deviation(daily)
    if downside_dev:
        annual_downside_pct = downside_dev * (TRADING_DAYS_PER_YEAR ** 0.5) * 100
        out["sortino"] = round(excess / (annual_downside_pct / 100), 2)

    if benchmark_nav_history:
        bench_window = [r for r in benchmark_nav_history if _parse_nav_date(r["date"]) >= window_start]
        fund_paired, bench_paired = _paired_daily_returns(window, bench_window)
        beta = _beta(fund_paired, bench_paired)
        if beta is not None:
            out["beta"] = round(beta, 2)
            bench_return = _compute_trailing_returns(benchmark_nav_history).get("3y")
            if bench_return is not None:
                expected = RISK_FREE_RATE + beta * (bench_return / 100 - RISK_FREE_RATE)
                out["alpha"] = round((trailing_return / 100 - expected) * 100, 2)

    return out


async def _enrich_one(
    client: httpx.AsyncClient, amfi_code: str, isin: Optional[str], scheme_name: str,
    benchmark_nav_history: Optional[list[dict]] = None,
    cached_nav_history: Optional[list[dict]] = None,
) -> dict:
    """cached_nav_history: this scheme's NAV history already held in our
    own database, oldest-first, same shape _extract_mfapi_nav_history
    produces. When it's present and current, the whole mfapi.in history
    fetch is skipped — see the comment at the fetch below."""
    sources_used = []

    fields = dict(ENRICHED_FIELD_DEFAULTS)
    fields["returns"] = dict(ENRICHED_FIELD_DEFAULTS["returns"])
    fields["risk"] = dict(ENRICHED_FIELD_DEFAULTS["risk"])
    if MFDATA_ENABLED:
        mfdata_raw = await _fetch_json(client, f"{MFDATA_BASE}/schemes/{amfi_code}", timeout=MFDATA_TIMEOUT)
        if mfdata_raw:
            fields.update(_extract_mfdata_fields(mfdata_raw))
            sources_used.append("mfdata.in")

    # A scheme's NAV history is append-only: past NAVs never change, so
    # history we already have in nav_cache is permanently valid and only
    # ever needs new points added to the end. When ours is already
    # current, re-downloading the whole thing (a ~500KB response, 5000+
    # points, per scheme) buys literally nothing — and doing it for
    # every scheme in a portfolio is precisely the traffic that gets
    # this app rate-limited, which is the real cause of enrichment's
    # "worked last time, failed this time" behaviour. Skipping it here
    # is both the single biggest speed win available and the main way
    # to stop provoking the throttling in the first place.
    mfapi_raw = None
    if cached_nav_history and not _is_stale(cached_nav_history[-1]["date"]):
        nav_history = cached_nav_history
        sources_used.append("nav_cache")
    else:
        mfapi_raw = await _fetch_json_retrying(client, f"{MFAPI_BASE}/{amfi_code}")
        nav_history = _extract_mfapi_nav_history(mfapi_raw) if mfapi_raw else []
        # Falling back to what we already had beats returning nothing: a
        # throttled fetch shouldn't erase a perfectly good stored history
        # and downgrade this scheme to "unavailable" on the dashboard.
        if not nav_history and cached_nav_history:
            nav_history = cached_nav_history
            sources_used.append("nav_cache")
    if not nav_history or _is_stale(nav_history[-1]["date"]):
        # The AMFI code the CAS statement embeds has stopped publishing
        # NAVs — almost always an old code an AMC merger/rename retired
        # (see STALE_NAV_DAYS docstring). Try to recover the fund's real,
        # currently-updating code via its ISIN before giving up on it.
        alt_raw = await _find_fresh_alternate(client, scheme_name, isin)
        if alt_raw:
            mfapi_raw = alt_raw
            nav_history = _extract_mfapi_nav_history(mfapi_raw)

    computed_std_dev: Optional[float] = None
    if mfapi_raw:
        meta = mfapi_raw.get("meta", {})
        if not fields.get("category"):
            fields["category"] = meta.get("scheme_category")
        sources_used.append("mfapi.in")
    # Gated on nav_history, NOT on mfapi_raw: everything below derives
    # purely from the NAV series, and it's identical data whether it
    # arrived from a fresh fetch or straight out of nav_cache. Keying it
    # to the raw response (as it was, when a response was the only way
    # to have a series at all) would mean every cache hit silently
    # skipped every returns/risk computation and reported a scheme with
    # a full history as having no data — the exact class of silent,
    # shape-dependent blanking this module has been bitten by before.
    # Only the `meta` category lookup above genuinely needs the raw
    # response, so only that stays behind the mfapi_raw check.
    if nav_history:
        # Stored as ISO regardless of mfapi.in's own DD-MM-YYYY format —
        # every consumer (frontend date parsing, JSON) can rely on one
        # unambiguous shape rather than re-detecting it downstream.
        latest_parsed = _parse_nav_date(nav_history[-1]["date"]) if nav_history else None
        fields["nav_as_of"] = latest_parsed.isoformat() if latest_parsed else None
        # mfdata.in is the only source with pre-computed returns and it is
        # unreachable in practice (see module docstring) — compute our own
        # from the NAV history we just fetched rather than leave every
        # fund summary row blank.
        if not any(fields["returns"].values()):
            fields["returns"] = _compute_trailing_returns(nav_history)
        # Same story for sharpe/sortino/max_drawdown: nothing else here
        # provides them, so they always come from this computation.
        # std_dev is held back here rather than applied straight away —
        # captnemo's own volatility figure (below) should win over this
        # approximation when it's available; computed_std_dev is only
        # applied as the last-resort fallback, after that block runs.
        computed_risk = _compute_risk_ratios(nav_history, benchmark_nav_history)
        computed_std_dev = computed_risk.get("std_dev")
        for key in ("sharpe", "sortino", "max_drawdown", "alpha", "beta"):
            if fields["risk"].get(key) is None:
                fields["risk"][key] = computed_risk.get(key)
        if fields["risk"].get("beta") is not None and not fields.get("benchmark"):
            fields["benchmark"] = BENCHMARK_LABEL

    # captnemo (Kuvera's backing API) turns out to carry real per-scheme
    # analytics keyed by ISIN — category, expense ratio, fund manager(s),
    # and a volatility figure — that this module previously only used as
    # a last-resort category fallback. Queried unconditionally (not just
    # when mfdata failed) since mfdata practically never succeeds. AUM is
    # deliberately NOT taken from here: its units couldn't be confirmed
    # against a known fund's real AUM, and a wrong number dressed up as
    # "corpus_cr" is worse than a blank field.
    if isin:
        cn_raw = await _fetch_json_retrying(client, f"{CAPTNEMO_BASE}/{isin}", follow_redirects=True)
        cn_entry = cn_raw[0] if isinstance(cn_raw, list) and cn_raw else cn_raw
        if isinstance(cn_entry, dict):
            fields["category"] = fields.get("category") or cn_entry.get("fund_category") or cn_entry.get("category")
            fields["expense_ratio"] = fields.get("expense_ratio") or _num(cn_entry.get("expense_ratio"))
            fields["fund_manager"] = fields.get("fund_manager") or cn_entry.get("fund_manager")
            if fields["risk"].get("std_dev") is None:
                fields["risk"]["std_dev"] = _num(cn_entry.get("volatility"))
            sources_used.append("captnemo")

    # Last-resort std_dev fallback: mfdata (never) > captnemo's real
    # figure (above) > our own NAV-derived approximation.
    if fields["risk"].get("std_dev") is None:
        fields["risk"]["std_dev"] = computed_std_dev

    fields["enriched_at"] = datetime.now(timezone.utc).isoformat()
    fields["enrichment_source"] = "+".join(sources_used) if sources_used else "failed"
    fields["_nav_history"] = nav_history  # not part of the public `enriched` shape; consumed by portfolio.py
    return fields


BENCHMARK_CACHE_KEY = "__benchmark_nav_history__"


async def _get_benchmark_nav_history(client: httpx.AsyncClient, cache: dict) -> list[dict]:
    """The shared benchmark series (see BENCHMARK_AMFI_CODE) is one
    request that every scheme's alpha/beta in this batch depends on —
    confirmed live (not hypothetical): the exact same fetch, run seconds
    apart, failed once and succeeded once, because mfapi.in is a free
    best-effort API with no uptime guarantee. Without this fallback, that
    one blip nulled out alpha/beta for every fund in the batch, and
    because the per-scheme cache has its own 24h TTL, the null result
    stuck around for a full day instead of just retrying next time.
    _fetch_json_retrying already covers the "retry a few times" part;
    this adds the next layer down — falling back to whatever was cached
    from the last successful fetch, regardless of its own age, since a
    day-old benchmark series is still far better for beta/alpha than
    none at all (the index barely moves day to day relative to the 3y
    window these ratios use anyway)."""
    raw = await _fetch_json_retrying(client, f"{MFAPI_BASE}/{BENCHMARK_AMFI_CODE}")
    history = _extract_mfapi_nav_history(raw) if raw else []
    if history:
        cache[BENCHMARK_CACHE_KEY] = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "data": history,
        }
        return history
    stale = cache.get(BENCHMARK_CACHE_KEY)
    return stale["data"] if stale else []


async def enrich_schemes(schemes: list[dict]) -> dict[str, dict]:
    """schemes: list of {"amfi": str, "isin": str, "scheme": str,
    "nav_history": Optional[list[dict]]}. Returns {amfi_code:
    enriched_fields_dict}, using the on-disk cache wherever it's still
    fresh, and updating it with anything newly fetched.

    "nav_history" is optional and purely a performance path: this scheme's
    NAV series as already stored in the caller's database. Supplying it
    lets a scheme whose history is already current skip its mfapi.in
    fetch entirely (see _enrich_one) — correctness is identical either
    way, since it's the same append-only series from the same source."""
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
            # Fetched once per batch, not once per scheme: it's the same
            # series for every fund, and this way a portfolio with 20
            # holdings costs 1 extra request, not 20.
            benchmark_nav_history = await _get_benchmark_nav_history(client, cache)
            # A fresh Semaphore per call, not a module-level one: this
            # runs inside a background task's own short-lived event loop
            # (asyncio.run() per enrichment run — see main.py), and a
            # Semaphore created before any loop exists risks binding to
            # the wrong one.
            semaphore = asyncio.Semaphore(ENRICH_CONCURRENCY_LIMIT)

            async def _enrich_one_bounded(s: dict) -> dict:
                async with semaphore:
                    return await _enrich_one(
                        client, s["amfi"], s.get("isin"), s.get("scheme", ""), benchmark_nav_history,
                        cached_nav_history=s.get("nav_history"),
                    )

            # return_exceptions=True is load-bearing, not defensive
            # boilerplate: without it, one scheme raising (a real
            # incident — one bad fund in a 14-scheme real-portfolio batch
            # took down NAV enrichment for all 13 others, silently,
            # because plain gather() discards every already-completed
            # result the moment any single coroutine raises) blows up the
            # whole batch instead of failing just that one scheme.
            fetched = await asyncio.gather(*[
                _enrich_one_bounded(s) for s in to_fetch
            ], return_exceptions=True)
        for scheme, data in zip(to_fetch, fetched):
            amfi = scheme["amfi"]
            if isinstance(data, BaseException):
                logger.exception("enrich_schemes: _enrich_one failed for amfi=%s", amfi, exc_info=data)
                continue
            results[amfi] = data
            cache[amfi] = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "ttl_hours": CACHE_TTL_HOURS,
                "data": data,
            }
        _save_cache(cache)

    return results
