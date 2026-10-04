from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS
import asyncio
import pytest
import ledger, portfolio_service as portfolio, snapshot_service, provider, auth
from fifo import LotInput, run_fifo
from models import Holding, Folio, Scheme, Transaction


def txn(i, when, kind, units, amount, position=None):
    return Transaction(
        transaction_id=i,
        holding_id=1,
        date=when,
        type=kind,
        units=D(str(units)),
        amount=D(str(amount)),
        nav=None,
        ledger_position=position,
    )


def context(
    transactions, nav=D("12"), opening=D("0"), start=date(2023, 1, 1), code=None
):
    h = Holding(
        holding_id=1,
        folio_id=1,
        scheme_id=1,
        opening_units=opening,
        opening_date=start,
        reconciliation_status="reconciled",
        data_quality_code=code,
    )
    s = Scheme(
        scheme_id=1,
        name="Test",
        isin="TEST",
        identity_confirmed=True,
        asset_class="OTHER",
    )
    f = Folio(folio_id=1, normalized_folio="123", amc="AMC")
    navs = {1: NS(nav=nav, resolved_date=date(2024, 1, 1))} if nav is not None else {}
    return portfolio.PortfolioContext(
        {1: h}, {1: f}, {1: s}, {}, navs, {1: transactions}
    )


def test_reversal_preserves_old_purchase_cost():
    result = run_fifo(
        [
            LotInput(1, date(2023, 1, 1), "PURCHASE", D(100), D(1000), D(10)),
            LotInput(2, date(2024, 1, 1), "PURCHASE_SIP", D(10), D(200), D(20)),
            LotInput(3, date(2024, 1, 2), "REVERSAL", D(10), D(200), D(20)),
        ]
    )
    assert sum(l.remaining_cost for l in result.lots) == 1000
    assert result.reversal_links == {3: 2}
    assert not result.allocations


def test_ambiguous_reversal_does_not_consume_other_lots():
    events = [
        LotInput(i, date(2024, 1, 1), "PURCHASE_SIP", D(10), D(200), D(20))
        for i in (1, 2)
    ]
    result = run_fifo(
        events + [LotInput(3, date(2024, 1, 2), "REVERSAL", D(10), D(200), D(20))]
    )
    assert 3 in result.reversal_errors
    assert sum(l.remaining_cost for l in result.lots) == 400


@pytest.mark.parametrize("purchase_first", [True, False])
def test_same_day_reversal_pair_is_cancelled_before_redemption(purchase_first):
    old = LotInput(1, date(2023, 1, 1), "PURCHASE", D(10), D(100), D(10))
    purchase = LotInput(2, date(2024, 1, 1), "PURCHASE_SIP", D(5), D(100), D(20))
    reversal = LotInput(3, date(2024, 1, 1), "REVERSAL", D(5), D(100), D(20))
    disposal = LotInput(4, date(2024, 1, 1), "REDEMPTION", D(10), D(200), D(20))
    pair = (
        [purchase, disposal, reversal]
        if purchase_first
        else [reversal, disposal, purchase]
    )
    result = run_fifo([old, *pair])
    assert not result.reversal_errors and not result.shortfalls
    assert result.reversal_links == {3: 2}
    assert sum(l.remaining_units for l in result.lots) == 0
    assert sum(a.allocated_cost for a in result.allocations) == 100


def test_reversal_cannot_match_a_purchase_on_a_future_date():
    result = run_fifo(
        [
            LotInput(1, date(2024, 1, 1), "REVERSAL", D(5), D(100), D(20)),
            LotInput(2, date(2024, 1, 2), "PURCHASE", D(5), D(100), D(20)),
        ]
    )
    assert 1 in result.reversal_errors
    assert result.lots[0].remaining_units == 5


def test_stamp_duty_refund_is_not_extra_cost_or_cash_outflow():
    when = date(2024, 1, 1)
    rows = [
        txn(1, when, "REVERSAL", -5, -99.95),
        txn(2, when, "STAMP_DUTY_TAX", 0, -0.05),
        txn(3, when, "PURCHASE_SIP", 5, 99.95),
        txn(4, when, "STAMP_DUTY_TAX", 0, 0.05),
        txn(5, when, "PURCHASE_SIP", 10, 199.90),
        txn(6, when, "STAMP_DUTY_TAX", 0, 0.10),
    ]
    result = ledger.fifo_for(rows)
    assert sum(l.remaining_cost for l in result.lots) == D("200")
    assert ledger.cashflows(rows) == [(when, D("-200"))]


