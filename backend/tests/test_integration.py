"""
Integration tests against a real (test-scoped) Neon connection, covering
the remaining spec 20.1 rows not exercised by test_fifo.py/test_xirr.py:
weekend/holiday NAV resolution, overlapping-CAS-upload idempotency, and
missing-benchmark-data handling. Each test cleans up its own rows.

Run: python backend/tests/test_integration.py (needs DATABASE_URL set —
backend/.env is loaded automatically, same as the app itself).
"""
import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

import db  # noqa: E402
import nav_service  # noqa: E402
import benchmark_service  # noqa: E402
import snapshot_service  # noqa: E402
from ingestion import ingest_cas  # noqa: E402
from models import Holding, Scheme  # noqa: E402
from sqlalchemy import select  # noqa: E402

from casparser.types import (  # noqa: E402
    CASData, StatementPeriod, InvestorInfo, Folio, Scheme as CasScheme, SchemeValuation, TransactionData,
)
from casparser.enums import TransactionType, CASFileType, FileType  # noqa: E402


def _cleanup():
    with db.engine.begin() as conn:
        for t in ("disposal_allocations", "purchase_lots", "transactions", "nav_cache",
                  "holdings", "scheme_aliases", "schemes", "folios", "cas_uploads"):
            conn.execute(text(f"TRUNCATE TABLE {t} RESTART IDENTITY CASCADE"))


def test_weekend_holiday_nav_resolution():
    """Appendix A.3: requesting a Sunday with no NAV that day must fall
    back to the most recent NAV on or before it (here, the Friday
    before), while the requested date itself is preserved separately."""
    _cleanup()
    with db.get_session() as session:
        scheme = Scheme(isin="INF_TESTWKND", amfi_code=None, name="Weekend Test Fund", asset_class="EQUITY")
        session.add(scheme)
        session.flush()
        nav_service.store_nav_points(session, scheme.scheme_id, [
            (date(2026, 7, 24), Decimal("100.00")),  # Friday
            (date(2026, 7, 27), Decimal("102.00")),  # Monday
        ])
        scheme_id = scheme.scheme_id

    with db.get_session() as session:
        point = nav_service.get_nav_on_or_before(session, scheme_id, date(2026, 7, 26))  # Sunday
        assert point is not None
        assert point.resolved_date == date(2026, 7, 24), f"expected Friday fallback, got {point.resolved_date}"
        assert point.requested_date == date(2026, 7, 26)
        assert point.nav == Decimal("100.00")
    _cleanup()
    print("PASS test_weekend_holiday_nav_resolution")


def test_overlapping_cas_upload_no_duplicates():
    """Spec 20.1: re-importing an overlapping/identical CAS produces no
    duplicate transactions and the same result on re-import."""
    _cleanup()
    txns = [TransactionData(
        date=date(2024, 1, 1), description="Purchase", amount=Decimal("1000"), units=Decimal("100"),
        nav=Decimal("10"), balance=Decimal("100"), type=TransactionType.PURCHASE,
    )]
    scheme = CasScheme(
        scheme="Overlap Test Fund - Direct Growth", advisor=None, rta_code="X", rta="CAMS", type="EQUITY",
        isin="INF_TESTOVERLAP", amfi=None, open=Decimal("0"), close=Decimal("100"), close_calculated=Decimal("100"),
        valuation=SchemeValuation(date=date(2026, 7, 29), nav=Decimal("11"), cost=Decimal("1000"), value=Decimal("1100")),
        transactions=txns,
    )
    folio = Folio(folio="OVERLAPFOLIO", amc="Test AMC", schemes=[scheme])
    parsed = CASData(
        statement_period=StatementPeriod(**{"from": "2024-01-01", "to": "2026-07-29"}), folios=[folio],
        investor_info=InvestorInfo(name="T", email="t@e.com", address="a", mobile="9999999999"),
        cas_type=CASFileType.DETAILED, file_type=FileType.CAMS,
    )

    async def run():
        async with httpx.AsyncClient() as client:
            with db.get_session() as session:
                r1 = await ingest_cas(session, client, parsed, b"overlap-bytes-1", investor_id=None)
            with db.get_session() as session:
                r2 = await ingest_cas(session, client, parsed, b"overlap-bytes-1", investor_id=None)  # identical bytes
        return r1, r2

    r1, r2 = asyncio.run(run())
    assert r1.duplicate is False
    assert r2.duplicate is True, "identical file bytes must be recognised as a duplicate upload"

    with db.get_session() as session:
        from models import Transaction
        count = len(list(session.execute(select(Transaction)).scalars()))
        assert count == 1, f"expected exactly 1 transaction after re-import, got {count}"
    _cleanup()
    print("PASS test_overlapping_cas_upload_no_duplicates")


