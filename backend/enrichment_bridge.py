"""
PortfolioIQ — enrichment_bridge.py

Bridges the existing, already-tested enrichment.py (mfapi.in/captnemo
fetching, Sharpe/Sortino/alpha/beta computation, stale-AMFI-code retry
logic — all built and validated earlier) onto the new Postgres schema,
rather than rewriting that module's internals. enrichment.py keeps its
own file-based cache as a secondary/redundant layer (harmless — it just
occasionally re-fetches after a Render redeploy wipes it, same as
before this migration); this bridge is what makes the results actually
SURVIVE a redeploy, by persisting the two things that matter for
correctness into Postgres:

  - the NAV history it fetches -> nav_cache (this is what
    portfolio_service's live valuation depends on)
  - the computed returns/risk/category/etc. -> enrichment_cache (this is
    what the Fund Summary page depends on)

24h freshness is checked against enrichment_cache's own fetched_at
first, so a warm Postgres cache skips calling enrichment.py entirely on
most requests, not just after the first one per process.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import enrichment
import nav_service
from models import EnrichmentCache, Scheme

CACHE_TTL_HOURS = 24
PROVIDER_KEY = "mfapi.in+captnemo"


def _is_fresh(fetched_at: Optional[datetime]) -> bool:
    if fetched_at is None:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched_at < timedelta(hours=CACHE_TTL_HOURS)


def get_cached_enrichment(session: Session, scheme_id: int) -> Optional[dict]:
    row = session.execute(
        select(EnrichmentCache).where(EnrichmentCache.scheme_id == scheme_id, EnrichmentCache.provider == PROVIDER_KEY)
    ).scalar_one_or_none()
    if row and _is_fresh(row.fetched_at):
        return row.payload
    return None


async def refresh_enrichment(session: Session, schemes: list[Scheme]) -> dict[int, dict]:
    """schemes: DB Scheme rows needing enrichment. Returns {scheme_id: payload}."""
    fresh: dict[int, dict] = {}
    to_fetch: list[Scheme] = []
    for scheme in schemes:
        cached = get_cached_enrichment(session, scheme.scheme_id)
        if cached is not None:
            fresh[scheme.scheme_id] = cached
        else:
            to_fetch.append(scheme)

    if not to_fetch:
        return fresh

    targets = [
        {"amfi": s.amfi_code, "isin": s.isin, "scheme": s.name}
        for s in to_fetch if s.amfi_code
    ]
    results = await enrichment.enrich_schemes(targets)  # {amfi_code: payload}

    for scheme in to_fetch:
        payload = results.get(scheme.amfi_code) if scheme.amfi_code else None
        if payload is None:
            continue
        nav_history = payload.pop("_nav_history", None) or []
        if nav_history:
            points = []
            for row in nav_history:
                d = enrichment._parse_nav_date(row["date"])
                if d is not None:
                    points.append((d, Decimal(str(row["nav"]))))
            nav_service.store_nav_points(session, scheme.scheme_id, points)

        existing = session.execute(
            select(EnrichmentCache).where(
                EnrichmentCache.scheme_id == scheme.scheme_id, EnrichmentCache.provider == PROVIDER_KEY,
            )
        ).scalar_one_or_none()
        data_as_of = date.today()
        if existing:
            existing.payload = payload
            existing.data_as_of = data_as_of
            existing.fetched_at = datetime.now(timezone.utc)
            existing.status = "ok" if payload.get("enrichment_source") != "failed" else "unavailable"
        else:
            session.add(EnrichmentCache(
                scheme_id=scheme.scheme_id, provider=PROVIDER_KEY, payload=payload,
                data_as_of=data_as_of, fetched_at=datetime.now(timezone.utc),
                status="ok" if payload.get("enrichment_source") != "failed" else "unavailable",
            ))
        fresh[scheme.scheme_id] = payload

    session.flush()
    return fresh
