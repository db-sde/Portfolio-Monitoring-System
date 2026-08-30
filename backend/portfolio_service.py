"""
PortfolioIQ — portfolio_service.py

Holding-level metrics (spec section 9, 11.1) computed from the FIFO lot
engine's persisted state — never from CAS-printed valuation.value/nav
(spec 10.1, 22: "Do not use CAS valuation NAV/value for dashboard,
portfolio, snapshot closing value or exposure").

This is the one place balance units, weighted purchase NAV, current
value, gain, days held, absolute return and XIRR are computed for a
holding — Dashboard/Portfolio/Exposure/Summary all call into this
rather than each re-deriving their own version, so there's exactly one
definition of "current value" in the whole app.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import nav_service
import xirr_engine
from models import Folio, Holding, PurchaseLot, Scheme, Transaction

ZERO = Decimal("0")

# Spec 8.1 / 9.5 sign convention — money OUT of the investor's pocket is
# negative, money IN is positive, for scheme-level XIRR cash flows.
MONEY_OUT_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}
MONEY_IN_TYPES = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER", "DIVIDEND_PAYOUT"}
# No external cash-flow impact for scheme XIRR: DIVIDEND_REINVEST (units
# added, no net external flow), STAMP_DUTY_TAX/STT_TAX/TDS_TAX (already
# folded into acquisition/disposal cost, not a separate flow event here),
# SEGREGATION/GIFT_IN/GIFT_OUT/MISC/UNKNOWN (spec 8.3: excluded without
# linked donor/cost data). REVERSAL is NOT in this "no impact" group —
# despite what an earlier version of this comment claimed ("handled by
# the transaction it reverses no longer being counted"), nothing ever
# actually excluded that transaction; a bounced SIP's original
# PURCHASE_SIP outflow is always counted normally. REVERSAL needs its
# own offsetting flow instead, handled explicitly below (not via this
# set, since its amount is already negative and would double-negate
# through the abs() both sets apply).


@dataclass
class DataQualityFlag:
    code: str  # NAV_UNAVAILABLE | SCHEME_UNRESOLVED | CAS_RECONCILIATION_FAILED | INCOMPLETE_OPENING_HISTORY | FIFO_SHORTFALL | XIRR_NO_SOLUTION
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
    remaining_purchase_value: Decimal
    current_value: Decimal
    gain: Optional[Decimal]
    weighted_days_held: Optional[int]
    absolute_return_pct: Optional[Decimal]
    xirr_pct: Optional[Decimal]
    reconciliation_status: str
    flags: list[DataQualityFlag]


def _txn_cash_flow(txn: Transaction) -> Optional[Decimal]:
    if txn.type in MONEY_OUT_TYPES:
        return -abs(txn.amount) if txn.amount is not None else None
    if txn.type in MONEY_IN_TYPES:
        return abs(txn.amount) if txn.amount is not None else None
    if txn.type == "REVERSAL":
        return -txn.amount if txn.amount is not None else None
    return None


@dataclass
class PortfolioContext:
    """Everything compute_holding_metrics needs for a whole set of
    holdings, loaded in a fixed handful of queries instead of six per
    holding.

    Why this exists: computing one page of holdings issued ~6 round
    trips each (holding, folio, scheme, lots, NAV, transactions), and
    profiling a real 65-holding portfolio put 106 of its 107 seconds
    inside psycopg's connection wait across 383 of them — pure network
    latency to Neon at ~277ms a trip, with essentially no computation
    behind it. The per-holding functions were each individually cheap
    and obviously correct, which is exactly why this stayed invisible:
    nothing is slow until you count the round trips.
    """
    holdings: dict[int, Holding]
    folios: dict[int, Folio]
    schemes: dict[int, Scheme]
    lots: dict[int, list[PurchaseLot]]
    navs: dict[int, nav_service.NavPoint]
    transactions: dict[int, list[Transaction]]


def build_context(session: Session, holding_ids: list[int], valuation_date: date) -> PortfolioContext:
    """Six batched queries for any number of holdings. Transactions are
    included because the XIRR path needs them; they're grouped by
    holding here so that path stays a dict lookup."""
    if not holding_ids:
        return PortfolioContext({}, {}, {}, {}, {}, {})

    holdings = {
        h.holding_id: h
        for h in session.execute(select(Holding).where(Holding.holding_id.in_(holding_ids))).scalars()
    }
    folio_ids = {h.folio_id for h in holdings.values()}
    scheme_ids = {h.scheme_id for h in holdings.values()}
    folios = {
        f.folio_id: f for f in session.execute(select(Folio).where(Folio.folio_id.in_(folio_ids))).scalars()
    }
    schemes = {
        s.scheme_id: s for s in session.execute(select(Scheme).where(Scheme.scheme_id.in_(scheme_ids))).scalars()
    }

    lots: dict[int, list[PurchaseLot]] = {}
    for lot in session.execute(
        select(PurchaseLot).where(PurchaseLot.holding_id.in_(holding_ids), PurchaseLot.remaining_units > 0)
    ).scalars():
        lots.setdefault(lot.holding_id, []).append(lot)

    transactions: dict[int, list[Transaction]] = {}
    for txn in session.execute(
        select(Transaction).where(Transaction.holding_id.in_(holding_ids)).order_by(Transaction.date)
    ).scalars():
        transactions.setdefault(txn.holding_id, []).append(txn)

    navs = nav_service.get_navs_on_or_before(session, list(scheme_ids), valuation_date)
    return PortfolioContext(holdings, folios, schemes, lots, navs, transactions)


def compute_holding_metrics(
    session: Session, holding_id: int, valuation_date: date, ctx: Optional[PortfolioContext] = None,
) -> HoldingMetrics:
    """ctx: optional prefetched data from build_context. Purely a
    performance path — with it, this function makes no queries at all;
    without it, it loads exactly what it always did, so single-holding
    callers keep working unchanged."""
    if ctx is not None:
        holding = ctx.holdings.get(holding_id) or session.get(Holding, holding_id)
        folio = ctx.folios.get(holding.folio_id) or session.get(Folio, holding.folio_id)
        scheme = ctx.schemes.get(holding.scheme_id) or session.get(Scheme, holding.scheme_id)
        lots = ctx.lots.get(holding_id, [])
    else:
        holding = session.get(Holding, holding_id)
        folio = session.get(Folio, holding.folio_id)
        scheme = session.get(Scheme, holding.scheme_id)
        lots = list(session.execute(
            select(PurchaseLot).where(PurchaseLot.holding_id == holding_id, PurchaseLot.remaining_units > 0)
        ).scalars())
    flags: list[DataQualityFlag] = []

    balance_units = sum((l.remaining_units for l in lots), ZERO)
    remaining_purchase_value = sum((l.remaining_cost for l in lots), ZERO)
    weighted_purchase_nav = (
        sum((l.remaining_units * l.purchase_nav for l in lots), ZERO) / balance_units
        if balance_units > 0 else None
    )
    weighted_days_held = (
        int(sum((l.remaining_units * (valuation_date - l.acquired_date).days for l in lots), ZERO) / balance_units)
        if balance_units > 0 else None
    )

    nav_point = (
        ctx.navs.get(holding.scheme_id) if ctx is not None
        else nav_service.get_nav_on_or_before(session, holding.scheme_id, valuation_date)
    )
    current_nav = nav_point.nav if nav_point else None
    current_nav_date = nav_point.resolved_date if nav_point else None
    if current_nav is None and balance_units > 0:
        flags.append(DataQualityFlag("NAV_UNAVAILABLE", "No NAV on or before the requested date for this scheme."))

    current_value = (balance_units * current_nav) if (current_nav is not None) else ZERO
    gain = (current_value - remaining_purchase_value) if remaining_purchase_value else None
    absolute_return_pct = (
        (gain / remaining_purchase_value * 100).quantize(Decimal("0.01"))
        if remaining_purchase_value and gain is not None else None
    )

    if holding.data_quality_code:
        flags.append(DataQualityFlag(holding.data_quality_code, holding.data_quality_detail or ""))

    xirr_pct = None
    blocking_codes = {"CAS_RECONCILIATION_FAILED", "INCOMPLETE_OPENING_HISTORY", "SCHEME_UNRESOLVED", "FIFO_SHORTFALL"}
    if not any(f.code in blocking_codes for f in flags):
        transactions = (
            ctx.transactions.get(holding_id, []) if ctx is not None
            else list(session.execute(
                select(Transaction).where(Transaction.holding_id == holding_id).order_by(Transaction.date)
            ).scalars())
        )
        cashflows: list[tuple[date, Decimal]] = []
        for t in transactions:
            cf = _txn_cash_flow(t)
            if cf is not None:
                cashflows.append((t.date, cf))
        if balance_units > 0 and current_nav is not None:
            cashflows.append((valuation_date, current_value))
        outcome = xirr_engine.xirr(cashflows)
        xirr_pct = outcome.value
        if outcome.value is None and outcome.reason:
            flags.append(DataQualityFlag("XIRR_NO_SOLUTION", outcome.reason))

    return HoldingMetrics(
        holding_id=holding_id, folio=folio.normalized_folio, amc=folio.amc,
        scheme_name=scheme.name, isin=scheme.isin, asset_class=scheme.asset_class,
        advisor_arn=holding.advisor_arn, balance_units=balance_units,
        weighted_purchase_nav=weighted_purchase_nav, current_nav=current_nav,
        current_nav_date=current_nav_date, remaining_purchase_value=remaining_purchase_value,
        current_value=current_value, gain=gain, weighted_days_held=weighted_days_held,
        absolute_return_pct=absolute_return_pct, xirr_pct=xirr_pct,
        reconciliation_status=holding.reconciliation_status, flags=flags,
    )


def compute_all_holdings(
    session: Session, valuation_date: date, include_zero_value: bool = False,
) -> list[HoldingMetrics]:
    holding_ids = list(session.execute(select(Holding.holding_id)).scalars())
    ctx = build_context(session, holding_ids, valuation_date)
    results = [compute_holding_metrics(session, hid, valuation_date, ctx) for hid in holding_ids]
    if not include_zero_value:
        results = [r for r in results if r.balance_units > 0]
    return results


@dataclass
class AggregateTotals:
    invested_value: Decimal
    current_value: Decimal
    gain: Decimal
    absolute_return_pct: Optional[Decimal]
    weighted_days_held: Optional[int]
    xirr_pct: Optional[Decimal]


def aggregate(
    session: Session, holdings: list[HoldingMetrics], valuation_date: date,
    ctx: Optional[PortfolioContext] = None,
) -> AggregateTotals:
    """Spec 9.6: sums for invested/current/gain, gain/invested for
    absolute return, current-value-weighted average for days held, and
    XIRR recalculated from ALL consolidated cash flows — never an
    average of the individual holdings' own XIRRs (spec 9.5, 22:
    'Do not calculate weighted-average XIRR or CAGR').

    ctx: optional prefetched data from build_context, same performance-
    only contract as compute_holding_metrics. It matters more here than
    it looks: /api/portfolio calls this four times (one per asset-class
    bucket plus the grand total), so the transaction query below ran
    once per holding per bucket — four full passes over the portfolio's
    transactions per page load."""
    invested = sum((h.remaining_purchase_value for h in holdings), ZERO)
    current = sum((h.current_value for h in holdings), ZERO)
    gain = current - invested
    absolute_return_pct = (gain / invested * 100).quantize(Decimal("0.01")) if invested else None

    weighted_days = None
    if current > 0:
        day_products = sum(
            (h.current_value * h.weighted_days_held for h in holdings if h.weighted_days_held is not None), ZERO
        )
        weighted_days = int(day_products / current) if day_products else None

    cashflows: list[tuple[date, Decimal]] = []
    for hid in {h.holding_id for h in holdings}:
        transactions = (
            ctx.transactions.get(hid, []) if ctx is not None
            else list(session.execute(
                select(Transaction).where(Transaction.holding_id == hid).order_by(Transaction.date)
            ).scalars())
        )
        for t in transactions:
            cf = _txn_cash_flow(t)
            if cf is not None:
                cashflows.append((t.date, cf))
    holding_by_id = {h.holding_id: h for h in holdings}
    for hid, h in holding_by_id.items():
        if h.balance_units > 0 and h.current_nav is not None:
            cashflows.append((valuation_date, h.current_value))
    outcome = xirr_engine.xirr(cashflows)

    return AggregateTotals(
        invested_value=invested, current_value=current, gain=gain,
        absolute_return_pct=absolute_return_pct, weighted_days_held=weighted_days, xirr_pct=outcome.value,
    )
