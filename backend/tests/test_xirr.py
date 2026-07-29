"""Unit tests for xirr_engine.py (spec section 9.5, 13, 20)."""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xirr_engine import xirr  # noqa: E402


def test_appendix_a1_cash_flows_solve():
    flows = [
        (date(2026, 1, 1), Decimal("-1000")),
        (date(2026, 2, 1), Decimal("-600")),
        (date(2026, 3, 1), Decimal("1800")),
        (date(2026, 7, 28), Decimal("480")),
    ]
    result = xirr(flows)
    assert result.value is not None
    assert result.reason is None


def test_single_purchase_equals_cagr():
    """A single-tranche investment's XIRR is economically equivalent to
    CAGR (spec 20.1) — 1000 -> 1210 over exactly 1 year is a clean 21%."""
    flows = [(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("1210"))]
    result = xirr(flows)
    assert result.value == Decimal("21.00")


def test_same_date_only_is_no_solution_not_arbitrary_value():
    """Zero time separation between all flows must return
    XIRR_NO_SOLUTION explicitly, not an arbitrary boundary value from a
    flat-zero NPV function (previously silently returned ~-99.99%)."""
    flows = [(date(2026, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("1000"))]
    result = xirr(flows)
    assert result.value is None
    assert result.reason is not None


def test_near_zero_rate_never_displays_negative_zero():
    flows = [(date(2026, 1, 1), Decimal("-1000")), (date(2027, 1, 1), Decimal("999.9999"))]
    result = xirr(flows)
    assert result.value == Decimal("0.00")
    assert not str(result.value).startswith("-")


def test_single_flow_no_solution():
    result = xirr([(date(2026, 1, 1), Decimal("-1000"))])
    assert result.value is None


def test_all_same_sign_no_solution():
    result = xirr([(date(2026, 1, 1), Decimal("1000")), (date(2026, 6, 1), Decimal("500"))])
    assert result.value is None


def test_empty_no_solution():
    result = xirr([])
    assert result.value is None


if __name__ == "__main__":
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
    sys.exit(1 if failed else 0)
