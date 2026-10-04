"""Fund identity is confirmed by ISIN, separately from NAV freshness."""

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy import select, delete
from models import Scheme, SchemeAlias, NavCache, EnrichmentCache
from provider import fetch_json

MFAPI_BASE = "https://api.mfapi.in/mf"
MAX_ALTERNATE_CANDIDATES = 10


@dataclass
class ResolutionResult:
    scheme: Optional[Scheme]
    method: str
    confidence: str
    raw: Optional[dict] = None
    cache_reset: bool = False


def key_for(s):
    return s.isin or f"{s.amfi}:{s.scheme}"


def matches(raw, isin):
    meta = (raw or {}).get("meta", {})
    return bool(
        isin and isin in (meta.get("isin_growth"), meta.get("isin_div_reinvestment"))
    )


def latest_date(raw):
    for row in (raw or {}).get("data", [])[:1]:
        for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(row["date"], fmt).date()
            except (ValueError, KeyError, TypeError):
                pass
    return date.min


def build_scheme_cache(session):
    return {
        s.isin: s
        for s in session.scalars(select(Scheme).where(Scheme.isin.isnot(None)))
    }


async def prefetch_mfapi_schemes(client, amfi_codes):
    codes = list(dict.fromkeys(c for c in amfi_codes if c))
    results = await asyncio.gather(
        *(fetch_json(client, f"{MFAPI_BASE}/{c}") for c in codes)
    )
    return dict(zip(codes, results))


async def prepare_schemes(client, parsed, known):
    """All network work completes before the atomic database replacement."""
    unique = {key_for(s): s for f in parsed.folios for s in f.schemes}
    to_fetch = [s for s in unique.values() if not (s.isin in known and known[s.isin])]
    raw_by_code = await prefetch_mfapi_schemes(client, [s.amfi for s in to_fetch])

    async def resolve(s):
        raw = raw_by_code.get(s.amfi)
        if matches(raw, s.isin) and (date.today() - latest_date(raw)).days <= 10:
            return {"code": s.amfi, "raw": raw}
        if not s.isin:
            return None
        query = re.split(r"\s*-\s*|\(", s.scheme or "")[0].strip()
        candidates = (
            await fetch_json(client, f"{MFAPI_BASE}/search", params={"q": query}) or []
        )
        codes = [
            str(c["schemeCode"])
            for c in candidates[:MAX_ALTERNATE_CANDIDATES]
            if "schemeCode" in c
        ]
        values = await prefetch_mfapi_schemes(client, codes)
        valid = [(c, r) for c, r in values.items() if matches(r, s.isin)]
        if matches(raw, s.isin):
            valid.append((s.amfi, raw))
        if not valid:
            return None
        # A retired fund can have a valid identity and old historical NAV.
        code, raw = max(valid, key=lambda pair: latest_date(pair[1]))
        return {"code": code, "raw": raw}

    resolved = await asyncio.gather(*(resolve(s) for s in to_fetch))
    raw_by_code["__resolved__"] = {key_for(s): r for s, r in zip(to_fetch, resolved)}
    return raw_by_code


async def resolve_scheme(
    session,
    client,
    *,
    cas_isin,
    cas_amfi_code,
    cas_scheme_name,
    cas_rta_code=None,
    plan=None,
    option=None,
    asset_class=None,
    prefetched_mfapi=None,
    scheme_cache=None,
    offline=False,
):
    cache = scheme_cache if scheme_cache is not None else build_scheme_cache(session)
    existing = cache.get(cas_isin) if cas_isin else None
    if existing and existing.identity_confirmed:
        return ResolutionResult(existing, "isin_exact", "confirmed")
    prepared = prefetched_mfapi or {}
    key = cas_isin or f"{cas_amfi_code}:{cas_scheme_name}"
    chosen = prepared.get("__resolved__", {}).get(key)
    raw = prepared.get(cas_amfi_code)
    if chosen:
        code, raw = chosen["code"], chosen["raw"]
    else:
        code = cas_amfi_code
    if not offline and not matches(raw, cas_isin):
        from types import SimpleNamespace

        s = SimpleNamespace(isin=cas_isin, amfi=cas_amfi_code, scheme=cas_scheme_name)
        result = await prepare_schemes(
            client, SimpleNamespace(folios=[SimpleNamespace(schemes=[s])]), {}
        )
        chosen = result["__resolved__"].get(key)
        code, raw = (chosen["code"], chosen["raw"]) if chosen else (cas_amfi_code, None)
    confirmed = matches(raw, cas_isin)
    cache_reset = bool(existing and confirmed and not existing.identity_confirmed)
    if cache_reset:
        # Legacy histories may belong to a retired/wrong AMFI code. Rebuild once.
        session.execute(
            delete(NavCache).where(NavCache.scheme_id == existing.scheme_id)
        )
        session.execute(
            delete(EnrichmentCache).where(
                EnrichmentCache.scheme_id == existing.scheme_id
            )
        )
    scheme = existing or Scheme(
        isin=cas_isin,
        name=cas_scheme_name,
        plan=plan,
        option=option,
        asset_class=asset_class,
        active=True,
    )
    scheme.amfi_code = str(code) if code else None
    scheme.identity_confirmed = confirmed
    scheme.rta_code = cas_rta_code
    session.add(scheme)
    session.flush()
    if cas_isin:
        cache[cas_isin] = scheme
    if confirmed and cas_amfi_code and str(code) != str(cas_amfi_code):
        alias = session.scalar(
            select(SchemeAlias).where(
                SchemeAlias.alias_type == "amfi_code",
                SchemeAlias.alias_value == str(cas_amfi_code),
            )
        )
        if alias is None:
            session.add(
                SchemeAlias(
                    alias_type="amfi_code",
                    alias_value=str(cas_amfi_code),
                    scheme_id=scheme.scheme_id,
                    confidence="confirmed",
                    source="isin_recovery",
                    resolved_at=datetime.now(timezone.utc),
                )
            )
    return ResolutionResult(
        scheme,
        "amfi_validated" if confirmed else "unresolved",
        "confirmed" if confirmed else "needs_review",
        raw if confirmed else None,
        cache_reset,
    )
