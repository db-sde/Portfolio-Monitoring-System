"""
PortfolioIQ — gains_service_db.py

Capital gains (spec section 12), Postgres-backed. Realized gains come
straight from disposal_allocations (spec: "one row per FIFO disposal
allocation, not merely one redemption order") — no separate gains cache
table, so there's exactly one FIFO computation in the app (fifo.py),
not two.

Tax CLASSIFICATION (LTCG/STCG thresholds, grandfathering at 31-Jan-2018,
the 23-Jul-2024 LTCG-regime split, debt-fund indexation) is deliberately
NOT reimplemented here — spec section 22: "Do not hard-code tax rates
in UI code... delegate to a configurable tax engine or the verified
casparser gains module." casparser.analysis.gains.GainEntry already
carries every one of those rules as pure computed properties over
(purchase_date, purchase_value, sale_date, sale_value, stamp_duty, stt,
units, fund type/ISIN) — so each disposal_allocation row here is wrapped
in a GainEntry and its properties (gain_type, ltcg, stcg, coa, ...) are
read directly, rather than re-deriving tax rules from scratch.

This is why disposal_allocations is populated by THIS app's own fifo.py
rather than by running casparser's own FIFOUnits/CapitalGainsReport
end-to-end: that class raises IncompleteCASError for the WHOLE CAS the
moment any one scheme has a non-zero opening balance (spec 6.5 instead
wants that handled per-holding, not as an all-or-nothing failure) —
fifo.py already does the per-holding exclusion correctly, and GainEntry
alone (no FIFOUnits/CapitalGainsReport) is used purely for its tax math.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from casparser.analysis.gains import CapitalGainsReport, Fund, GainEntry
from casparser.analysis.utils import get_fin_year
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import DisposalAllocation, Folio, Holding, PurchaseLot, Scheme, Transaction


@dataclass
class RealizedGainRow:
    fy: str
    scheme_name: str
    folio: str
    isin: Optional[str]
    fund_type: str
    advisor_arn: Optional[str]
    acquired_date: date
    sold_date: date
    units: Decimal
    acquisition_value: Decimal
    sale_value: Decimal
    gain: Decimal
    gain_type: str  # "LTCG" | "STCG"
    ltcg: Decimal
    stcg: Decimal
    ltcg_taxable: Decimal


def _asset_class_for_tax(asset_class: Optional[str]) -> str:
    """GainEntry.gain_type indexes into a dict keyed by FundType.EQUITY.name
    / FundType.DEBT.name — an "OTHER"/unclassified scheme has no defined
    LTCG holding-period rule, so it's treated as EQUITY's 12-month
    threshold (the more common case) rather than raising. Flagged via the
    caller's own data-quality reporting, not silently guessed at without
    a trace."""
    return asset_class if asset_class in ("EQUITY", "DEBT") else "EQUITY"


@dataclass
class _RawGain:
    entry: GainEntry
    holding: Holding
    scheme: Scheme
    folio: Folio


@dataclass
class ExcludedGiftDisposal:
    """A disposal that consumed units originally received as a gift (spec
    8.3) — no gain is computed for these units since the donor's cost
    basis/holding period (Sec 49(1)/2(42A)) isn't in this CAS. Surfaced
    explicitly rather than silently missing from the gains totals."""
    scheme_name: str
    folio: str
    sold_date: date
    units: Decimal


def _gain_entries(
    session: Session, holding_ids: Optional[list[int]] = None,
) -> tuple[list[_RawGain], list[ExcludedGiftDisposal]]:
    query = select(DisposalAllocation, PurchaseLot, Holding, Scheme, Folio).join(
        PurchaseLot, DisposalAllocation.lot_id == PurchaseLot.lot_id
    ).join(
        Holding, PurchaseLot.holding_id == Holding.holding_id
    ).join(
        Scheme, Holding.scheme_id == Scheme.scheme_id
    ).join(
        Folio, Holding.folio_id == Folio.folio_id
    )
    if holding_ids is not None:
        query = query.where(Holding.holding_id.in_(holding_ids))

    out: list[_RawGain] = []
    excluded: list[ExcludedGiftDisposal] = []
    for alloc, lot, holding, scheme, folio in session.execute(query).all():
        if lot.origin_type == "GIFT_IN":
            excluded.append(ExcludedGiftDisposal(
                scheme_name=scheme.name, folio=folio.normalized_folio,
                sold_date=alloc.sold_date, units=alloc.allocated_units,
            ))
            continue
        fund_type = _asset_class_for_tax(scheme.asset_class)
        fund = Fund(scheme=scheme.name, folio=folio.normalized_folio, isin=scheme.isin or "", type=fund_type)
        lot_stamp_share = (
            lot.stamp_duty * (alloc.allocated_units / lot.original_units) if lot.original_units else Decimal("0")
        )
        entry = GainEntry(
            fy=get_fin_year(alloc.sold_date),
            fund=fund,
            type=fund_type,
            purchase_date=lot.acquired_date,
            purchase_nav=lot.purchase_nav,
            purchase_value=alloc.allocated_cost - lot_stamp_share,
            stamp_duty=lot_stamp_share,
            sale_date=alloc.sold_date,
            sale_nav=(alloc.sale_value / alloc.allocated_units) if alloc.allocated_units else Decimal("0"),
            sale_value=alloc.sale_value,
            stt=Decimal("0"),  # STT is tracked as its own transaction row (STT_TAX), not on the allocation
            units=alloc.allocated_units,
        )
        out.append(_RawGain(entry=entry, holding=holding, scheme=scheme, folio=folio))
    return out, excluded


def realized_gains(
    session: Session, holding_ids: Optional[list[int]] = None,
) -> tuple[list[RealizedGainRow], list[ExcludedGiftDisposal]]:
    raw_gains, excluded = _gain_entries(session, holding_ids)
    rows: list[RealizedGainRow] = []
    for raw in raw_gains:
        entry, holding, scheme, folio = raw.entry, raw.holding, raw.scheme, raw.folio
        rows.append(RealizedGainRow(
            fy=entry.fy, scheme_name=scheme.name, folio=folio.normalized_folio, isin=scheme.isin,
            fund_type=entry.type, advisor_arn=holding.advisor_arn,
            acquired_date=entry.purchase_date, sold_date=entry.sale_date, units=entry.units,
            acquisition_value=entry.acquisition_value, sale_value=entry.sale_value, gain=entry.gain,
            gain_type=entry.gain_type.name, ltcg=entry.ltcg, stcg=entry.stcg, ltcg_taxable=entry.ltcg_taxable,
        ))
    return rows, excluded


def generate_112a_csv(session: Session, fy: str, holding_ids: Optional[list[int]] = None) -> str:
    """The OFFICIAL Schedule 112A format (14/15 columns incl. grandfathered
    FMV treatment and the 23-Jul-2024 LTCG-regime transfer flag) — spec
    12.2: "must use... the parser's verified export logic," not a
    hand-rolled CSV. CapitalGainsReport.generate_112a_csv_data only
    depends on self.gains, so a bare instance with just _gains populated
    (bypassing __init__/process_data(), which is what raises
    IncompleteCASError for the whole CAS on any non-zero opening balance
    — exactly what this module's per-holding approach avoids) is enough
    to reuse it correctly."""
    raw_gains, _excluded = _gain_entries(session, holding_ids)
    report = CapitalGainsReport.__new__(CapitalGainsReport)
    report._gains = [raw.entry for raw in raw_gains]
    return report.generate_112a_csv_data(fy)


def available_fys(rows: list[RealizedGainRow]) -> list[str]:
    return sorted({r.fy for r in rows}, reverse=True)


@dataclass
class FySummary:
    fy: str
    stcg: Decimal
    ltcg: Decimal
    net: Decimal


def fy_summary(rows: list[RealizedGainRow], fy: str) -> FySummary:
    """Spec 12.1: every summary card is scoped to the SELECTED FY, not
    all years — the bug this replaces had STCG/LTCG/Net summing across
    every FY while only the transaction table beneath was FY-filtered."""
    fy_rows = [r for r in rows if r.fy == fy]
    stcg = sum((r.stcg for r in fy_rows), Decimal("0"))
    ltcg = sum((r.ltcg for r in fy_rows), Decimal("0"))
    return FySummary(fy=fy, stcg=stcg, ltcg=ltcg, net=stcg + ltcg)


@dataclass
class GiftRow:
    fy: str
    scheme_name: str
    isin: Optional[str]
    direction: str  # "IN" | "OUT"
    date: date
    units: Decimal
    nav: Optional[Decimal]
    value: Optional[Decimal]
    counterparty_folio: Optional[str]


def gifts(session: Session, holding_ids: Optional[list[int]] = None) -> list[GiftRow]:
    """GIFT_IN/GIFT_OUT transactions, informational only (spec 8.3: a
    gift-out isn't a sale — Sec 47(iii) — and a gift-in needs the
    donor's own cost basis/holding period, Sec 49(1)/2(42A), which a
    single CAS never has) — never part of the gains totals above."""
    query = select(Transaction, Holding, Scheme).join(
        Holding, Transaction.holding_id == Holding.holding_id
    ).join(
        Scheme, Holding.scheme_id == Scheme.scheme_id
    ).where(Transaction.type.in_(("GIFT_IN", "GIFT_OUT")))
    if holding_ids is not None:
        query = query.where(Holding.holding_id.in_(holding_ids))

    out: list[GiftRow] = []
    for txn, holding, scheme in session.execute(query).all():
        out.append(GiftRow(
            fy=get_fin_year(txn.date), scheme_name=scheme.name, isin=scheme.isin,
            direction="IN" if txn.type == "GIFT_IN" else "OUT", date=txn.date,
            units=abs(txn.units) if txn.units is not None else Decimal("0"),
            nav=txn.nav, value=abs(txn.amount) if txn.amount is not None else None,
            counterparty_folio=txn.gift_folio,
        ))
    return sorted(out, key=lambda g: (g.fy, g.scheme_name, g.date))
