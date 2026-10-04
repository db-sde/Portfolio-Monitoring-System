"""Pure ledger ordering, cash flows and FIFO event construction."""

from collections import defaultdict
from decimal import Decimal
from fifo import (
    LotInput,
    LOT_CREATING_TYPES,
    DISPOSAL_TYPES,
    NON_TAXABLE_REDUCTION_TYPES,
    run_fifo,
)

ZERO = Decimal("0")
BLOCKING_CODES = {
    "CAS_RECONCILIATION_FAILED",
    "INCOMPLETE_OPENING_HISTORY",
    "SCHEME_UNRESOLVED",
    "FIFO_SHORTFALL",
    "REVERSAL_UNRESOLVED",
    "NAV_UNAVAILABLE",
    "NAV_STALE",
    "COST_BASIS_UNAVAILABLE",
}


def ordered(transactions):
    return sorted(
        transactions,
        key=lambda t: (
            t.date,
            getattr(t, "ledger_position", None)
            if getattr(t, "ledger_position", None) is not None
            else t.transaction_id,
        ),
    )


def fifo_for(transactions):
    transactions = ordered(transactions)
    duty, amounts = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    for t in transactions:
        if t.type == "STAMP_DUTY_TAX" and t.amount and t.amount > ZERO:
            # Refunds belong to cancelled purchases, not extra acquisition
            # costs on the day's surviving lots.
            duty[t.date] += t.amount
        if t.type in LOT_CREATING_TYPES and t.amount:
            amounts[t.date] += abs(t.amount)
    events = []
    for t in transactions:
        if (
            t.type
            not in LOT_CREATING_TYPES | DISPOSAL_TYPES | NON_TAXABLE_REDUCTION_TYPES
        ):
            continue
        # A positive segregation is an acquisition with unknown carried cost,
        # not a disposal. Ownership still comes from the signed ledger.
        if t.type == "SEGREGATION" and (t.units or ZERO) >= ZERO:
            continue
        stamp = (
            duty[t.date] * abs(t.amount or ZERO) / amounts[t.date]
            if t.type in LOT_CREATING_TYPES and amounts[t.date]
            else ZERO
        )
        events.append(
            LotInput(
                t.transaction_id,
                t.date,
                t.type,
                abs(t.units or ZERO),
                abs(t.amount or ZERO),
                t.nav,
                stamp,
                getattr(t, "reverses_transaction_id", None),
            )
        )
    return run_fifo(events)


def cashflows(transactions, end_date=None):
    """Signed flows combine same-day internal switch legs at the caller's scope."""
    totals = defaultdict(lambda: ZERO)
    for t in transactions:
        if end_date and t.date > end_date:
            continue
        if t.amount is None:
            continue
        if t.type in {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}:
            totals[t.date] -= abs(t.amount)
        elif t.type == "STAMP_DUTY_TAX":
            totals[t.date] -= t.amount  # negative duty is a refund
        elif t.type in {
            "REDEMPTION",
            "SWITCH_OUT",
            "SWITCH_OUT_MERGER",
            "DIVIDEND_PAYOUT",
            "REVERSAL",
        }:
            totals[t.date] += abs(t.amount)
    return [(d, amount) for d, amount in sorted(totals.items()) if amount]
