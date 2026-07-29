"""
PortfolioIQ — ingestion.py

CAS import workflow (spec section 6): hash + dedupe the upload, resolve
every scheme to its canonical row, dedupe transactions across
overlapping CAS periods, (re)build FIFO lots, reconcile derived units
against the CAS's own printed close, and record a data-quality status
per holding — never per file, since one statement can have some
holdings reconciled cleanly and others not.

Re-importing an overlapping CAS is idempotent: existing transactions are
skipped by fingerprint, and a holding's lots/allocations are fully
rebuilt from its transaction set every time rather than incrementally
patched — simpler to reason about, and cheap enough for a personal
portfolio's transaction volumes (spec 20.1: "Overlapping CAS upload ->
no duplicate transactions; same result after re-import").
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from fifo import DISPOSAL_TYPES, LOT_CREATING_TYPES, NON_TAXABLE_REDUCTION_TYPES, LotInput, run_fifo
from models import (
    CasUpload, DisposalAllocation, Folio, Holding, PurchaseLot, Transaction,
)
from scheme_resolution import resolve_scheme

RECONCILIATION_TOLERANCE = Decimal("0.001")  # units; casparser's own Decimal rounding noise floor


@dataclass
class HoldingIngestNote:
    holding_id: int
    scheme_name: str
    status: str
    detail: Optional[str] = None


@dataclass
class IngestResult:
    upload_id: Optional[int]
    duplicate: bool
    holdings: list[HoldingIngestNote] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _derive_plan_option(scheme_name: str) -> tuple[str, str]:
    """casparser's Scheme model has no explicit plan/option fields — both
    are embedded in the scheme name string, same as the archived
    portfolio.py's _mode_from_name did for plan alone."""
    lowered = (scheme_name or "").lower()
    plan = "Direct" if "direct" in lowered else "Regular"
    option = "IDCW" if any(k in lowered for k in ("idcw", "dividend")) else "Growth"
    return plan, option


def _asset_class(scheme_type: Optional[str]) -> str:
    t = (scheme_type or "").upper()
    return t if t in ("EQUITY", "DEBT") else "OTHER"


