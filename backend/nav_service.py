"""
PortfolioIQ — nav_service.py

resolve_nav(scheme_id, requested_date) (spec section 7.1) backed by the
nav_cache table: historical points are immutable once stored (refetch
only on integrity error), the latest date's row is the only one ever
refreshed. This replaces enrichment.py's old pattern of re-fetching a
scheme's whole NAV history on every enrichment cycle — a real DB cache
means a page request never waits on mfapi.in directly (spec 18: "Do not
fetch one MFAPI history per scheme during every page request").

Never returns a future NAV for a historical date (spec 7.1, guardrail
in section 22) — resolve_nav_on_or_before enforces requested_date as a
hard upper bound on the candidate search, not just "closest available."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from models import NavCache, Scheme


@dataclass
class NavPoint:
    scheme_id: int
    requested_date: date
    resolved_date: date  # may be earlier than requested_date (weekend/holiday/no data that day)
    nav: Decimal
    source: str


def get_nav_on_or_before(session: Session, scheme_id: int, requested_date: date) -> Optional[NavPoint]:
    """Step 1-3 of the spec 7.1 algorithm: exact-date cache hit first,
    else the latest cached point at or before requested_date. Does NOT
    itself fetch from mfapi.in — that's nav_ingest.store_nav_history's
    job, run ahead of time by the enrichment background task (spec 18),
    so a page request only ever reads this cache, never blocks on an
    external call."""
    row = session.execute(
        select(NavCache)
        .where(NavCache.scheme_id == scheme_id, NavCache.nav_date <= requested_date)
        .order_by(NavCache.nav_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return NavPoint(
        scheme_id=scheme_id,
        requested_date=requested_date,
        resolved_date=row.nav_date,
        nav=row.nav,
        source=row.source,
    )


def get_latest_nav(session: Session, scheme_id: int) -> Optional[NavPoint]:
    return get_nav_on_or_before(session, scheme_id, date.today())


NAV_UPSERT_BATCH_SIZE = 500


def store_nav_points(
    session: Session, scheme_id: int, points: list[tuple[date, Decimal]], source: str = "mfapi.in"
) -> None:
    """Upsert a batch of (date, nav) points for one scheme in single
    multi-row statements rather than one round-trip per point — a
    fund's full history can be 3000+ points (13 years daily), and this
    is exactly the "batch refresh in background jobs, don't hammer the
    DB row by row" performance this app needs (spec 18). Historical
    points are immutable in practice (a past NAV never changes), but
    this upserts rather than insert-if-missing so a corrected re-fetch
    (integrity-error recovery, spec 7.3) can still overwrite a bad
    cached value without a separate delete step."""
    if not points:
        return
    now = datetime.now(timezone.utc)
    for i in range(0, len(points), NAV_UPSERT_BATCH_SIZE):
        chunk = points[i:i + NAV_UPSERT_BATCH_SIZE]
        stmt = pg_insert(NavCache).values([
            {"scheme_id": scheme_id, "nav_date": nav_date, "nav": nav, "source": source, "fetched_at": now}
            for nav_date, nav in chunk
        ])
        stmt = stmt.on_conflict_do_update(
            index_elements=["scheme_id", "nav_date"],
            set_={"nav": stmt.excluded.nav, "source": stmt.excluded.source, "fetched_at": stmt.excluded.fetched_at},
        )
        session.execute(stmt)


def resolve_scheme_by_amfi(session: Session, amfi_code: str) -> Optional[Scheme]:
    return session.execute(select(Scheme).where(Scheme.amfi_code == amfi_code)).scalar_one_or_none()


def resolve_scheme_by_isin(session: Session, isin: str) -> Optional[Scheme]:
    return session.execute(select(Scheme).where(Scheme.isin == isin)).scalar_one_or_none()
