"""Unit tests for fifo.py against spec section 8.2 and Appendix A.1.
Run: python -m pytest backend/tests/test_fifo.py -v
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fifo import run_fifo, LotInput  # noqa: E402


def test_appendix_a1_multiple_purchases_partial_redemption():
    events = [
        LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00")),
        LotInput(2, date(2026, 2, 1), "PURCHASE", Decimal("50"), Decimal("600"), Decimal("12.00")),
        LotInput(3, date(2026, 3, 1), "REDEMPTION", Decimal("120"), Decimal("1800"), Decimal("15.00")),
    ]
    result = run_fifo(events)

    assert len(result.lots) == 2
    lot1, lot2 = result.lots
    assert lot1.remaining_units == Decimal("0")
    assert lot2.remaining_units == Decimal("30")
    assert lot2.remaining_cost == Decimal("360.0")

    assert len(result.allocations) == 2
    a1, a2 = result.allocations
    assert a1.allocated_units == Decimal("100")
    assert a1.allocated_cost == Decimal("1000")
    assert a1.realized_gain == Decimal("500.000000000000000000000000")
    assert a2.allocated_units == Decimal("20")
    assert a2.allocated_cost == Decimal("240.0")
    assert round(a2.realized_gain, 2) == Decimal("60.00")

    remaining_units, remaining_cost = lot2.remaining_units, lot2.remaining_cost
    current_value = remaining_units * Decimal("16.00")
    assert current_value == Decimal("480.00")
    unrealized_gain = current_value - remaining_cost
    assert unrealized_gain == Decimal("120.00")
    absolute_return_pct = unrealized_gain / remaining_cost * 100
    assert round(absolute_return_pct, 2) == Decimal("33.33")

    assert not result.shortfalls


def test_single_purchase_no_flows():
    events = [LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00"))]
    result = run_fifo(events)
    assert len(result.lots) == 1
    assert result.lots[0].remaining_units == Decimal("100")
    assert not result.allocations
    assert not result.shortfalls


def test_two_purchases_no_redemption():
    events = [
        LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00")),
        LotInput(2, date(2026, 2, 1), "PURCHASE_SIP", Decimal("50"), Decimal("600"), Decimal("12.00")),
    ]
    result = run_fifo(events)
    assert len(result.lots) == 2
    total_units = sum(l.remaining_units for l in result.lots)
    total_cost = sum(l.remaining_cost for l in result.lots)
    weighted_nav = sum(l.remaining_units * l.purchase_nav for l in result.lots) / total_units
    assert total_units == Decimal("150")
    assert total_cost == Decimal("1600")
    assert round(weighted_nav, 4) == Decimal("10.6667")


def test_idcw_payout_no_units_no_lot():
    """DIVIDEND_PAYOUT isn't in LOT_CREATING_TYPES or DISPOSAL_TYPES, so
    run_fifo must never see it in the first place — the caller filters
    by TransactionType before building LotInput events. This test
    documents that expectation: passing a payout-shaped event through
    (if a caller mistakenly did) would be silently ignored, not crash or
    create a phantom lot."""
    events = [
        LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00")),
        LotInput(2, date(2026, 6, 1), "DIVIDEND_PAYOUT", Decimal("0"), Decimal("50"), None),
    ]
    result = run_fifo(events)
    assert len(result.lots) == 1
    assert not result.allocations


def test_idcw_reinvestment_creates_new_lot():
    events = [
        LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00")),
        LotInput(2, date(2026, 6, 1), "DIVIDEND_REINVEST", Decimal("5"), Decimal("55"), Decimal("11.00")),
    ]
    result = run_fifo(events)
    assert len(result.lots) == 2
    assert result.lots[1].origin_type == "DIVIDEND_REINVEST"
    assert result.lots[1].remaining_units == Decimal("5")


def test_switch_scheme_level_flows():
    """SWITCH_OUT disposes the source scheme via FIFO; SWITCH_IN (tested
    separately, at the destination holding) creates a new lot. This test
    is the source-side leg only."""
    events = [
        LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00")),
        LotInput(2, date(2026, 6, 1), "SWITCH_OUT", Decimal("100"), Decimal("1200"), Decimal("12.00")),
    ]
    result = run_fifo(events)
    assert result.lots[0].remaining_units == Decimal("0")
    assert len(result.allocations) == 1
    assert result.allocations[0].realized_gain == Decimal("200")


def test_fifo_shortfall_not_fabricated():
    """Redemption units exceeding known open lots must be flagged, not
    given an invented cost basis (spec 8.2)."""
    events = [
        LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00")),
        LotInput(2, date(2026, 6, 1), "REDEMPTION", Decimal("150"), Decimal("1800"), Decimal("12.00")),
    ]
    result = run_fifo(events)
    assert result.shortfalls == {2: Decimal("50")}
    # The 100 units that DID have a matching lot are still allocated correctly.
    assert len(result.allocations) == 1
    assert result.allocations[0].allocated_units == Decimal("100")


def test_reversal_nets_out_against_the_purchase_it_reverses():
    """Real production bug, found on a real statement: a bounced/reversed
    SIP installment shows up as its own PURCHASE_SIP row followed by a
    REVERSAL row clawing back the exact same units/amount. Before this
    test, REVERSAL wasn't in any of fifo.py's type sets at all, so
    run_fifo silently skipped it — the reversed purchase's units stayed
    in the derived balance forever, inflating a real holding's balance
    by exactly the reversed amount versus the CAS statement's own
    printed close. No realized_gain should come out of this either: a
    reversed purchase was never a real disposal."""
    events = [
        LotInput(1, date(2020, 10, 12), "PURCHASE_SIP", Decimal("43.387"), Decimal("999.95"), Decimal("23.047")),
        LotInput(2, date(2020, 10, 12), "REVERSAL", Decimal("43.387"), Decimal("999.95"), Decimal("23.047")),
    ]
    result = run_fifo(events)
    assert sum(l.remaining_units for l in result.lots) == Decimal("0")
    assert not result.allocations  # a reversal is not a taxable disposal
    assert not result.shortfalls


def test_zero_holding_excluded_by_caller():
    """A fully-redeemed holding (remaining_units == 0 on every lot) isn't
    something fifo.py itself hides — that's a caller-side display
    decision (spec 10.3/11.2: 'zero-balance holdings excluded by
    default'). This test just confirms the engine reports 0 remaining
    accurately so the caller CAN make that decision."""
    events = [
        LotInput(1, date(2026, 1, 1), "PURCHASE", Decimal("100"), Decimal("1000"), Decimal("10.00")),
        LotInput(2, date(2026, 6, 1), "REDEMPTION", Decimal("100"), Decimal("1300"), Decimal("13.00")),
    ]
    result = run_fifo(events)
    assert sum(l.remaining_units for l in result.lots) == Decimal("0")


if __name__ == "__main__":
    import sys as _sys
    test_fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in test_fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(test_fns) - failed}/{len(test_fns)} passed")
    _sys.exit(1 if failed else 0)
