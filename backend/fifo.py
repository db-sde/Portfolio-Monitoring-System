"""
PortfolioIQ — fifo.py

Pure FIFO lot-matching engine (spec section 8.2), independent of the DB
layer so it's directly unit-testable against Appendix A.1's worked
example. Consumes one holding's transactions in date order and produces
the lots and disposal allocations models.py persists — this module
returns plain dataclasses; db_ingestion.py is what turns them into
PurchaseLot/DisposalAllocation rows.

Decimal throughout (spec 5.3) — never float. Every money/units value
that enters this module must already be a Decimal; it's on the caller
(casparser gives Decimal fields directly) to not have downcast to float
before this point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

ZERO = Decimal("0")

# Spec 8.1 sign convention / lot action table.
LOT_CREATING_TYPES = {
    "PURCHASE",
    "PURCHASE_SIP",
    "SWITCH_IN",
    "SWITCH_IN_MERGER",
    "DIVIDEND_REINVEST",
    "GIFT_IN",
}
DISPOSAL_TYPES = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER"}
# GIFT_IN creates a lot (so a later disposal from the same holding has
# real units to consume) but it's tagged via origin_type so gains code
# can exclude it — the donor's cost basis/holding period (Sec 49(1)/
# 2(42A)) isn't known from a single CAS (spec 8.3), so this lot's
# "purchase_amount" is only a balance-tracking placeholder, never a
# taxable cost basis.
#
# GIFT_OUT/SEGREGATION reduce a holding's real balance (units genuinely
# leave, or get reclassified into a side-pocket scheme) but are NOT a
# sale for capital-gains purposes (Sec 47(iii) for gifts; segregation
# needs its own explicit cost-split data this transaction row doesn't
# carry) — FIFO-consumed like a disposal for balance accuracy, but never
# produces a DisposalAllocation/realized_gain.
#
# Reversals cancel a matching purchase; they never consume unrelated
# FIFO lots. A statement may print a reversal before its same-day purchase.
NON_TAXABLE_REDUCTION_TYPES = {"GIFT_OUT", "SEGREGATION", "REVERSAL"}
# No unit effect, no lot action at all: DIVIDEND_PAYOUT, STT_TAX,
# STAMP_DUTY_TAX, TDS_TAX, MISC, UNKNOWN.


@dataclass
class LotInput:
    """One transaction that creates or consumes units, already filtered
    to LOT_CREATING_TYPES/DISPOSAL_TYPES by the caller (fifo_engine only
    handles the matching, not classifying every TransactionType)."""

    transaction_id: int
    date: date
    type: str
    units: Decimal
    amount: Decimal  # purchase cash outflow, or disposal proceeds
    nav: Optional[Decimal]
    stamp_duty: Decimal = ZERO
    reverses_transaction_id: Optional[int] = None


@dataclass
class Lot:
    transaction_id: int
    acquired_date: date
    original_units: Decimal
    remaining_units: Decimal
    purchase_nav: Decimal
    purchase_amount: Decimal
    remaining_cost: Decimal
    stamp_duty: Decimal
    origin_type: str


@dataclass
class DisposalAllocation:
    disposal_transaction_id: int
    lot_index: int  # index into the Lot list this run produced, resolved to a real lot_id by the DB layer
    allocated_units: Decimal
    allocated_cost: Decimal
    sale_value: Decimal
    realized_gain: Decimal
    sold_date: date


@dataclass
class FifoResult:
    lots: list[Lot] = field(default_factory=list)
    allocations: list[DisposalAllocation] = field(default_factory=list)
    # Units a disposal tried to sell beyond what any open lot could cover
    # (spec 8.2: "If redemption units exceed known open lots, mark a FIFO
    # shortfall and do not fabricate a cost basis") — kept per offending
    # transaction so the caller can raise FIFO_SHORTFALL against exactly
    # that disposal, not the whole holding.
    shortfalls: dict[int, Decimal] = field(default_factory=dict)
    reversal_errors: dict[int, str] = field(default_factory=dict)
    reversal_links: dict[int, int] = field(default_factory=dict)


def run_fifo(events: list[LotInput]) -> FifoResult:
    """Match disposals in ledger order; reversals cancel a specific purchase.

    The cursor skips exhausted lots permanently. Lot indices remain stable
    for disposal allocations even when a reversal closes a newer lot.
    """
    result = FifoResult()
    # Resolve unique same-day cancellation pairs before replay. Printed row
    # order does not establish intraday execution order, and a cancelled
    # purchase must not become available to an intervening redemption.
    purchases_by_date = {}
    for event in events:
        if event.type in {"PURCHASE", "PURCHASE_SIP"} and event.units > ZERO:
            purchases_by_date.setdefault(event.date, []).append(event)
    proposed = {}
    claims = {}
    for event in events:
        if event.type != "REVERSAL":
            continue
        candidates = [
            p
            for p in purchases_by_date.get(event.date, [])
            if p.units == event.units
            and abs(p.amount - event.amount) <= Decimal("0.01")
            and (
                not event.reverses_transaction_id
                or p.transaction_id == event.reverses_transaction_id
            )
        ]
        if len(candidates) == 1:
            purchase_id = candidates[0].transaction_id
            proposed[event.transaction_id] = purchase_id
            claims[purchase_id] = claims.get(purchase_id, 0) + 1
    result.reversal_links = {
        reversal: purchase
        for reversal, purchase in proposed.items()
        if claims[purchase] == 1
    }
    cancelled = set(result.reversal_links.values())
    cursor = 0
    for event in events:
        if event.type in LOT_CREATING_TYPES:
            if event.units <= ZERO:
                continue
            result.lots.append(
                Lot(
                    transaction_id=event.transaction_id,
                    acquired_date=event.date,
                    original_units=event.units,
                    remaining_units=ZERO
                    if event.transaction_id in cancelled
                    else event.units,
                    purchase_nav=event.nav
                    if event.nav is not None
                    else event.amount / event.units,
                    purchase_amount=event.amount,
                    remaining_cost=ZERO
                    if event.transaction_id in cancelled
                    else event.amount + event.stamp_duty,
                    stamp_duty=event.stamp_duty,
                    origin_type=event.type,
                )
            )
            continue
        if event.type == "REVERSAL":
            if event.transaction_id in result.reversal_links:
                continue
            candidates = [
                l
                for l in result.lots
                if l.origin_type in {"PURCHASE", "PURCHASE_SIP"}
                and l.remaining_units == l.original_units
                and l.original_units == event.units
                and (
                    l.transaction_id == event.reverses_transaction_id
                    if event.reverses_transaction_id
                    else abs(l.purchase_amount - event.amount) <= Decimal("0.01")
                )
            ]
            if len(candidates) != 1:
                result.reversal_errors[event.transaction_id] = (
                    "Reversal has no unique, unconsumed originating purchase."
                )
                continue
            lot = candidates[0]
            lot.remaining_units = ZERO
            lot.remaining_cost = ZERO
            result.reversal_links[event.transaction_id] = lot.transaction_id
            continue
        if event.type not in DISPOSAL_TYPES | NON_TAXABLE_REDUCTION_TYPES:
            continue
        remaining = event.units
        while cursor < len(result.lots) and remaining > ZERO:
            lot = result.lots[cursor]
            if lot.remaining_units <= ZERO:
                cursor += 1
                continue
            matched = min(lot.remaining_units, remaining)
            cost = (lot.purchase_amount + lot.stamp_duty) * matched / lot.original_units
            if event.type in DISPOSAL_TYPES:
                proceeds = event.amount * matched / event.units
                result.allocations.append(
                    DisposalAllocation(
                        disposal_transaction_id=event.transaction_id,
                        lot_index=cursor,
                        allocated_units=matched,
                        allocated_cost=cost,
                        sale_value=proceeds,
                        realized_gain=proceeds - cost,
                        sold_date=event.date,
                    )
                )
            lot.remaining_units -= matched
            lot.remaining_cost -= cost
            remaining -= matched
        if remaining > ZERO:
            result.shortfalls[event.transaction_id] = remaining
    return result