def test_stamp_duty_flows_into_lot_cost_basis():
    """Spec 8.3/12: 'tax cost includes apportioned acquisition stamp duty
    where applicable.' A real CAS ledger carries stamp duty as its own
    STAMP_DUTY_TAX row next to the purchase it applies to — the
    infrastructure to carry it through (PurchaseLot.stamp_duty,
    gains_service_db's proportional split) already existed, but nothing
    in ingestion.py was ever populating it, so every lot's stamp duty
    silently stayed zero. This asserts the real fix: ingest a purchase
    with its stamp duty row and confirm the resulting PurchaseLot's
    stamp_duty and remaining_cost both reflect it."""
    _cleanup()
    txns = [
        TransactionData(
            date=date(2024, 1, 1), description="Purchase", amount=Decimal("1000"), units=Decimal("100"),
            nav=Decimal("10"), balance=Decimal("100"), type=TransactionType.PURCHASE,
        ),
        TransactionData(
            date=date(2024, 1, 1), description="Stamp Duty", amount=Decimal("0.05"), units=None,
            nav=None, balance=None, type=TransactionType.STAMP_DUTY_TAX,
        ),
    ]
    scheme = CasScheme(
        scheme="Stamp Duty Test Fund - Direct Growth", advisor=None, rta_code="X", rta="CAMS", type="EQUITY",
        isin="INF_TESTSTAMPDUTY", amfi=None, open=Decimal("0"), close=Decimal("100"), close_calculated=Decimal("100"),
        valuation=SchemeValuation(date=date(2026, 7, 29), nav=Decimal("11"), cost=Decimal("1000"), value=Decimal("1100")),
        transactions=txns,
    )
    folio = Folio(folio="STAMPDUTYFOLIO", amc="Test AMC", schemes=[scheme])
    parsed = CASData(
        statement_period=StatementPeriod(**{"from": "2024-01-01", "to": "2026-07-29"}), folios=[folio],
        investor_info=InvestorInfo(name="T", email="t@e.com", address="a", mobile="9999999999"),
        cas_type=CASFileType.DETAILED, file_type=FileType.CAMS,
    )

    async def run():
        async with httpx.AsyncClient() as client:
            with db.get_session() as session:
                await ingest_cas(session, client, parsed, b"stamp-duty-bytes", investor_id=None)

    asyncio.run(run())

    with db.get_session() as session:
        from models import PurchaseLot
        lot = session.execute(select(PurchaseLot)).scalar_one()
        assert lot.stamp_duty == Decimal("0.05"), f"expected stamp_duty 0.05, got {lot.stamp_duty}"
        assert lot.remaining_cost == Decimal("1000.05"), f"expected remaining_cost 1000.05, got {lot.remaining_cost}"
    _cleanup()
    print("PASS test_stamp_duty_flows_into_lot_cost_basis")