def _get_or_create_folio(session: Session, investor_id: Optional[int], folio_number: str, amc: str) -> Folio:
    normalized = (folio_number or "").strip()
    existing = session.execute(
        select(Folio).where(
            Folio.investor_id == investor_id, Folio.normalized_folio == normalized, Folio.amc == amc,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    folio = Folio(investor_id=investor_id, normalized_folio=normalized, amc=amc)
    session.add(folio)
    session.flush()
    return folio


def _get_or_create_holding(
    session: Session, folio_id: int, scheme_id: int, plan: str, option: str, advisor_arn: Optional[str],
) -> Holding:
    existing = session.execute(
        select(Holding).where(
            Holding.folio_id == folio_id, Holding.scheme_id == scheme_id,
            Holding.plan == plan, Holding.option == option,
        )
    ).scalar_one_or_none()
    if existing:
        if advisor_arn and not existing.advisor_arn:
            existing.advisor_arn = advisor_arn
        return existing
    holding = Holding(
        folio_id=folio_id, scheme_id=scheme_id, plan=plan, option=option, advisor_arn=advisor_arn,
    )
    session.add(holding)
    session.flush()
    return holding


def _as_date(value) -> date:
    return value if isinstance(value, date) else datetime.strptime(str(value), "%Y-%m-%d").date()


def _existing_transaction(session: Session, holding_id: int, txn, occurrence_index: int) -> Optional[Transaction]:
    """Fingerprint lookup (spec 6.3): holding + date + type + amount +
    units + nav + occurrence_index. A description isn't part of the key
    here — two rows can have cosmetically different descriptions
    (e.g. RTA batch IDs) for what's genuinely the same transaction."""
    return session.execute(
        select(Transaction).where(
            Transaction.holding_id == holding_id,
            Transaction.date == _as_date(txn.date),
            Transaction.type == txn.type,
            Transaction.amount == txn.amount,
            Transaction.units == txn.units,
            Transaction.nav == txn.nav,
            Transaction.occurrence_index == occurrence_index,
        )
    ).scalar_one_or_none()


def _persist_transactions(session: Session, holding: Holding, scheme, upload_id: int) -> list[Transaction]:
    """Insert any transaction rows not already present (by fingerprint),
    and return the FULL set for this holding (old + new) so FIFO always
    runs against the complete ledger, not just this upload's rows."""
    # occurrence_index disambiguates genuinely-identical same-day rows
    # (spec 6.3) — counted per (date, type, amount, units, nav) group,
    # in the order casparser itself returned them.
    seen_keys: dict[tuple, int] = {}
    for txn in scheme.transactions:
        key = (_as_date(txn.date), txn.type, txn.amount, txn.units, txn.nav)
        occurrence_index = seen_keys.get(key, 0)
        seen_keys[key] = occurrence_index + 1
        existing = _existing_transaction(session, holding.holding_id, txn, occurrence_index)
        if existing is None:
            session.add(Transaction(
                holding_id=holding.holding_id,
                date=_as_date(txn.date),
                type=txn.type,
                amount=txn.amount,
                units=txn.units,
                nav=txn.nav,
                balance=txn.balance,
                description=txn.description,
                gift_folio=txn.gift_folio,
                source_upload_id=upload_id,
                occurrence_index=occurrence_index,
            ))
    session.flush()
    return list(session.execute(
        select(Transaction).where(Transaction.holding_id == holding.holding_id).order_by(Transaction.date, Transaction.occurrence_index)
    ).scalars())


def _rebuild_fifo(session: Session, holding: Holding, transactions: list[Transaction]) -> tuple[Decimal, dict]:
    """Delete and recreate every lot/allocation for this holding from its
    full transaction set — see module docstring for why full rebuild
    rather than incremental patching."""
    session.query(DisposalAllocation).filter(
        DisposalAllocation.lot_id.in_(
            select(PurchaseLot.lot_id).where(PurchaseLot.holding_id == holding.holding_id)
        )
    ).delete(synchronize_session=False)
    session.query(PurchaseLot).filter(PurchaseLot.holding_id == holding.holding_id).delete(synchronize_session=False)
    session.flush()

    # casparser signs units/amount by cash-flow direction (a REDEMPTION's
    # units and amount are both negative) — fifo.py's model wants plain
    # magnitudes ("100 units acquired" / "120 units disposed"), not
    # signed deltas, so both are normalised to abs() here regardless of
    # transaction type.
    events = [
        LotInput(
            transaction_id=t.transaction_id, date=t.date, type=t.type,
            units=abs(t.units) if t.units is not None else Decimal("0"),
            amount=abs(t.amount) if t.amount is not None else Decimal("0"),
            nav=t.nav,
        )
        for t in transactions
        if t.type in LOT_CREATING_TYPES or t.type in DISPOSAL_TYPES or t.type in NON_TAXABLE_REDUCTION_TYPES
    ]
    result = run_fifo(events)

    lot_rows: list[PurchaseLot] = []
    for lot in result.lots:
        row = PurchaseLot(
            holding_id=holding.holding_id, transaction_id=lot.transaction_id,
            acquired_date=lot.acquired_date, original_units=lot.original_units,
            remaining_units=lot.remaining_units, purchase_nav=lot.purchase_nav,
            purchase_amount=lot.purchase_amount, remaining_cost=lot.remaining_cost,
            stamp_duty=lot.stamp_duty, origin_type=lot.origin_type,
        )
        session.add(row)
        lot_rows.append(row)
    session.flush()

    for alloc in result.allocations:
        session.add(DisposalAllocation(
            disposal_transaction_id=alloc.disposal_transaction_id,
            lot_id=lot_rows[alloc.lot_index].lot_id,
            allocated_units=alloc.allocated_units, allocated_cost=alloc.allocated_cost,
            sale_value=alloc.sale_value, realized_gain=alloc.realized_gain, sold_date=alloc.sold_date,
        ))
    session.flush()

    derived_closing_units = sum((l.remaining_units for l in lot_rows), Decimal("0"))
    return derived_closing_units, result.shortfalls


async def ingest_cas(
    session: Session,
    client: httpx.AsyncClient,
    parsed,  # casparser.types.CASData
    file_bytes: bytes,
    investor_id: Optional[int] = None,
    parse_warnings: Optional[list[str]] = None,
) -> IngestResult:
    file_hash = hashlib.sha256(file_bytes).hexdigest()
    existing_upload = session.execute(select(CasUpload).where(CasUpload.file_hash == file_hash)).scalar_one_or_none()
    if existing_upload:
        return IngestResult(upload_id=existing_upload.upload_id, duplicate=True)

    period = parsed.statement_period
    upload = CasUpload(
        investor_id=investor_id,
        file_hash=file_hash,
        issuer=str(getattr(parsed, "file_type", None) or ""),
        period_from=_as_date(period.from_) if getattr(period, "from_", None) else None,
        period_to=_as_date(period.to) if getattr(period, "to", None) else None,
        warnings=list(parse_warnings or getattr(parsed, "parse_warnings", []) or []),
        raw_parsed_json=parsed.model_dump(mode="json", by_alias=True),
    )
    session.add(upload)
    session.flush()

    result = IngestResult(upload_id=upload.upload_id, duplicate=False, warnings=list(upload.warnings))

    for folio in parsed.folios:
        folio_row = _get_or_create_folio(session, investor_id, folio.folio, folio.amc)
        for scheme in folio.schemes:
            plan, option = _derive_plan_option(scheme.scheme)
            asset_class = _asset_class(scheme.type)
            resolution = await resolve_scheme(
                session, client,
                cas_isin=scheme.isin, cas_amfi_code=scheme.amfi, cas_scheme_name=scheme.scheme,
                cas_rta_code=scheme.rta_code, plan=plan, option=option, asset_class=asset_class,
            )
            holding = _get_or_create_holding(
                session, folio_row.folio_id, resolution.scheme.scheme_id, plan, option, scheme.advisor,
            )

            transactions = _persist_transactions(session, holding, scheme, upload.upload_id)
            derived_closing_units, shortfalls = _rebuild_fifo(session, holding, transactions)

            delta = (scheme.close or Decimal("0")) - derived_closing_units
            has_lot_creating_txn = any(t.type in LOT_CREATING_TYPES for t in transactions)
            opening_nonzero_no_history = (scheme.open or Decimal("0")) != 0 and not has_lot_creating_txn

            # Distinct spec-17 error codes, not one generic "review_required"
            # bucket — SCHEME_UNRESOLVED/FIFO_SHORTFALL/
            # CAS_RECONCILIATION_FAILED each mean a different thing to a
            # consumer of this API and need to stay distinguishable.
            code = None
            if resolution.confidence == "needs_review":
                holding.reconciliation_status = "review_required"
                code = "SCHEME_UNRESOLVED"
                detail = f"Scheme identity not confirmed by ISIN (method={resolution.method}) — needs manual mapping."
            elif shortfalls:
                holding.reconciliation_status = "review_required"
                code = "FIFO_SHORTFALL"
                detail = f"Disposal(s) sold more units than known lots covered ({shortfalls})."
            elif opening_nonzero_no_history:
                holding.reconciliation_status = "incomplete_opening_history"
                code = "INCOMPLETE_OPENING_HISTORY"
                detail = (
                    f"Opening balance of {scheme.open} units has no purchase history in any imported "
                    "CAS — cost basis, XIRR and gains are unavailable for this holding (spec 6.5)."
                )
            elif abs(delta) > RECONCILIATION_TOLERANCE:
                holding.reconciliation_status = "review_required"
                code = "CAS_RECONCILIATION_FAILED"
                detail = f"Derived units ({derived_closing_units}) vs CAS-printed close ({scheme.close}): delta {delta}"
            else:
                holding.reconciliation_status = "reconciled"
                detail = None

            holding.data_quality_code = code
            holding.data_quality_detail = detail

            result.holdings.append(HoldingIngestNote(
                holding_id=holding.holding_id, scheme_name=scheme.scheme,
                status=holding.reconciliation_status, detail=detail,
            ))

    return result