def test_recomputed_fifo_clears_stale_import_reversal_warning():
    when = date(2023, 6, 1)
    ctx = context(
        [
            txn(1, when, "REVERSAL", -10, -100),
            txn(2, when, "PURCHASE_SIP", 10, 100),
            txn(3, date(2023, 7, 1), "PURCHASE", 20, 200),
        ],
        code="REVERSAL_UNRESOLVED",
    )
    row = portfolio.compute_holding_metrics(None, 1, date(2024, 1, 1), ctx)
    assert row.balance_units == 20 and row.remaining_purchase_value == 200
    assert row.gain == 40 and row.xirr_pct is not None
    assert not row.flags


def test_closed_holdings_count_separately_and_keep_lifetime_cashflows():
    ctx = context(
        [
            txn(1, date(2023, 6, 1), "PURCHASE", 100, 1000),
            txn(2, date(2023, 12, 1), "REDEMPTION", -100, -1100),
        ]
    )
    row = portfolio.compute_holding_metrics(None, 1, date(2024, 1, 1), ctx)
    total = portfolio.aggregate(None, [row], date(2024, 1, 1), ctx)
    assert total.total_holdings == total.valued_holdings == 0
    assert total.closed_holdings == 1
    assert total.current_value == total.invested_value == 0
    assert total.xirr_pct is not None and total.xirr_pct > 0


def test_snapshot_known_value_survives_unknown_carried_cost(monkeypatch):
    ctx = context([txn(1, date(2023, 6, 1), "GIFT_IN", 100, 1000)])
    monkeypatch.setattr(
        snapshot_service.nav_service, "get_navs_on_or_before", lambda *a: ctx.navs
    )
    result = snapshot_service.compute_snapshot(None, [1], None, date(2024, 1, 1), ctx)[
        "total"
    ]
    assert result["closing_balance"] == 1200
    assert result["net_gain"] is None and result["xirr"] is None


def test_snapshot_cashflows_include_stamp_duty(monkeypatch):
    ctx = context(
        [
            txn(1, date(2023, 6, 1), "PURCHASE", 100, 999.95),
            txn(2, date(2023, 6, 1), "STAMP_DUTY_TAX", 0, 0.05),
        ]
    )
    monkeypatch.setattr(
        snapshot_service.nav_service, "get_navs_on_or_before", lambda *a: ctx.navs
    )
    result = snapshot_service.compute_snapshot(None, [1], None, date(2024, 1, 1), ctx)[
        "total"
    ]
    assert result["purchase"] == 1000 and result["net_gain"] == 200
    assert (
        result["xirr"]
        == portfolio.compute_holding_metrics(None, 1, date(2024, 1, 1), ctx).xirr_pct
    )


def test_same_day_ledger_order_does_not_use_occurrence_index():
    rows = [txn(i, date(2024, 1, 1), "PURCHASE", 1, 10, position=i) for i in (1, 2, 3)]
    for t, o in zip(rows, (0, 1, 0)):
        t.occurrence_index = o
    assert [t.transaction_id for t in ledger.ordered(rows)] == [1, 2, 3]


def test_historical_metrics_ignore_future_purchase_and_redemption():
    rows = [
        txn(1, date(2023, 6, 1), "PURCHASE", 100, 1000),
        txn(2, date(2025, 1, 1), "REDEMPTION", -100, -1500),
    ]
    ctx = context(rows)
    result = portfolio.compute_holding_metrics(None, 1, date(2024, 1, 1), ctx)
    assert result.balance_units == 100 and result.remaining_purchase_value == 1000
    assert result.weighted_days_held == 214
    before = portfolio.compute_holding_metrics(None, 1, date(2023, 3, 1), ctx)
    assert before.balance_units == 0 and before.weighted_days_held is None


