"""
PortfolioIQ — benchmark_service.py

Personalized benchmark simulation (spec section 14.2): replay the same
external cash-flow amounts, on the same dates, into a benchmark's own
NAV series, then XIRR that simulated position exactly like a real
holding. Comparable to the portfolio's own XIRR because it uses the
identical cash-flow dates/amounts — only what those rupees were "put
into" differs.

Three benchmark columns (spec 14.1, 14.3):
  - Nifty 50: no free source publishes the real TRI index values (proven
    this session — checked directly against Kuvera's own OpenAPI spec).
    UTI Nifty 50 Index Fund (AMFI 120716) stands in as a NAV proxy —
    labelled "proxy" everywhere, never presented as the real TRI, per
    the spec's own explicit guardrail (14.3, 22).
  - Nifty 500: no reliable source at all -> BENCHMARK_UNAVAILABLE.
  - Fund-respective: no scheme->benchmark mapping/history source exists
    yet -> BENCHMARK_UNAVAILABLE. The scheme_benchmark_map table and
    BenchmarkProvider interface (spec 16.1) are still real and usable
    the moment such a source is configured — nothing here is a stub
    that needs rewriting later, just an empty mapping table today.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

import nav_service
import xirr_engine
from models import BenchmarkDefinition, BenchmarkPoint, Scheme

NIFTY50_PROXY_AMFI_CODE = "120716"  # UTI Nifty 50 Index Fund - Direct - Growth
NIFTY50_PROXY_NAME = "Nifty 50"
NIFTY50_PROXY_DISCLOSURE = (
    "Proxy via UTI Nifty 50 Index Fund (Direct Growth), not the official Nifty 50 TRI series — "
    "no free source publishes raw TRI values. Includes the fund's own expense ratio/tracking error/cash drag."
)
MFAPI_BASE = "https://api.mfapi.in/mf"


@dataclass
class BenchmarkXirrResult:
    value: Optional[Decimal]
    label: str
    proxy_disclosure: Optional[str]
    status: str  # "ok" | "unavailable"
    reason: Optional[str] = None


def get_or_create_nifty50_proxy(session: Session) -> BenchmarkDefinition:
    existing = session.execute(
        select(BenchmarkDefinition).where(
            BenchmarkDefinition.name == NIFTY50_PROXY_NAME
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    benchmark = BenchmarkDefinition(
        name=NIFTY50_PROXY_NAME,
        kind="index_fund_proxy",
        source_code=NIFTY50_PROXY_AMFI_CODE,
        proxy_disclosure=NIFTY50_PROXY_DISCLOSURE,
    )
    session.add(benchmark)
    session.flush()
    return benchmark


def _get_or_create_proxy_scheme(session: Session, amfi_code: str, name: str) -> Scheme:
    existing = session.execute(
        select(Scheme).where(Scheme.amfi_code == amfi_code).limit(1)
    ).scalar_one_or_none()
    if existing:
        return existing
    scheme = Scheme(amfi_code=amfi_code, name=name, asset_class="OTHER", active=True)
    session.add(scheme)
    session.flush()
    return scheme


FETCH_RETRY_ATTEMPTS = 3
FETCH_RETRY_DELAY_SECONDS = 1.0


async def refresh_nifty50_proxy_nav(
    session: Session, client: httpx.AsyncClient
) -> None:
    """Fetch/refresh the proxy fund's full NAV history into nav_cache —
    call this from the same background enrichment cycle that refreshes
    per-scheme NAVs (spec 18), not per-request.

    Retries on failure and never raises: mfapi.in is proven this session
    to fail transiently under normal conditions, and this call shares a
    session/transaction with the rest of the enrichment background task
    (see main.py's _run_enrichment_task) — an uncaught exception here
    would roll back every scheme's freshly-fetched NAV/risk data in the
    same run, not just this one benchmark fetch (caught live: a single
    ConnectTimeout here silently discarded an already-successful scheme
    NAV population that happened earlier in the same transaction)."""
    scheme = _get_or_create_proxy_scheme(
        session, NIFTY50_PROXY_AMFI_CODE, "UTI Nifty 50 Index Fund - Direct Growth"
    )
    latest = nav_service.get_latest_nav(session, scheme.scheme_id)
    if latest and (date.today() - latest.resolved_date).days <= 1:
        return
    session.commit()
    from provider import fetch_json

    raw = await fetch_json(client, f"{MFAPI_BASE}/{NIFTY50_PROXY_AMFI_CODE}")
    if raw is None:
        return

    points = []
    for row in raw.get("data", []):
        try:
            d = datetime.strptime(row["date"], "%d-%m-%Y").date()
            points.append((d, Decimal(str(row["nav"]))))
        except (ValueError, KeyError, TypeError):
            continue
    nav_service.store_nav_points(
        session,
        scheme.scheme_id,
        points,
        stored_summary=nav_service.get_stored_nav_summary(
            session, [scheme.scheme_id]
        ).get(scheme.scheme_id),
    )


def _nifty50_proxy_scheme_id(session: Session) -> Optional[int]:
    scheme = session.execute(
        select(Scheme).where(Scheme.amfi_code == NIFTY50_PROXY_AMFI_CODE).limit(1)
    ).scalar_one_or_none()
    return scheme.scheme_id if scheme else None


def simulate_benchmark_xirr(
    session: Session,
    cashflows: list[tuple[date, Decimal]],
    valuation_date: date,
    benchmark_name: str = "Nifty 50",
) -> BenchmarkXirrResult:
    """Spec 14.2's replay algorithm. `cashflows` are the SAME external
    cash flows already used for the real portfolio/scheme/advisor XIRR —
    negative for money invested, positive for redemption/IDCW payout —
    with internal switch legs already eliminated by the caller (spec
    14.2: "Internal switches are removed... for overall/advisor/investor
    benchmark calculations")."""
    if benchmark_name != "Nifty 50":
        return BenchmarkXirrResult(
            value=None,
            label=benchmark_name,
            proxy_disclosure=None,
            status="unavailable",
            reason="BENCHMARK_UNAVAILABLE: no reliable free source configured for this benchmark yet.",
        )

    if "benchmark_scheme_id" not in session.info:
        session.info["benchmark_scheme_id"] = _nifty50_proxy_scheme_id(session)
    scheme_id = session.info["benchmark_scheme_id"]
    if scheme_id is None:
        return BenchmarkXirrResult(
            value=None,
            label=NIFTY50_PROXY_NAME,
            proxy_disclosure=NIFTY50_PROXY_DISCLOSURE,
            status="unavailable",
            reason="BENCHMARK_UNAVAILABLE: proxy fund NAV history not yet fetched.",
        )

    history_key = ("benchmark_history", scheme_id)
    history = session.info.get(history_key)
    if history is None:
        history = nav_service.get_nav_history(session, scheme_id)
        session.info[history_key] = history
    dates = [date.fromisoformat(r["date"]) for r in history]

    def point_on(d):
        index = bisect_right(dates, d) - 1
        if index < 0:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(nav=Decimal(str(history[index]["nav"])))

    benchmark_units = Decimal("0")
    for cf_date, cf in sorted(cashflows, key=lambda x: x[0]):
        point = point_on(cf_date)
        if point is None or not point.nav:
            return BenchmarkXirrResult(
                value=None,
                label=NIFTY50_PROXY_NAME,
                proxy_disclosure=NIFTY50_PROXY_DISCLOSURE,
                status="unavailable",
                reason=f"BENCHMARK_UNAVAILABLE: no benchmark NAV on or before {cf_date}.",
            )
        if cf < 0:
            benchmark_units += (-cf) / point.nav
        elif cf > 0:
            benchmark_units -= cf / point.nav

    terminal_point = point_on(valuation_date)
    if terminal_point is None or benchmark_units <= 0:
        return BenchmarkXirrResult(
            value=None,
            label=NIFTY50_PROXY_NAME,
            proxy_disclosure=NIFTY50_PROXY_DISCLOSURE,
            status="unavailable",
            reason="BENCHMARK_UNAVAILABLE: no terminal benchmark NAV or non-positive simulated position.",
        )

    terminal_value = benchmark_units * terminal_point.nav
    sim_flows = list(cashflows) + [(valuation_date, terminal_value)]
    outcome = xirr_engine.xirr(sim_flows)
    if outcome.value is None:
        return BenchmarkXirrResult(
            value=None,
            label=NIFTY50_PROXY_NAME,
            proxy_disclosure=NIFTY50_PROXY_DISCLOSURE,
            status="unavailable",
            reason=f"XIRR_NO_SOLUTION: {outcome.reason}",
        )
    return BenchmarkXirrResult(
        value=outcome.value,
        label=NIFTY50_PROXY_NAME,
        proxy_disclosure=NIFTY50_PROXY_DISCLOSURE,
        status="ok",
    )
