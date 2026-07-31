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
LOT_CREATING_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER", "DIVIDEND_REINVEST", "GIFT_IN"}
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
# REVERSAL belongs here too, not in the no-op list below — a real
# statement's REVERSAL row (a bounced/reversed SIP installment: the AMC
# claws back the exact units and amount of an earlier PURCHASE_SIP) is
# never paired to its originating purchase by any explicit key, so
# rather than try to match them, it's consumed the same way GIFT_OUT
# already is: chronologically against whatever lots are open, oldest
# first. Confirmed against a real statement where the original design
# ("no unit effect, handled by the transaction it reverses simply not
# being counted") left REVERSAL out of every FIFO-affecting set
# entirely — the reversed SIP's units stayed in the derived balance
# forever since nothing ever subtracted them back out, inflating a real
# holding's balance by exactly the sum of its REVERSAL units versus the
# CAS statement's own printed close.
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


def run_fifo(events: list[LotInput]) -> FifoResult:
    """events MUST already be sorted (date, then a stable original
    ledger order for same-day ties) by the caller — this function does
    not re-sort, since "same-day ties" ordering is a CAS-ledger-position
    decision (spec 6.3's occurrence_index), not something to infer here."""
    result = FifoResult()
    open_lots: list[Lot] = []  # oldest-first; index also serves as lot_index

    for event in events:
        if event.type in LOT_CREATING_TYPES:
            lot = Lot(
                transaction_id=event.transaction_id,
                acquired_date=event.date,
                original_units=event.units,
                remaining_units=event.units,
                purchase_nav=event.nav if event.nav is not None else (
                    event.amount / event.units if event.units else ZERO
                ),
                purchase_amount=event.amount,
                remaining_cost=event.amount + event.stamp_duty,
                stamp_duty=event.stamp_duty,
                origin_type=event.type,
            )
            open_lots.append(lot)
            result.lots.append(lot)

        elif event.type in DISPOSAL_TYPES or event.type in NON_TAXABLE_REDUCTION_TYPES:
            is_taxable = event.type in DISPOSAL_TYPES
            remaining_to_sell = event.units
            total_units_for_proceeds = event.units  # for proportional sale-value allocation
            for idx, lot in enumerate(open_lots):
                if remaining_to_sell <= ZERO:
                    break
                if lot.remaining_units <= ZERO:
                    continue
                matched = min(lot.remaining_units, remaining_to_sell)
                cost_fraction = matched / lot.original_units if lot.original_units else ZERO
                # Proportional slice of THIS lot's own original cost, not
                # the lot's current remaining_cost — remaining_cost was
                # already reduced by any earlier partial disposal, so
                # re-deriving from original_units/purchase_amount keeps
                # each allocation's cost tied to what was actually paid
                # for those specific units, not a stale running remainder.
                allocated_cost = (lot.purchase_amount + lot.stamp_duty) * cost_fraction
                if is_taxable:
                    sale_value = (
                        event.amount * (matched / total_units_for_proceeds)
                        if total_units_for_proceeds else ZERO
                    )
                    result.allocations.append(DisposalAllocation(
                        disposal_transaction_id=event.transaction_id,
                        lot_index=idx,
                        allocated_units=matched,
                        allocated_cost=allocated_cost,
                        sale_value=sale_value,
                        realized_gain=sale_value - allocated_cost,
                        sold_date=event.date,
                    ))
                # Non-taxable reduction (gift-out/segregation): units and
                # cost still come off the lot so balance stays accurate,
                # just with no allocation/gain record created.
                lot.remaining_units -= matched
                lot.remaining_cost -= allocated_cost
                remaining_to_sell -= matched

            if remaining_to_sell > ZERO:
                result.shortfalls[event.transaction_id] = remaining_to_sell

    return result
