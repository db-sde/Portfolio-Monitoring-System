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

from sqlalchemy import func, select
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


def get_navs_on_or_before(
    session: Session, scheme_ids: list[int], requested_date: date
) -> dict[int, NavPoint]:
    """get_nav_on_or_before for many schemes in ONE query, via Postgres
    DISTINCT ON. Same semantics per scheme — the latest point at or
    before requested_date, never a future NAV (spec 7.1) — just resolved
    for the whole set at once instead of a round trip each. Called once
    per page render rather than once per holding."""
    if not scheme_ids:
        return {}
    rows = session.execute(
        select(NavCache)
        .where(NavCache.scheme_id.in_(scheme_ids), NavCache.nav_date <= requested_date)
        .distinct(NavCache.scheme_id)
        .order_by(NavCache.scheme_id, NavCache.nav_date.desc())
    ).scalars()
    return {
        row.scheme_id: NavPoint(
            scheme_id=row.scheme_id, requested_date=requested_date,
            resolved_date=row.nav_date, nav=row.nav, source=row.source,
        )
        for row in rows
    }


# 500 was too conservative in practice: a real 52-scheme portfolio (some
# funds with 20+ years of daily NAV history, ~5,000+ rows each) took
# 262s to enrich — 23s of that was the concurrent mfapi.in fetch, the
# rest was this function alone, because 500-row chunking meant 6+
# separate round trips to Neon per scheme, at maybe 400-800ms each on a
# free-tier connection: enough round trips add up to minutes, and a
# background task that slow risks never finishing before Render's
# process cycles it — which is almost certainly why a real production
# upload this size came back with zero enrichment for every scheme, not
# just some of them. 8000 rows/batch covers any realistic single
# scheme's full history (23 years * ~250 trading days ≈ 5,750 rows) in
# one round trip, and 8000 rows * 5 columns = 40,000 bind parameters,
# safely under Postgres's ~65,535-parameter-per-statement limit.
NAV_UPSERT_BATCH_SIZE = 8000


def get_nav_history(session: Session, scheme_id: int) -> list[dict]:
    """This scheme's full stored NAV series, oldest-first, in the same
    {"date": ISO str, "nav": float} shape enrichment.py builds from an
    mfapi.in response — so it can be handed straight back to
    enrich_schemes as `nav_history` and used in place of re-fetching.
    Measured: reading a 5,200-point history from Neon takes ~1.1s versus
    a ~500KB HTTP round trip to a host that rate-limits us."""
    rows = session.execute(
        select(NavCache.nav_date, NavCache.nav).where(NavCache.scheme_id == scheme_id).order_by(NavCache.nav_date)
    ).all()
    return [{"date": nav_date.isoformat(), "nav": float(nav)} for nav_date, nav in rows]


def get_nav_histories(session: Session, scheme_ids: list[int]) -> dict[int, list[dict]]:
    """get_nav_history for many schemes in ONE query instead of one per
    scheme. Against Neon the per-query latency dominates the payload:
    53 separate history reads measured 16.8s versus 12.4s batched, and
    the equivalent per-scheme metadata lookups went 13.9s -> 0.5s. The
    full series is returned deliberately — max drawdown is computed over
    a fund's ENTIRE history, so truncating to a few recent years to save
    bandwidth would quietly change a reported figure rather than just
    making things faster."""
    if not scheme_ids:
        return {}
    rows = session.execute(
        select(NavCache.scheme_id, NavCache.nav_date, NavCache.nav)
        .where(NavCache.scheme_id.in_(scheme_ids))
        .order_by(NavCache.scheme_id, NavCache.nav_date)
    ).all()
    out: dict[int, list[dict]] = {}
    for scheme_id, nav_date, nav in rows:
        out.setdefault(scheme_id, []).append({"date": nav_date.isoformat(), "nav": float(nav)})
    return out


def get_stored_nav_summary(session: Session, scheme_ids: list[int]) -> dict[int, tuple[Optional[date], int]]:
    """{scheme_id: (max_nav_date, row_count)} for many schemes in ONE
    aggregate query. Deliberately an aggregate rather than reading the
    rows themselves: this is used to decide how much of an incoming
    history actually needs writing, so it must be far cheaper than the
    write it's trying to avoid — a few dozen rows back, whatever the
    portfolio's total (197,600 rows here)."""
    if not scheme_ids:
        return {}
    rows = session.execute(
        select(NavCache.scheme_id, func.max(NavCache.nav_date), func.count())
        .where(NavCache.scheme_id.in_(scheme_ids))
        .group_by(NavCache.scheme_id)
    ).all()
    return {scheme_id: (max_date, count) for scheme_id, max_date, count in rows}


def store_nav_points(
    session: Session, scheme_id: int, points: list[tuple[date, Decimal]], source: str = "mfapi.in",
    stored_summary: Optional[tuple[Optional[date], int]] = None,
) -> None:
    """Upsert a batch of (date, nav) points for one scheme in single
    multi-row statements rather than one round-trip per point — a
    fund's full history can be 3000+ points (13 years daily), and this
    is exactly the "batch refresh in background jobs, don't hammer the
    DB row by row" performance this app needs (spec 18). Historical
    points are immutable in practice (a past NAV never changes), but
    this upserts rather than insert-if-missing so a corrected re-fetch
    (integrity-error recovery, spec 7.3) can still overwrite a bad
    cached value without a separate delete step.

    stored_summary: optional (max_stored_date, stored_row_count) for this
    scheme, from get_stored_nav_summary. When supplied, only genuinely
    new points are written instead of the whole history every time. This
    was the single largest cost in the entire enrichment run — measured
    at 3.8s per scheme to re-upsert an unchanged 5,202-point history,
    roughly 205s across a real 54-scheme portfolio, spent rewriting rows
    to the values they already held. Past NAVs are immutable, so on a
    re-run there is usually nothing to write at all."""
    if not points:
        return
    if stored_summary is not None:
        max_stored_date, stored_count = stored_summary
        if max_stored_date is not None:
            new_points = [(d, nav) for d, nav in points if d > max_stored_date]
            # Only take the cheap append-only path when the stored row
            # count is exactly consistent with "everything up to
            # max_stored_date is already present". If it isn't, an
            # earlier write left a gap mid-history, and topping up only
            # the tail would preserve that gap forever — fall through to
            # the full upsert, which self-heals it.
            if stored_count == len(points) - len(new_points):
                if not new_points:
                    return
                points = new_points
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