def test_snapshot_reversal_does_not_inflate_purchase_or_xirr():
    """Companion to test_reversal_nets_out_against_the_purchase_it_reverses
    in test_fifo.py — that one covers FIFO units, this covers the
    Portfolio Snapshot page's own separate purchase-total accumulator
    and cash-flow list in snapshot_service.py, which had the exact same
    gap: REVERSAL wasn't netted against the PURCHASE_SIP it reverses in
    either the displayed 'Purchase' total or the XIRR cash flows, so a
    fully-reversed SIP (bought then immediately clawed back, net
    nothing) would have shown up as a real ₹999.95 purchase with no
    offsetting flow — a phantom outflow inflating both figures."""
    _cleanup()
    txns = [
        TransactionData(
            date=date(2024, 1, 1), description="SIP Purchase", amount=Decimal("999.95"), units=Decimal("43.387"),
            nav=Decimal("23.047"), balance=Decimal("43.387"), type=TransactionType.PURCHASE_SIP,
        ),
        TransactionData(
            date=date(2024, 1, 2), description="Reversal", amount=Decimal("-999.95"), units=Decimal("-43.387"),
            nav=Decimal("23.047"), balance=Decimal("0"), type=TransactionType.REVERSAL,
        ),
    ]
    scheme = CasScheme(
        scheme="Reversal Snapshot Test Fund - Direct Growth", advisor=None, rta_code="X", rta="CAMS", type="EQUITY",
        isin="INF_TESTREVSNAP", amfi=None, open=Decimal("0"), close=Decimal("0"), close_calculated=Decimal("0"),
        valuation=SchemeValuation(date=date(2026, 7, 29), nav=Decimal("25"), cost=Decimal("0"), value=Decimal("0")),
        transactions=txns,
    )
    folio = Folio(folio="REVSNAPFOLIO", amc="Test AMC", schemes=[scheme])
    parsed = CASData(
        statement_period=StatementPeriod(**{"from": "2024-01-01", "to": "2026-07-29"}), folios=[folio],
        investor_info=InvestorInfo(name="T", email="t@e.com", address="a", mobile="9999999999"),
        cas_type=CASFileType.DETAILED, file_type=FileType.CAMS,
    )

    async def run():
        async with httpx.AsyncClient() as client:
            with db.get_session() as session:
                await ingest_cas(session, client, parsed, b"reversal-snapshot-bytes", investor_id=None)

    asyncio.run(run())

    with db.get_session() as session:
        holding = session.execute(select(Holding)).scalar_one()
        result = snapshot_service.compute_snapshot(
            session, [holding.holding_id], date(2023, 12, 31), date(2026, 7, 29),
        )
    total = result["total"]
    assert total["purchase"] == Decimal("0"), f"expected purchase fully netted to 0, got {total['purchase']}"
    assert total["net_gain"] == Decimal("0"), f"expected net_gain 0 (nothing really happened), got {total['net_gain']}"
    _cleanup()
    print("PASS test_snapshot_reversal_does_not_inflate_purchase_or_xirr")


def test_missing_benchmark_shows_unavailable_not_zero():
    """Spec 22: 'Do not return 0.00% for an unavailable or mathematically
    invalid return.' Nifty 500 has no configured source at all -> must
    be an explicit unavailable status, never a fabricated 0%."""
    _cleanup()
    with db.get_session() as session:
        result = benchmark_service.simulate_benchmark_xirr(
            session, [(date(2024, 1, 1), Decimal("-1000"))], date(2026, 7, 29), "Nifty 500",
        )
    assert result.value is None
    assert result.status == "unavailable"
    assert result.value != 0
    print("PASS test_missing_benchmark_shows_unavailable_not_zero")


if __name__ == "__main__":
    tests = [test_weekend_holiday_nav_resolution, test_overlapping_cas_upload_no_duplicates,
             test_stamp_duty_flows_into_lot_cost_basis, test_snapshot_reversal_does_not_inflate_purchase_or_xirr,
             test_missing_benchmark_shows_unavailable_not_zero]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
