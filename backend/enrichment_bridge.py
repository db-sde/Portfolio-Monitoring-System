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
# How many schemes' cache rows to commit per round trip. Small enough
# that a failure (and the individual-retry fallback it triggers) stays
# cheap, large enough to collapse most of the per-scheme commit latency
# that dominated this module's runtime. See _commit_chunk.
COMMIT_CHUNK_SIZE = 10
# How many schemes' NAV histories are held in memory at once.
#
# A/B'd back to back on a real 53-scheme portfolio: batched at 25 ran
# 192.8s at 76MB peak, the same run unbatched 196.5s at 110MB. So this
# buys ~31% of the peak for no measurable time cost. Worth stating
# plainly, because the intuition is that batching must cost latency:
# each batch is one asyncio.gather and a gather only finishes when its
# slowest member does, so extra batches can re-pay the slowest scheme's
# cost. That effect is real but small here, and a larger batch is the
# lever if it ever isn't.
#
# Don't read those absolute numbers as this module's normal speed —
# both runs were made while mfapi.in was actively throttling this
# machine (the same portfolio enriched in 24s earlier the same day).
# They're a controlled comparison of batched vs not, nothing more.
ENRICH_BATCH_SIZE = 25


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


def get_cached_enrichments(session: Session, scheme_ids: list[int]) -> dict[int, dict]:
    """get_cached_enrichment for many schemes in one query. Same gate
    (status "ok" and still fresh); only the round-trip count differs."""
    if not scheme_ids:
        return {}
    rows = session.execute(
        select(EnrichmentCache).where(
            EnrichmentCache.scheme_id.in_(scheme_ids), EnrichmentCache.provider == PROVIDER_KEY,
        )
    ).scalars()
    return {
        row.scheme_id: row.payload
        for row in rows
        if row.status == "ok" and _is_fresh(row.fetched_at)
    }


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

    # One query for every scheme's cache row, not get_cached_enrichment
    # per scheme. Profiling the real 53-scheme run put 135 of its ~176
    # seconds inside psycopg's connection wait, spread over ~545 round
    # trips at ~250ms each — the cost here is the NUMBER of trips to
    # Neon, not the data. Loops of individually-cheap single-row queries
    # are what actually made enrichment slow.
    scheme_ids = [s.scheme_id for s in schemes]
    all_rows = {
        row.scheme_id: row
        for row in session.execute(
            select(EnrichmentCache).where(
                EnrichmentCache.scheme_id.in_(scheme_ids), EnrichmentCache.provider == PROVIDER_KEY,
            )
        ).scalars()
    }

    fresh: dict[int, dict] = {}
    to_fetch: list[Scheme] = []
    for scheme in schemes:
        row = all_rows.get(scheme.scheme_id)
        # Same gate as get_cached_enrichment (status must be "ok", not
        # merely recent) — kept identical deliberately, since a cached
        # FAILURE counting as "already handled" is exactly the bug that
        # made the retry endpoint a silent no-op.
        if row is not None and row.status == "ok" and _is_fresh(row.fetched_at):
            fresh[scheme.scheme_id] = row.payload
        else:
            to_fetch.append(scheme)

    if not to_fetch:
        return fresh

    # Hand enrichment.py the NAV history we already hold, so a scheme
    # whose stored series is current skips its mfapi.in fetch entirely
    # rather than re-downloading ~500KB it already has. Read here rather
    # than inside enrichment.py deliberately: that module owns no DB
    # session and shouldn't start — this bridge is the layer that knows
    # about Postgres, exactly as its docstring describes.
    fetch_ids = [s.scheme_id for s in to_fetch]
    stored_summaries = nav_service.get_stored_nav_summary(session, fetch_ids)

    # Reuse the rows already loaded above rather than re-querying them
    # per scheme in the persist loop. Safe to hold these ORM objects
    # across the loop's per-scheme commits because the session is
    # configured expire_on_commit=False (see db.py), so a commit doesn't
    # invalidate them.
    existing_rows = all_rows

    def _write_row(scheme_id: int, cache_payload: dict, status: str) -> None:
        """Stage one scheme's cache row on the session. No commit — the
        caller decides when to flush a whole chunk (see _commit_chunk)."""
        existing = existing_rows.get(scheme_id)
        if existing is not None:
            existing.payload = cache_payload
            existing.data_as_of = date.today()
            existing.fetched_at = datetime.now(timezone.utc)
            existing.status = status
        else:
            row = EnrichmentCache(
                scheme_id=scheme_id, provider=PROVIDER_KEY, payload=cache_payload,
                data_as_of=date.today(), fetched_at=datetime.now(timezone.utc), status=status,
            )
            session.add(row)
            existing_rows[scheme_id] = row

    def _commit_chunk(chunk: list[tuple[int, dict, str]]) -> None:
        """Commit a batch of staged rows in ONE round trip, falling back
        to one-at-a-time if that fails.

        Committing per scheme was costing ~1.1s each against Neon (a
        BEGIN/UPDATE/COMMIT round trip apiece at ~250ms), about 59s of a
        53-scheme run, purely in latency. Batching alone would be a
        straight trade of durability for speed — and a batch commit that
        fails would lose every scheme in it, which is exactly the
        regression a previous attempt at batching here caused (zero rows
        written, silently). The fallback is what makes it safe rather
        than a gamble: if the batch fails, roll back, then re-apply and
        commit each scheme on its own, so one bad row costs only itself
        and the rest still persist. Fast in the normal case, no worse
        than the old behaviour in the bad one.

        Re-reading each row inside the fallback matters: a rollback
        reverts the in-memory ORM objects too (and expunges newly-added
        ones), so `existing_rows` can't be trusted afterwards and the
        retry has to start from what's actually in the database."""
        if not chunk:
            return
        try:
            session.commit()
            return
        except Exception:
            logger.exception(
                "refresh_enrichment: batch commit of %d schemes failed — retrying individually", len(chunk),
            )
            session.rollback()
        for scheme_id, cache_payload, status in chunk:
            try:
                existing_rows[scheme_id] = session.execute(
                    select(EnrichmentCache).where(
                        EnrichmentCache.scheme_id == scheme_id, EnrichmentCache.provider == PROVIDER_KEY,
                    )
                ).scalar_one_or_none()
                _write_row(scheme_id, cache_payload, status)
                session.commit()
            except Exception:
                logger.exception("refresh_enrichment: failed to persist scheme_id=%s", scheme_id)
                session.rollback()

    # Staged rows not yet committed, flushed every COMMIT_CHUNK_SIZE.
    pending: list[tuple[int, dict, str]] = []

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
        payload = {"enrichment_source": "failed", "reason": reason}
        _write_row(scheme.scheme_id, payload, "unavailable")
        pending.append((scheme.scheme_id, payload, "unavailable"))

    def _persist_batch(batch: list[Scheme], results: dict[str, dict], batch_start: int) -> None:
        nonlocal pending
        for offset, scheme in enumerate(batch):
            _stage(f"persisting_{batch_start + offset}_of_{len(to_fetch)}_scheme_id_{scheme.scheme_id}")
            if not scheme.amfi_code:
                # No AMFI code at all (a SCHEME_UNRESOLVED holding — an
                # old/obscure fund ingestion couldn't confirm an identity
                # for) is never in `targets`, so `results` has nothing for
                # it — not "not fetched yet", but "can never be fetched."
                _persist_unavailable(scheme, "no AMFI code resolved for this scheme")
                continue
            payload = results.get(scheme.amfi_code)
            if payload is None:
                # A resolvable scheme whose fetch still came back empty —
                # it either raised inside enrich_schemes's own gather()
                # (return_exceptions=True catches it there, but the result
                # is simply absent from `results`, indistinguishable here
                # from never having been fetched) or genuinely returned
                # nothing. Recorded as unavailable rather than silently
                # dropped, so it counts as attempted and gets another
                # chance next run instead of being invisible until then.
                _persist_unavailable(scheme, "fetch returned no data for this scheme")
                continue
            try:
                # No SAVEPOINT here, deliberately. One was added back when
                # this loop shared a single transaction across every
                # scheme, to stop one scheme's rollback discarding earlier
                # schemes' uncommitted work. _commit_chunk's individual-
                # retry fallback now covers that directly, and a SAVEPOINT
                # plus its RELEASE cost two extra round trips per scheme
                # (~250ms each against Neon) to defend against it.
                #
                # payload is looked up from `results` by amfi_code, and two
                # DIFFERENT schemes can legitimately share one amfi_code —
                # mfapi.in issues a single code per fund with two ISINs
                # (isin_growth vs isin_div_reinvestment) for a dividend/
                # IDCW plan's payout vs reinvestment variants. Both scheme
                # rows then get the SAME dict object out of `results`. A
                # `.pop()` here used to mutate that shared dict in place,
                # so whichever scheme was processed first drained
                # `_nav_history` for itself and left the second scheme's
                # copy permanently empty — reproduced live: scheme_id 1520
                # ended up with all 5021 NAV points, scheme_id 1524 (same
                # amfi_code 100120) got zero, so its holding valued at
                # Rs.0 despite showing enrichment status "ok". `.get()`
                # (never mutating the shared dict) plus building a
                # separate dict for what gets persisted keeps every scheme
                # sharing a code independent.
                nav_history = payload.get("_nav_history") or []
                if nav_history:
                    points = []
                    for row in nav_history:
                        d = enrichment._parse_nav_date(row["date"])
                        if d is not None:
                            points.append((d, Decimal(str(row["nav"]))))
                    # stored_summary turns this from "rewrite the whole
                    # history" into "append whatever's actually new" —
                    # see store_nav_points. Usually nothing on a re-run.
                    nav_service.store_nav_points(
                        session, scheme.scheme_id, points,
                        stored_summary=stored_summaries.get(scheme.scheme_id),
                    )

                cache_payload = {k: v for k, v in payload.items() if k != "_nav_history"}
                status = "ok" if cache_payload.get("enrichment_source") != "failed" else "unavailable"
                _write_row(scheme.scheme_id, cache_payload, status)
                pending.append((scheme.scheme_id, cache_payload, status))
            except Exception:
                logger.exception(
                    "refresh_enrichment: failed to stage scheme_id=%s amfi=%s",
                    scheme.scheme_id, scheme.amfi_code,
                )
                # rollback (not just letting the exception propagate) is
                # what lets a scheme AFTER this one still succeed on the
                # same session — SQLAlchemy invalidates a connection that
                # died mid-query and transparently gets a fresh one from
                # the pool (pool_pre_ping=True) on the session's next use,
                # but only once the session's own error state is cleared.
                # Any rows staged alongside this one are reverted by that
                # rollback too, so they're dropped from `pending` rather
                # than left there claiming to have been written.
                session.rollback()
                pending.clear()
                continue
            fresh[scheme.scheme_id] = cache_payload

            if len(pending) >= COMMIT_CHUNK_SIZE:
                _commit_chunk(pending)
                pending = []

    # Processed in batches, and this is a memory bound rather than a
    # speed one. Loading every scheme's NAV history at once measured
    # 109MB peak for a real 53-scheme portfolio (199,265 points), and
    # enrich_schemes hands back a SECOND copy of the same series inside
    # each payload's _nav_history — so the whole-portfolio version held
    # roughly 200MB+ of NAV data simultaneously, on top of Python,
    # FastAPI and SQLAlchemy, inside a 512MB container. That is close
    # enough to the ceiling to risk the OOM killer taking the process
    # down mid-run, which would look exactly like the "enrichment
    # silently did nothing" failures this module has a long history of.
    # Batching keeps the working set to one batch's worth (~25MB) and
    # lets each batch's histories and payloads be freed before the next
    # is read. It costs nothing in round trips — the queries are still
    # batched, just per chunk instead of per portfolio.
    for batch_start in range(0, len(to_fetch), ENRICH_BATCH_SIZE):
        batch = to_fetch[batch_start:batch_start + ENRICH_BATCH_SIZE]
        batch_ids = [s.scheme_id for s in batch]
        stored_histories = nav_service.get_nav_histories(session, batch_ids)
        targets = []
        for s in batch:
            if not s.amfi_code:
                continue
            target = {"amfi": s.amfi_code, "isin": s.isin, "scheme": s.name}
            history = stored_histories.get(s.scheme_id)
            if history:
                target["nav_history"] = history
            # Carry forward a category we already learned. It's stable
            # fund metadata, and knowing it lets _enrich_one skip the
            # small /latest metadata fetch it would otherwise make when
            # NAV comes from cache.
            previous = existing_rows.get(s.scheme_id)
            if previous is not None and (previous.payload or {}).get("category"):
                target["category"] = previous.payload["category"]
            targets.append(target)
        _stage(f"fetching_{batch_start + len(batch)}_of_{len(to_fetch)}_schemes")
        results = await enrichment.enrich_schemes(targets)  # {amfi_code: payload}
        _persist_batch(batch, results, batch_start)
        # Drop this batch's NAV data before reading the next one.
        stored_histories = None
        results = None

    _commit_chunk(pending)
    return fresh
