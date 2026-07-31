"""
Pure unit tests for ingestion.py's helper functions — no DB needed.

Regression coverage for a real production bug: casparser's
statement_period.from_/to fields come back as "DD-Mon-YYYY" strings
(e.g. "01-Jan-2003") on a real CAS statement, not the ISO format every
synthetic test fixture in this repo had used until this bug shipped —
_as_date crashed with ValueError on the very first real statement
uploaded after the Postgres migration went live. Diagnosed from the
production traceback, not guessed.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import _as_date, _asset_class, _derive_plan_option  # noqa: E402


def test_as_date_handles_dd_mon_yyyy():
    """The exact format that crashed production."""
    assert _as_date("01-Jan-2003") == date(2003, 1, 1)


def test_as_date_handles_iso():
    assert _as_date("2024-06-10") == date(2024, 6, 10)


def test_as_date_handles_dd_mm_yyyy():
    assert _as_date("15-01-2024") == date(2024, 1, 15)


def test_as_date_passes_through_real_date_object():
    d = date(2024, 1, 15)
    assert _as_date(d) is d


def test_as_date_raises_clearly_on_garbage():
    try:
        _as_date("not-a-date")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not-a-date" in str(exc)


def test_derive_plan_option():
    assert _derive_plan_option("HSBC Small Cap Fund - Direct Plan - Growth") == ("Direct", "Growth")
    assert _derive_plan_option("SBI Contra Fund - Regular Plan - IDCW") == ("Regular", "IDCW")
    assert _derive_plan_option("Some Fund - Dividend Option") == ("Regular", "IDCW")


def test_asset_class_folds_unknown_into_other():
    assert _asset_class("EQUITY") == "EQUITY"
    assert _asset_class("DEBT") == "DEBT"
    assert _asset_class("N/A") == "OTHER"
    assert _asset_class(None) == "OTHER"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
