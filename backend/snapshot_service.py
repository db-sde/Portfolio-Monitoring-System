"""
PortfolioIQ — snapshot_service.py

Portfolio Snapshot (spec section 13): a user-selected [start, end]
window's opening/closing units+value, net addition, net gain, and
period XIRR, bucketed by asset class then aggregated to Total.

Opening/closing units come from summing the RAW transaction ledger's own
signed units up to a cutoff date (casparser already signs REDEMPTION/
SWITCH_OUT/GIFT_OUT units negative) — a plain point-in-time reconstruction,
independent of the FIFO lot engine's *current* remaining-lot state,
which is exactly what "closing units as of an arbitrary past date" needs
(the lot engine only knows today's remaining state, not a historical
snapshot of it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

import nav_service
import xirr_engine
from models import Folio, Holding, Scheme, Transaction

ZERO = Decimal("0")
BUCKET_KEYS = ("EQUITY", "HYBRID", "DEBT", "total")

MONEY_OUT_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}
MONEY_IN_TYPES = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER", "DIVIDEND_PAYOUT"}
# REVERSAL isn't in MONEY_IN_TYPES: its amount is already negative (a
# bounced SIP's amount/units are both stored negative), so it needs its
# own signed handling below rather than an abs()'d addition — see
# portfolio_service.py's _txn_cash_flow, which already does this
# correctly; this module's own period_cashflows loop didn't, until this
# was found by auditing every MONEY_IN/OUT_TYPES site against the same
# real-statement bug that hit fifo.py (a reversed SIP's original
# PURCHASE_SIP outflow was being counted with nothing to offset it,
# overstating net invested cash and understating this holding's XIRR
# for this exact window).


@dataclass
class BucketSnapshot:
    opening_balance: Decimal = ZERO
    purchase: Decimal = ZERO
    switch_in: Decimal = ZERO
    switch_out: Decimal = ZERO
    div_payout: Decimal = ZERO
    redemption: Decimal = ZERO
    net_addition: Decimal = ZERO
    closing_balance: Decimal = ZERO
    net_gain: Decimal = ZERO
    xirr_pct: Optional[Decimal] = None
    _cashflows: list = field(default_factory=list, repr=False)


def _units_at(transactions: list[Transaction], as_of: Optional[date]) -> Decimal:
    if as_of is None:
        return ZERO
    return sum((t.units for t in transactions if t.units is not None and t.date <= as_of), ZERO)


def _bucket_for(asset_class: Optional[str]) -> str:
    return asset_class if asset_class in ("EQUITY", "HYBRID", "DEBT") else "total"
    # Note: casparser's own scheme.type only ever resolves to EQUITY/DEBT/
    # None (never HYBRID) — the HYBRID bucket exists structurally per
    # spec 13.2 but will be empty for casparser-sourced data today.


def compute_snapshot(
    session: Session, holding_ids: list[int], start_date: Optional[date], end_date: date,
) -> dict[str, dict]:
    buckets: dict[str, BucketSnapshot] = {k: BucketSnapshot() for k in (*BUCKET_KEYS[:-1], "total")}

    for holding_id in holding_ids:
        holding = session.get(Holding, holding_id)
        scheme = session.get(Scheme, holding.scheme_id)
        transactions = list(session.execute(
            select(Transaction).where(Transaction.holding_id == holding_id).order_by(Transaction.date)
        ).scalars())

        opening_units = _units_at(transactions, start_date) if start_date else ZERO
        opening_point = nav_service.get_nav_on_or_before(session, holding.scheme_id, start_date) if start_date else None
        opening_value = (opening_units * opening_point.nav) if (start_date and opening_point) else ZERO

        closing_units = _units_at(transactions, end_date)
        closing_point = nav_service.get_nav_on_or_before(session, holding.scheme_id, end_date)
        closing_value = (closing_units * closing_point.nav) if closing_point else ZERO

        period = {"purchase": ZERO, "switch_in": ZERO, "switch_out": ZERO, "div_payout": ZERO, "redemption": ZERO}
        type_key = {
            "PURCHASE": "purchase", "PURCHASE_SIP": "purchase",
            "SWITCH_IN": "switch_in", "SWITCH_IN_MERGER": "switch_in",
            "SWITCH_OUT": "switch_out", "SWITCH_OUT_MERGER": "switch_out",
            "DIVIDEND_PAYOUT": "div_payout", "REDEMPTION": "redemption",
        }
        period_cashflows: list[tuple[date, Decimal]] = []
        if start_date and opening_value:
            period_cashflows.append((start_date, -opening_value))
        for t in transactions:
            if start_date and t.date <= start_date:
                continue
            if t.date > end_date:
                continue
            key = type_key.get(t.type)
            if key and t.amount is not None:
                period[key] += abs(t.amount)
            elif t.type == "REVERSAL" and t.amount is not None:
                # Same gap as the cash-flow branch below: a bounced SIP's
                # PURCHASE_SIP amount already landed in period["purchase"]
                # via type_key above, and nothing reversed it back out —
                # inflating the displayed Purchase total on the Snapshot
                # page by however much was reversed. REVERSAL is
                # casparser's own name for a reversed SIP installment
                # specifically, so it always nets against "purchase".
                period["purchase"] -= abs(t.amount)
            if t.type in MONEY_OUT_TYPES and t.amount is not None:
                period_cashflows.append((t.date, -abs(t.amount)))
            elif t.type in MONEY_IN_TYPES and t.amount is not None:
                period_cashflows.append((t.date, abs(t.amount)))
            elif t.type == "REVERSAL" and t.amount is not None:
                period_cashflows.append((t.date, -t.amount))
        if closing_value:
            period_cashflows.append((end_date, closing_value))

        bucket_key = _bucket_for(scheme.asset_class)
        for key in (bucket_key, "total"):
            b = buckets[key]
            b.opening_balance += opening_value
            b.closing_balance += closing_value
            b.purchase += period["purchase"]
            b.switch_in += period["switch_in"]
            b.switch_out += period["switch_out"]
            b.div_payout += period["div_payout"]
            b.redemption += period["redemption"]
            b._cashflows.extend(period_cashflows)

    result = {}
    for key, b in buckets.items():
        b.net_addition = b.purchase + b.switch_in - b.switch_out - b.redemption
        # div_payout is real money paid out with no offsetting unit
        # reduction, so it has to be an outflow here or it silently
        # vanishes from net_gain (spec 13.2).
        b.net_gain = b.closing_balance + b.redemption + b.switch_out + b.div_payout - b.opening_balance - b.purchase - b.switch_in
        outcome = xirr_engine.xirr(b._cashflows) if len(b._cashflows) >= 2 else xirr_engine.XirrOutcome(None, "XIRR_NO_SOLUTION: fewer than 2 cash flows")
        b.xirr_pct = outcome.value
        result[key] = {
            "opening_balance": b.opening_balance, "purchase": b.purchase, "switch_in": b.switch_in,
            "switch_out": b.switch_out, "div_payout": b.div_payout, "redemption": b.redemption,
            "net_addition": b.net_addition, "closing_balance": b.closing_balance,
            "net_gain": b.net_gain, "xirr": b.xirr_pct,
        }
    return result