def test_missing_nav_is_unknown_and_blocks_aggregate_return():
    ctx = context([txn(1, date(2023, 6, 1), "PURCHASE", 100, 1000)], nav=None)
    row = portfolio.compute_holding_metrics(None, 1, date(2024, 1, 1), ctx)
    total = portfolio.aggregate(None, [row], date(2024, 1, 1), ctx)
    assert row.current_value is None and row.gain is None
    assert total.current_value is None and total.xirr_pct is None
    assert total.valued_holdings == 0 and total.known_current_value == 0


def test_opening_units_are_owned_but_have_unknown_cost():
    ctx = context([], opening=D(100))
    row = portfolio.compute_holding_metrics(None, 1, date(2024, 1, 1), ctx)
    assert row.current_value == 1200
    assert row.remaining_purchase_value is None and row.xirr_pct is None


def test_other_snapshot_counted_once(monkeypatch):
    ctx = context([txn(1, date(2023, 6, 1), "PURCHASE", 100, 1000)])
    monkeypatch.setattr(
        snapshot_service.nav_service, "get_navs_on_or_before", lambda *a: ctx.navs
    )
    result = snapshot_service.compute_snapshot(None, [1], None, date(2024, 1, 1), ctx)
    assert result["OTHER"]["closing_balance"] == 1200
    assert result["total"]["closing_balance"] == 1200
    assert result["total"]["purchase"] == 1000


def test_snapshot_missing_nav_never_reports_zero_loss(monkeypatch):
    ctx = context([txn(1, date(2023, 6, 1), "PURCHASE", 100, 1000)], nav=None)
    monkeypatch.setattr(
        snapshot_service.nav_service, "get_navs_on_or_before", lambda *a: {}
    )
    result = snapshot_service.compute_snapshot(None, [1], None, date(2024, 1, 1), ctx)[
        "total"
    ]
    assert result["closing_balance"] is None and result["net_gain"] is None
    assert result["xirr"] is None and result["data_quality"] == "PARTIAL"


def test_cashflows_net_same_day_switches_and_drop_future_flows():
    rows = [
        txn(1, date(2024, 1, 1), "SWITCH_IN", 10, 100),
        txn(2, date(2024, 1, 1), "SWITCH_OUT", -10, -100),
        txn(3, date(2025, 1, 1), "PURCHASE", 1, 10),
    ]
    assert ledger.cashflows(rows, date(2024, 1, 1)) == []


def test_encrypted_payload():
    encrypted = auth.seal({"password": "sensitive"})
    assert "sensitive" not in encrypted
    assert auth.unseal(encrypted)["password"] == "sensitive"


def test_provider_deduplicates_concurrent_calls():
    class Client:
        requests_by_key = {}
        metrics = {"cache_hits": 0}
        calls = 0

        async def get(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(0.01)
            return NS(status_code=200, json=lambda: {"ok": True})

    async def run():
        client = Client()
        result = await asyncio.gather(
            *(
                provider.fetch_json(client, "https://example.invalid/test")
                for _ in range(20)
            )
        )
        assert client.calls == 1 and all(r == {"ok": True} for r in result)

    asyncio.run(run())


def test_retry_after_is_minimum_delay():
    assert provider.retry_delay("30", 0) >= 30


def test_unverified_scheme_cannot_value_cached_wrong_nav():
    ctx = context([txn(1, date(2023, 6, 1), "PURCHASE", 100, 1000)])
    ctx.schemes[1].identity_confirmed = False
    assert (
        portfolio.compute_holding_metrics(None, 1, date(2024, 1, 1), ctx).current_value
        is None
    )


def test_provider_limits_nested_requests_per_host(monkeypatch):
    import httpx

    current = 0
    maximum = 0

    async def respond(*a, **kw):
        nonlocal current, maximum
        current += 1
        maximum = max(maximum, current)
        await asyncio.sleep(0.005)
        current -= 1
        return NS(status_code=200, json=lambda: {"ok": True})

    monkeypatch.setattr(httpx.AsyncClient, "get", respond)

    async def run():
        async with provider.Client() as client:
            await asyncio.gather(
                *(
                    provider.fetch_json(client, f"https://example.invalid/{i}")
                    for i in range(40)
                )
            )

    asyncio.run(run())
    assert maximum == 8
