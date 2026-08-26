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
    # status == "ok" is the real gate here, not just freshness — this is
    # what refresh_enrichment (below) calls to decide "do I need to
    # re-fetch this scheme," and a cached FAILURE inside the same 24h
    # window used to look just as valid as a cached success, so a
    # scheme that failed once got silently treated as "already handled"
    # for a full day. Reproduced live: POST /api/enrich/retry ran
    # cleanly and reported success, but every one of the 24 schemes it
    # was meant to retry came straight back out of this cache unchanged
    # — retry was a complete no-op until the 24h window happened to
    # expire on its own. main.py's own read of this (Fund Summary,
    # display-only) isn't affected: an "unavailable" payload's fields
    # are already all None, identical to getting nothing back here.
    row = session.execute(
        select(EnrichmentCache).where(EnrichmentCache.scheme_id == scheme_id, EnrichmentCache.provider == PROVIDER_KEY)
    ).scalar_one_or_none()
    if row and row.status == "ok" and _is_fresh(row.fetched_at):
        return row.payload
    return None


async def refresh_enrichment(session: Session, schemes: list[Scheme], on_stage=None) -> dict[int, dict]:
    """schemes: DB Scheme rows needing enrichment. Returns {scheme_id: payload}.

    on_stage: optional callable(str) -> None, called at checkpoints —
    temporary diagnostic for a real incident (enrichment stuck for
    minutes with zero progress and no exception). Lets the caller record
    progress (e.g. onto a pollable DB row) without this module needing
    to know anything about how/where that's stored."""
    def _stage(name: str) -> None:
        if on_stage:
            on_stage(name)

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
    _stage(f"fetching_{len(targets)}_schemes")
    results = await enrichment.enrich_schemes(targets)  # {amfi_code: payload}
    _stage(f"fetched_{len(results)}_results_persisting")

    def _persist_unavailable(scheme: Scheme, reason: str) -> None:
        # Shared by every "this scheme has no usable payload" case below —
        # whichever the reason, the fix is the same: an explicit
        # "unavailable" EnrichmentCache row, so it counts as attempted
        # (landing in "failed") instead of silently never being recorded
        # at all. The bug this replaces: a bare `continue` here, for ANY
        # of these reasons, left `pending` permanently non-zero — no
        # amount of waiting ever resolved it, since nothing was ever
        # written for that scheme. Reproduced live twice, for two
        # different reasons: no AMFI code at all (a SCHEME_UNRESOLVED
        # holding), and — the more surprising one — a perfectly normal,
        # well-known fund (valid AMFI code and ISIN, fetched successfully
        # in isolated testing) whose fetch simply failed or raised once
        # within a ~50-scheme concurrent batch, which is exactly the kind
        # of transient blip mfapi.in/captnemo are documented elsewhere in
        # this codebase to produce under concurrent load.
        try:
            with session.begin_nested():
                existing = session.execute(
                    select(EnrichmentCache).where(
                        EnrichmentCache.scheme_id == scheme.scheme_id, EnrichmentCache.provider == PROVIDER_KEY,
                    )
                ).scalar_one_or_none()
                payload = {"enrichment_source": "failed", "reason": reason}
                if existing:
                    existing.payload = payload
                    existing.data_as_of = date.today()
                    existing.fetched_at = datetime.now(timezone.utc)
                    existing.status = "unavailable"
                else:
                    session.add(EnrichmentCache(
                        scheme_id=scheme.scheme_id, provider=PROVIDER_KEY, payload=payload,
                        data_as_of=date.today(), fetched_at=datetime.now(timezone.utc), status="unavailable",
                    ))
            session.commit()
        except Exception:
            logger.exception(
                "refresh_enrichment: failed to persist unavailable status for scheme_id=%s (%s)",
                scheme.scheme_id, reason,
            )
            session.rollback()

    for i, scheme in enumerate(to_fetch):
        _stage(f"persisting_{i}_of_{len(to_fetch)}_scheme_id_{scheme.scheme_id}")
        if not scheme.amfi_code:
            # No AMFI code at all (a SCHEME_UNRESOLVED holding — an old/
            # obscure fund ingestion couldn't confirm an identity for) is
            # never in `targets` above, so `results` has nothing for it —
            # this isn't "not fetched yet," it's "can never be fetched."
            _persist_unavailable(scheme, "no AMFI code resolved for this scheme")
            continue
        payload = results.get(scheme.amfi_code)
        if payload is None:
            # A resolvable scheme whose fetch still came back empty — it
            # either raised inside enrich_schemes's own gather()
            # (return_exceptions=True catches that there, but the result
            # is simply absent from `results`, same as never having been
            # fetched at all from this function's point of view) or
            # genuinely returned nothing. Recorded as unavailable rather
            # than silently dropped, same reasoning as the no-AMFI-code
            # case above — this scheme just gets another chance on the
            # next enrichment run instead of being invisible until then.
            _persist_unavailable(scheme, "fetch returned no data for this scheme")
            continue
        try:
            # A SAVEPOINT (begin_nested), not the bare session: this loop
            # shares one session/transaction across every scheme in the
            # batch, and a plain session.rollback() on a mid-loop failure
            # would undo every earlier scheme's already-flushed writes
            # too, not just this one's — a real bug caught before it
            # shipped. begin_nested() scopes the rollback to just this
            # scheme's own SAVEPOINT on exception, leaving prior schemes'
            # flushed work in the (still-open) outer transaction intact —
            # but that alone only protects against a *data* problem in
            # one scheme's payload. A *connection*-level failure (a real
            # Neon timeout, caught live: writing one scheme's several-
            # thousand-row NAV history took long enough to hit "SSL
            # SYSCALL error: Operation timed out") breaks the whole
            # session, and everything still sitting uncommitted — every
            # earlier scheme in this same loop, even ones that finished
            # cleanly — would be lost when the loop's single trailing
            # flush/the caller's eventual commit fails too. Committing
            # per scheme, right after each one's SAVEPOINT releases,
            # makes every scheme's success durable independently of
            # whether a later one in the same batch loses its connection.
            with session.begin_nested():
                # payload is looked up from `results` by amfi_code, and two
                # DIFFERENT schemes can legitimately share one amfi_code —
                # mfapi.in issues a single code per fund with two ISINs
                # (isin_growth vs isin_div_reinvestment) for a dividend/IDCW
                # plan's payout vs reinvestment variants. Both scheme rows
                # then get the SAME dict object out of `results`. A `.pop()`
                # here used to mutate that shared dict in place, so whichever
                # scheme was processed first drained `_nav_history` for
                # itself and left the second scheme's copy permanently
                # empty — reproduced live: scheme_id 1520 ended up with all
                # 5021 NAV points, scheme_id 1524 (same amfi_code 100120)
                # got zero, so its holding valued at Rs.0 despite showing
                # enrichment status "ok". `.get()` (never mutating the
                # shared dict) plus building a separate dict for what gets
                # persisted keeps every scheme sharing a code independent.
                nav_history = payload.get("_nav_history") or []
                if nav_history:
                    points = []
                    for row in nav_history:
                        d = enrichment._parse_nav_date(row["date"])
                        if d is not None:
                            points.append((d, Decimal(str(row["nav"]))))
                    nav_service.store_nav_points(session, scheme.scheme_id, points)

                cache_payload = {k: v for k, v in payload.items() if k != "_nav_history"}
                existing = session.execute(
                    select(EnrichmentCache).where(
                        EnrichmentCache.scheme_id == scheme.scheme_id, EnrichmentCache.provider == PROVIDER_KEY,
                    )
                ).scalar_one_or_none()
                data_as_of = date.today()
                if existing:
                    existing.payload = cache_payload
                    existing.data_as_of = data_as_of
                    existing.fetched_at = datetime.now(timezone.utc)
                    existing.status = "ok" if cache_payload.get("enrichment_source") != "failed" else "unavailable"
                else:
                    session.add(EnrichmentCache(
                        scheme_id=scheme.scheme_id, provider=PROVIDER_KEY, payload=cache_payload,
                        data_as_of=data_as_of, fetched_at=datetime.now(timezone.utc),
                        status="ok" if cache_payload.get("enrichment_source") != "failed" else "unavailable",
                    ))
            session.commit()
        except Exception:
            logger.exception(
                "refresh_enrichment: failed to persist scheme_id=%s amfi=%s",
                scheme.scheme_id, scheme.amfi_code,
            )
            # rollback (not just letting the exception propagate) is what
            # lets a scheme AFTER this one still succeed on the same
            # session — SQLAlchemy invalidates a connection that died
            # mid-query and transparently gets a fresh one from the pool
            # (pool_pre_ping=True) on the session's next use, but only
            # once the session's own error state has been cleared.
            session.rollback()
            continue
        fresh[scheme.scheme_id] = cache_payload

    return fresh
