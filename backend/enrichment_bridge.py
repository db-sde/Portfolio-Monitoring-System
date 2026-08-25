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

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import enrichment
import nav_service
from models import EnrichmentCache, Scheme

logger = logging.getLogger("portfolioiq")

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

    # Committing after every scheme (as this used to) meant a large
    # portfolio spent a full Neon round-trip per scheme just on commit
    # overhead — reported live as enrichment taking minutes longer than
    # it needed to. Batched here instead: BATCH_COMMIT_SIZE schemes'
    # worth of writes share one commit, cutting round-trips by roughly
    # that factor, while still bounding a connection failure's damage to
    # at most one batch instead of either "lose everything" (a bare
    # session.rollback() shared across the whole loop, the bug this
    # SAVEPOINT design replaced) or "lose nothing" (the per-scheme-commit
    # version this replaces, correct but needlessly slow).
    BATCH_COMMIT_SIZE = 5
    uncommitted = 0

    for scheme in to_fetch:
        payload = results.get(scheme.amfi_code) if scheme.amfi_code else None
        if payload is None:
            continue
        try:
            # A SAVEPOINT (begin_nested), not the bare session: this loop
            # shares one session/transaction across a whole batch, and a
            # plain session.rollback() on a mid-batch failure would undo
            # every earlier scheme's already-flushed writes in this SAME
            # batch too, not just this one's — a real bug caught before
            # it shipped. begin_nested() scopes the rollback to just this
            # scheme's own SAVEPOINT on exception, leaving the rest of
            # the batch's already-flushed work in the (still-open) outer
            # transaction intact — this protects against a *data*
            # problem in one scheme's payload, and the exception handler
            # below only escalates to session.rollback() (losing the
            # whole in-flight batch) for the different case this SAVEPOINT
            # alone can't cover: a *connection*-level failure (a real
            # Neon timeout, caught live: writing one scheme's several-
            # thousand-row NAV history took long enough to hit "SSL
            # SYSCALL error: Operation timed out"), which invalidates the
            # session itself, not just the current SAVEPOINT.
            with session.begin_nested():
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
        except Exception:
            logger.exception(
                "refresh_enrichment: failed to persist scheme_id=%s amfi=%s",
                scheme.scheme_id, scheme.amfi_code,
            )
            # begin_nested()'s own SAVEPOINT rollback already handled a
            # plain data/payload problem cleanly on its own — the session
            # is still perfectly usable, so calling session.rollback()
            # here too would be the exact bug this design exists to
            # avoid, discarding the rest of this batch's already-flushed
            # schemes for no reason. is_active is False specifically when
            # the session itself is the casualty (a connection-level
            # failure) rather than just this one scheme's SAVEPOINT — only
            # then is losing the in-flight batch actually unavoidable.
            if not session.is_active:
                session.rollback()
                uncommitted = 0
            continue
        fresh[scheme.scheme_id] = payload
        uncommitted += 1
        if uncommitted >= BATCH_COMMIT_SIZE:
            session.commit()
            uncommitted = 0

    if uncommitted > 0:
        session.commit()

    return fresh
