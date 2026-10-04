"""Portfolio calculations share one dated ledger and explicit data coverage."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
import ledger
import nav_service
import xirr_engine
from models import Folio, Holding, Scheme, Transaction

ZERO = Decimal("0")


@dataclass
class DataQualityFlag:
    code: str
    detail: str


@dataclass
class HoldingMetrics:
    holding_id: int
    folio: str
    amc: str
    scheme_name: str
    isin: Optional[str]
    asset_class: Optional[str]
    advisor_arn: Optional[str]
    balance_units: Decimal
    weighted_purchase_nav: Optional[Decimal]
    current_nav: Optional[Decimal]
    current_nav_date: Optional[date]
    remaining_purchase_value: Optional[Decimal]
    current_value: Optional[Decimal]
    gain: Optional[Decimal]
    weighted_days_held: Optional[int]
    absolute_return_pct: Optional[Decimal]
    xirr_pct: Optional[Decimal]
    reconciliation_status: str
    flags: list[DataQualityFlag]


@dataclass
class PortfolioContext:
    holdings: dict
    folios: dict
    schemes: dict
    lots: dict
    navs: dict
    transactions: dict


def build_context(
    session: Session, holding_ids: list[int], valuation_date: date, *, with_nav=True
) -> PortfolioContext:
    if not holding_ids:
        return PortfolioContext({}, {}, {}, {}, {}, {})
    holdings = {
        h.holding_id: h
        for h in session.scalars(
            select(Holding).where(Holding.holding_id.in_(holding_ids))
        )
    }
    folios = {
        f.folio_id: f
        for f in session.scalars(
            select(Folio).where(
                Folio.folio_id.in_({h.folio_id for h in holdings.values()})
            )
        )
    }
    ids = {h.scheme_id for h in holdings.values()}
    schemes = {
        s.scheme_id: s
        for s in session.scalars(select(Scheme).where(Scheme.scheme_id.in_(ids)))
    }
    transactions = {}
    for t in session.scalars(
        select(Transaction)
        .where(
            Transaction.holding_id.in_(holding_ids), Transaction.date <= valuation_date
        )
        .order_by(
            Transaction.date, Transaction.ledger_position, Transaction.transaction_id
        )
    ):
        transactions.setdefault(t.holding_id, []).append(t)
    navs = (
        nav_service.get_navs_on_or_before(session, list(ids), valuation_date)
        if with_nav
        else {}
    )
    return PortfolioContext(holdings, folios, schemes, {}, navs, transactions)


def units_at(holding, transactions, as_of):
    if as_of is None:
        return ZERO
    opening_date = getattr(holding, "opening_date", None)
    if opening_date and as_of < opening_date:
        return None  # statement does not establish ownership before its coverage
    opening = getattr(holding, "opening_units", None) or ZERO
    return opening + sum(
        (t.units or ZERO for t in transactions if t.date <= as_of), ZERO
    )


def _txn_cash_flow(txn):
    flows = ledger.cashflows([txn])
    return flows[0][1] if flows else None


def compute_holding_metrics(
    session, holding_id, valuation_date, ctx=None, *, calculate_xirr=True
):
    ctx = ctx or build_context(session, [holding_id], valuation_date)
    h = ctx.holdings[holding_id]
    f, s = ctx.folios[h.folio_id], ctx.schemes[h.scheme_id]
    transactions = [
        t for t in ctx.transactions.get(holding_id, []) if t.date <= valuation_date
    ]
    fifo = ledger.fifo_for(transactions)
    lots = [l for l in fifo.lots if l.remaining_units > ZERO]
    flags = []
    code = getattr(h, "data_quality_code", None)
    # FIFO diagnostics are recalculated for the requested date. Persisted
    # import diagnostics may describe a later date or an older engine.
    if code and code not in {"REVERSAL_UNRESOLVED", "FIFO_SHORTFALL"}:
        flags.append(DataQualityFlag(code, h.data_quality_detail or ""))
    units = units_at(h, transactions, valuation_date)
    if units is None:
        flags.append(
            DataQualityFlag(
                "INCOMPLETE_OPENING_HISTORY",
                "The requested date precedes statement coverage.",
            )
        )
        units = ZERO
    cost_valid = not any(f.code in ledger.BLOCKING_CODES for f in flags)
    if fifo.reversal_errors:
        cost_valid = False
        flags.append(
            DataQualityFlag(
                "REVERSAL_UNRESOLVED",
                "A reversed purchase cannot be identified unambiguously.",
            )
        )
    if fifo.shortfalls:
        cost_valid = False
        flags.append(
            DataQualityFlag(
                "FIFO_SHORTFALL", "Purchase history does not cover all disposals."
            )
        )
    if any(l.origin_type == "GIFT_IN" for l in lots) or any(
        t.type == "SEGREGATION" for t in transactions
    ):
        cost_valid = False
        flags.append(
            DataQualityFlag(
                "COST_BASIS_UNAVAILABLE",
                "Transferred units require a verified carried cost basis.",
            )
        )
    if (getattr(h, "opening_units", None) or ZERO) != ZERO:
        cost_valid = False
        if not any(f.code == "INCOMPLETE_OPENING_HISTORY" for f in flags):
            flags.append(
                DataQualityFlag(
                    "INCOMPLETE_OPENING_HISTORY",
                    "Opening units have no acquisition history.",
                )
            )
    cost = sum((l.remaining_cost for l in lots), ZERO) if cost_valid else None
    lot_units = sum((l.remaining_units for l in lots), ZERO)
    weighted_nav = (
        sum((l.remaining_units * l.purchase_nav for l in lots), ZERO) / lot_units
        if cost_valid and lot_units
        else None
    )
    days = (
        int(
            sum(
                (
                    l.remaining_units * (valuation_date - l.acquired_date).days
                    for l in lots
                ),
                ZERO,
            )
            / lot_units
        )
        if cost_valid and lot_units
        else None
    )
    point = ctx.navs.get(h.scheme_id)
    nav = point.nav if point else None
    identity_ok = bool(s.identity_confirmed) and not any(
        f.code == "SCHEME_UNRESOLVED" for f in flags
    )
    coverage_ok = not (
        getattr(h, "opening_date", None) and valuation_date < h.opening_date
    )
    current = (
        units * nav
        if nav is not None and identity_ok and coverage_ok
        else (ZERO if units == ZERO and coverage_ok else None)
    )
    if units and point and (valuation_date - point.resolved_date).days > 7:
        flags.append(
            DataQualityFlag(
                "NAV_STALE", "The latest available NAV is over seven days old."
            )
        )
    if current is None:
        flags.append(
            DataQualityFlag(
                "NAV_UNAVAILABLE",
                "No verified valuation is available for these units on this date.",
            )
        )
    gain = current - cost if current is not None and cost is not None else None
    pct = (
        (gain / cost * 100).quantize(Decimal(".01"))
        if cost and gain is not None
        else None
    )
    irr = None
    if calculate_xirr and not any(f.code in ledger.BLOCKING_CODES for f in flags):
        flows = ledger.cashflows(transactions, valuation_date)
        if current:
            flows.append((valuation_date, current))
        outcome = xirr_engine.xirr(flows)
        irr = outcome.value
        if outcome.reason:
            flags.append(DataQualityFlag("XIRR_NO_SOLUTION", outcome.reason))
    return HoldingMetrics(
        holding_id,
        f.normalized_folio,
        f.amc,
        s.name,
        s.isin,
        s.asset_class,
        h.advisor_arn,
        units,
        weighted_nav,
        nav,
        point.resolved_date if point else None,
        cost,
        current,
        gain,
        days,
        pct,
        irr,
        h.reconciliation_status,
        flags,
    )


def compute_all_holdings(session, valuation_date, include_zero_value=False):
    ids = list(session.scalars(select(Holding.holding_id)))
    ctx = build_context(session, ids, valuation_date)
    rows = [compute_holding_metrics(session, hid, valuation_date, ctx) for hid in ids]
    return rows if include_zero_value else [r for r in rows if r.balance_units > ZERO]


@dataclass
class AggregateTotals:
    invested_value: Optional[Decimal]
    current_value: Optional[Decimal]
    gain: Optional[Decimal]
    absolute_return_pct: Optional[Decimal]
    weighted_days_held: Optional[int]
    xirr_pct: Optional[Decimal]
    known_current_value: Decimal = ZERO
    valued_holdings: int = 0
    total_holdings: int = 0
    closed_holdings: int = 0


def aggregate(session, holdings, valuation_date, ctx=None):
    holdings = list({h.holding_id: h for h in holdings}.values())
    active = [h for h in holdings if h.balance_units != ZERO or h.current_value is None]
    ids = [h.holding_id for h in holdings]
    ctx = ctx or build_context(session, ids, valuation_date)
    known = sum(
        (h.current_value for h in holdings if h.current_value is not None), ZERO
    )
    valued = sum(h.current_value is not None for h in active)
    current = known if valued == len(active) else None
    invested = (
        sum((h.remaining_purchase_value for h in active), ZERO)
        if all(h.remaining_purchase_value is not None for h in active)
        else None
    )
    gain = current - invested if current is not None and invested is not None else None
    pct = (
        (gain / invested * 100).quantize(Decimal(".01"))
        if invested and gain is not None
        else None
    )
    days = (
        int(
            sum(
                (
                    h.current_value * h.weighted_days_held
                    for h in holdings
                    if h.weighted_days_held is not None
                ),
                ZERO,
            )
            / current
        )
        if current
        and all(
            h.weighted_days_held is not None or not h.current_value for h in holdings
        )
        else None
    )
    eligible = not any(
        f.code in ledger.BLOCKING_CODES for h in holdings for f in h.flags
    )
    flows = ledger.cashflows(
        [t for hid in ids for t in ctx.transactions.get(hid, [])], valuation_date
    )
    if current:
        flows.append((valuation_date, current))
    irr = xirr_engine.xirr(flows).value if eligible and current is not None else None
    return AggregateTotals(
        invested,
        current,
        gain,
        pct,
        days,
        irr,
        known,
        valued,
        len(active),
        len(holdings) - len(active),
    )
