"""
PortfolioIQ — scheme_resolution.py

Canonical scheme matching (spec section 5.1, 7.2):
  1. ISIN exact match against schemes.isin
  2. Validated AMFI scheme code — fetch mfapi.in for that code, confirm
     its returned ISIN matches the CAS scheme's own ISIN
  3. scheme_aliases lookup (amfi_code/rta_code/name -> canonical scheme_id),
     covering codes retired by an AMC merger/rename (the HSBC/L&T case
     found live in this app's data: AMFI code 120069 froze at its
     Nov-2022 NAV when the fund recoded to 151130)
  4. Normalized-name match with confidence threshold -> needs_review

Step 2/3 recovery (an AMFI code that's stale/wrong) searches mfapi.in by
name and accepts only a candidate whose ISIN matches the CAS's own ISIN
— never a name-similarity guess alone — then PERSISTS that mapping as a
scheme_aliases row (source="isin_recovery") so it isn't re-discovered
via a live mfapi.in search on every future upload of the same fund.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Scheme, SchemeAlias

MFAPI_BASE = "https://api.mfapi.in/mf"
STALE_NAV_DAYS = 10
MAX_ALTERNATE_CANDIDATES = 10


@dataclass
class ResolutionResult:
    scheme: Optional[Scheme]
    method: str  # "isin_exact" | "amfi_validated" | "scheme_alias" | "name_match_needs_review" | "unresolved"
    confidence: str  # "confirmed" | "needs_review" | "none"


def _search_query_from_name(scheme_name: str) -> str:
    return re.split(r"\s*-\s*|\(", scheme_name or "")[0].strip()


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


FETCH_RETRY_ATTEMPTS = 3
FETCH_RETRY_DELAY_SECONDS = 1.0


async def _fetch_json_retrying(client: httpx.AsyncClient, url: str, **kwargs) -> Optional[dict]:
    """mfapi.in is confirmed (live, this session — see enrichment.py's own
    docstring) to fail transiently under normal load, not just when
    genuinely down. Scheme resolution has no fallback data source the
    way enrichment.py's risk ratios do, so a single unretried blip here
    is worse: it would wrongly kick a perfectly real, resolvable fund
    into needs_review/SCHEME_UNRESOLVED purely on bad luck."""
    result = None
    for attempt in range(FETCH_RETRY_ATTEMPTS):
        try:
            resp = await client.get(url, timeout=httpx.Timeout(10.0, connect=5.0), **kwargs)
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            pass
        if attempt < FETCH_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(FETCH_RETRY_DELAY_SECONDS)
    return result


async def _fetch_mfapi_scheme(client: httpx.AsyncClient, code: str) -> Optional[dict]:
    return await _fetch_json_retrying(client, f"{MFAPI_BASE}/{code}")


def _find_or_create_canonical_scheme(
    session: Session, isin: Optional[str], amfi_code: Optional[str], name: str,
    plan: Optional[str], option: Optional[str], asset_class: Optional[str],
) -> Scheme:
    """Every path in resolve_scheme() that confirms an identity ends up
    here: find the existing canonical row for this ISIN, or create one.
    ISIN is the join key — spec 5.1 puts it first in the matching
    priority for a reason: it's the one identifier that survives an AMC
    recode, unlike the AMFI code itself."""
    scheme = None
    if isin:
        scheme = session.execute(select(Scheme).where(Scheme.isin == isin)).scalar_one_or_none()
    if scheme is None:
        scheme = Scheme(
            isin=isin, amfi_code=amfi_code, name=name, plan=plan, option=option,
            asset_class=asset_class, active=True,
        )
        session.add(scheme)
        session.flush()  # assigns scheme_id
    elif amfi_code and scheme.amfi_code != amfi_code:
        # Same fund (same ISIN), but the CAS/mfapi code moved — update
        # the canonical row's own amfi_code to the current one and let
        # the OLD code live on only as an alias (below), not as the
        # scheme's primary identifier going forward.
        scheme.amfi_code = amfi_code
    return scheme


async def resolve_scheme(
    session: Session,
    client: httpx.AsyncClient,
    *,
    cas_isin: Optional[str],
    cas_amfi_code: Optional[str],
    cas_scheme_name: str,
    cas_rta_code: Optional[str] = None,
    plan: Optional[str] = None,
    option: Optional[str] = None,
    asset_class: Optional[str] = None,
) -> ResolutionResult:
    """Priority order exactly as spec 5.1 lists it. Every branch that
    succeeds persists what it learned (scheme row + alias row) so the
    next CAS mentioning the same fund resolves instantly from the DB,
    no network call needed."""

    # 1. ISIN exact match.
    if cas_isin:
        existing = session.execute(select(Scheme).where(Scheme.isin == cas_isin)).scalar_one_or_none()
        if existing:
            return ResolutionResult(existing, "isin_exact", "confirmed")

    # 2. Validated AMFI code: fetch mfapi.in, confirm ITS isin matches the CAS's.
    if cas_amfi_code:
        raw = await _fetch_mfapi_scheme(client, cas_amfi_code)
        if raw:
            meta = raw.get("meta", {})
            mfapi_isin = meta.get("isin_growth") or meta.get("isin_div_reinvestment")
            data = raw.get("data") or []
            latest_date = None
            if data:
                for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
                    try:
                        latest_date = datetime.strptime(data[0]["date"], fmt).date()
                        break
                    except ValueError:
                        continue
            is_fresh = latest_date is not None and (date.today() - latest_date).days <= STALE_NAV_DAYS
            if is_fresh and (not cas_isin or mfapi_isin == cas_isin):
                scheme = _find_or_create_canonical_scheme(
                    session, cas_isin or mfapi_isin, cas_amfi_code, cas_scheme_name, plan, option, asset_class,
                )
                return ResolutionResult(scheme, "amfi_validated", "confirmed")
            # AMFI code returned data but it's stale (frozen NAV) or its
            # ISIN doesn't match — the code the CAS embeds has almost
            # certainly been retired by an AMC merger/rename. Fall through
            # to alias lookup / ISIN-based recovery below rather than
            # trusting it.

    # 3. scheme_aliases lookup — a previously-recovered mapping for this
    # exact stale code, so we don't re-run the mfapi.in search every time.
    if cas_amfi_code:
        alias = session.execute(
            select(SchemeAlias).where(SchemeAlias.alias_type == "amfi_code", SchemeAlias.alias_value == cas_amfi_code)
        ).scalar_one_or_none()
        if alias:
            scheme = session.get(Scheme, alias.scheme_id)
            if scheme:
                return ResolutionResult(scheme, "scheme_alias", "confirmed")

    # Recovery: search mfapi.in by name, accept only an ISIN match, then
    # PERSIST the alias so this is a one-time cost per stale code.
    if cas_isin:
        query = _search_query_from_name(cas_scheme_name)
        if query:
            candidates = await _fetch_json_retrying(client, f"{MFAPI_BASE}/search", params={"q": query}) or []
            for candidate in candidates[:MAX_ALTERNATE_CANDIDATES]:
                code = candidate.get("schemeCode")
                if code is None:
                    continue
                raw = await _fetch_mfapi_scheme(client, str(code))
                if not raw:
                    continue
                meta = raw.get("meta", {})
                if cas_isin not in (meta.get("isin_growth"), meta.get("isin_div_reinvestment")):
                    continue
                scheme = _find_or_create_canonical_scheme(
                    session, cas_isin, str(code), cas_scheme_name, plan, option, asset_class,
                )
                if cas_amfi_code and cas_amfi_code != str(code):
                    session.add(SchemeAlias(
                        alias_type="amfi_code", alias_value=cas_amfi_code, scheme_id=scheme.scheme_id,
                        confidence="confirmed", source="isin_recovery", resolved_at=datetime.now(timezone.utc),
                    ))
                return ResolutionResult(scheme, "amfi_validated", "confirmed")

    # 4. Normalized-name match, needs_review — last resort, only when
    # there's no ISIN to verify against at all. Never silently promoted
    # to "confirmed": a name match alone isn't proof of identity.
    normalized_target = _normalize_name(cas_scheme_name)
    if normalized_target:
        for candidate in session.execute(select(Scheme)).scalars():
            if _normalize_name(candidate.name) == normalized_target:
                return ResolutionResult(candidate, "name_match_needs_review", "needs_review")

    # Nothing matched — create an unconfirmed scheme row so the holding
    # has somewhere to point, but flag it. Caller (ingestion) sets the
    # holding's reconciliation_status to review_required / SCHEME_UNRESOLVED
    # accordingly rather than valuing it.
    scheme = Scheme(
        isin=cas_isin, amfi_code=cas_amfi_code, name=cas_scheme_name, plan=plan,
        option=option, asset_class=asset_class, active=True,
    )
    session.add(scheme)
    session.flush()
    return ResolutionResult(scheme, "unresolved", "needs_review")
