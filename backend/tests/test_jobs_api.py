import os
from datetime import date
from decimal import Decimal as D
import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("Needs isolated PostgreSQL.", allow_module_level=True)
from sqlalchemy import select, func, event
from fastapi.testclient import TestClient
import db, main, worker, jobs, portfolio_service
from models import Base, IngestJob, CasUpload, Scheme, NavCache, Transaction, Holding
from casparser.types import CASData


@pytest.fixture(autouse=True)
def clean_database():
    # conftest validates TEST_DATABASE_URL before importing db.
    with db.engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def client():
    with TestClient(main.app, headers={"X-Requested-With": "PortfolioIQ"}) as client:
        yield client


def parsed():
    return CASData.model_validate(
        {
            "cas_type": "DETAILED",
            "file_type": "CAMS",
            "statement_period": {"from": "2023-01-01", "to": "2024-01-01"},
            "investor_info": {
                "name": "T",
                "email": "t@example.test",
                "mobile": "",
                "address": "",
            },
            "folios": [
                {
                    "folio": "123",
                    "amc": "AMC",
                    "schemes": [
                        {
                            "scheme": "Test Direct Growth",
                            "rta": "CAMS",
                            "rta_code": "X",
                            "isin": "INF_TEST",
                            "amfi": "1",
                            "type": "EQUITY",
                            "open": "0",
                            "close": "100",
                            "close_calculated": "100",
                            "valuation": {
                                "date": "2024-01-01",
                                "nav": "12",
                                "value": "1200",
                            },
                            "transactions": [
                                {
                                    "date": "2023-01-01",
                                    "description": "Purchase",
                                    "type": "PURCHASE",
                                    "units": "100",
                                    "amount": "1000",
                                    "nav": "10",
                                    "balance": "100",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def prepared():
    raw = {
        "meta": {"isin_growth": "INF_TEST"},
        "data": [{"date": "01-01-2024", "nav": "12"}],
    }
    return {"1": raw, "__resolved__": {"INF_TEST": {"code": "1", "raw": raw}}}


@pytest.mark.parametrize("legacy_mode", [None, "password", "local"])
def test_password_free_access_including_hosted_and_legacy_config(
    monkeypatch, legacy_mode
):
    monkeypatch.delenv("OWNER_PASSWORD", raising=False)
    if legacy_mode is None:
        monkeypatch.delenv("ACCESS_MODE", raising=False)
    else:
        monkeypatch.setenv("ACCESS_MODE", legacy_mode)
    with TestClient(
        main.app,
        base_url="https://portfolio.example",
        client=("203.0.113.1", 4321),
        headers={"X-Requested-With": "PortfolioIQ"},
    ) as visitor:
        assert visitor.get("/api/session").json() == {
            "authenticated": True,
            "password_required": False,
        }
        assert visitor.get("/api/portfolio").status_code == 200
        assert visitor.get("/api/statement").status_code == 200
        assert (
            visitor.post(
                "/api/upload-cas",
                files={
                    "file": (
                        "test.json",
                        parsed().model_dump_json(by_alias=True),
                        "application/json",
                    )
                },
            ).status_code
            == 202
        )
        assert not visitor.cookies


def test_mutations_still_require_application_header():
    with TestClient(main.app) as visitor:
        assert visitor.get("/api/portfolio").status_code == 200
        assert visitor.post("/api/enrich/retry").status_code == 403
        assert visitor.delete("/api/all-data").status_code == 403


def test_queue_blocks_upload_refresh_and_reset_until_terminal(client):
    response = client.post(
        "/api/upload-cas",
        files={
            "file": (
                "test.json",
                parsed().model_dump_json(by_alias=True),
                "application/json",
            )
        },
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert client.delete("/api/all-data").status_code == 409
    assert (
        client.post("/api/upload-cas", files={"file": ("t.json", "{}")}).status_code
        == 409
    )
    assert client.get("/api/jobs/current").json()["job_id"] == job["job_id"]
    client.post(f"/api/jobs/{job['job_id']}/cancel")
    assert client.get("/api/jobs/current").json() is None
    with db.get_session() as session:
        assert session.get(IngestJob, job["job_id"]).payload is None


def test_atomic_replacement_retains_old_data_on_failure_and_keeps_market_cache(
    monkeypatch,
):
    first = worker.replace_statement(parsed(), b"one", prepared())
    with db.get_session() as session:
        scheme_id = session.scalar(select(Scheme.scheme_id))
        count = session.scalar(select(func.count()).select_from(NavCache))

    async def fail(*a, **kw):
        raise RuntimeError("intentional rollback")

    with monkeypatch.context() as patch:
        patch.setattr(worker.ingestion, "ingest_cas", fail)
        with pytest.raises(RuntimeError):
            worker.replace_statement(parsed(), b"two", prepared())
    with db.get_session() as session:
        assert session.scalar(select(CasUpload.upload_id)) == first.upload_id
        assert session.scalar(select(func.count()).select_from(Transaction)) == 1
    second = worker.replace_statement(parsed(), b"two", prepared())
    assert second.upload_id != first.upload_id
    with db.get_session() as session:
        assert session.scalar(select(Scheme.scheme_id)) == scheme_id
        assert session.scalar(select(func.count()).select_from(NavCache)) == count


def test_worker_resume_after_commit_skips_reparse(monkeypatch):
    result = worker.replace_statement(parsed(), b"one", prepared())
    with db.get_session() as session:
        job = jobs.enqueue(session, "refresh")
        job.status = "processing"
        job.attempts = 1
        session.commit()
        id = job.job_id

    async def noop(*a, **kw):
        return {}

    monkeypatch.setattr(worker.enrichment_bridge, "refresh_enrichment", noop)
    monkeypatch.setattr(worker.benchmark_service, "refresh_nifty50_proxy_nav", noop)
    assert worker.run_once()
    with db.get_session() as session:
        job = session.get(IngestJob, id)
        assert job.status == "ok" and job.attempts == 2
        assert session.scalar(select(CasUpload.upload_id)) == result.upload_id


def test_historical_api_pagination_validation_and_settings_version(client):
    worker.replace_statement(parsed(), b"one", prepared())
    assert client.get("/api/portfolio?valuation_date=bad").status_code == 422
    assert (
        client.get(
            "/api/portfolio/snapshot?start_date=2025-01-01&end_date=2024-01-01"
        ).status_code
        == 422
    )
    tx = client.get("/api/transactions?page_size=1").json()
    assert tx["total"] == 1 and len(tx["scheme_options"]) == 1
    assert not client.get("/api/transactions?types=REDEMPTION").json()["transactions"]
    config = client.get("/api/config").json()
    assert client.post("/api/config", json=config).status_code == 200
    assert client.post("/api/config", json=config).status_code == 409


def test_build_context_query_count_is_constant():
    statement = parsed()
    template = statement.folios[0]
    statement.folios = [template.model_copy(deep=True) for _ in range(50)]
    for i, folio in enumerate(statement.folios):
        folio.folio = str(1000 + i)
    worker.replace_statement(statement, b"fifty-holdings", prepared())
    calls = []

    def record(*args):
        calls.append(args[2])

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        with db.get_session() as session:
            ids = list(session.scalars(select(Holding.holding_id)))
            calls.clear()
            ctx = portfolio_service.build_context(session, ids, date(2024, 1, 1))
            for id in ids:
                portfolio_service.compute_holding_metrics(
                    session, id, date(2024, 1, 1), ctx
                )
            assert len(calls) == 5, calls
    finally:
        event.remove(db.engine, "before_cursor_execute", record)


def test_rebuild_repairs_derived_lots_without_replacing_statement():
    import ingestion
    from models import PurchaseLot, DisposalAllocation

    raw = parsed().model_dump(mode="json", by_alias=True)
    scheme = raw["folios"][0]["schemes"][0]
    scheme["close"] = scheme["close_calculated"] = "20"
    for day, kind, units, amount, balance in [
        ("2023-06-01", "REVERSAL", "-10", "-100", "90"),
        ("2023-06-01", "PURCHASE_SIP", "10", "100", "100"),
        ("2023-07-01", "REDEMPTION", "-100", "-1200", "0"),
        ("2023-08-01", "PURCHASE", "20", "200", "20"),
    ]:
        scheme["transactions"].append(
            {
                "date": day,
                "type": kind,
                "description": kind,
                "units": units,
                "amount": amount,
                "nav": "10",
                "balance": balance,
            }
        )
    worker.replace_statement(CASData.model_validate(raw), b"rebuild-case", prepared())
    with db.get_session() as session:
        upload = session.scalar(select(CasUpload))
        original = (upload.upload_id, upload.file_hash, upload.raw_parsed_json)
        holding = session.scalar(select(Holding))
        holding.data_quality_code = "REVERSAL_UNRESOLVED"
        holding.reconciliation_status = "review_required"
        for lot in session.scalars(select(PurchaseLot)):
            lot.remaining_cost = D("9999")
    for _ in range(2):
        with db.get_session() as session:
            result = ingestion.rebuild_derived_ledger(session)
            assert result == {"holdings": 1, "reconciled": 1, "reversals_matched": 1}
        with db.get_session() as session:
            assert session.scalar(select(func.sum(PurchaseLot.remaining_cost))) == 200
            assert session.scalar(select(func.sum(PurchaseLot.remaining_units))) == 20
            assert (
                session.scalar(select(func.sum(DisposalAllocation.allocated_cost)))
                == 1000
            )
            assert session.scalar(select(func.count(Transaction.transaction_id))) == 5
            upload = session.scalar(select(CasUpload))
            assert (
                upload.upload_id,
                upload.file_hash,
                upload.raw_parsed_json,
            ) == original


def test_stale_attempt_cannot_write_or_replace():
    worker.replace_statement(parsed(), b"one", prepared())
    with db.get_session() as session:
        job = jobs.enqueue(session, "refresh")
        job.status = "processing"
        job.attempts = 2
        session.commit()
        id = job.job_id
    with db.get_session() as session:
        with pytest.raises(RuntimeError, match="newer worker"):
            jobs.set_stage(session, id, "stale", attempt=1)
    with pytest.raises(RuntimeError, match="newer worker"):
        worker.replace_statement(parsed(), b"two", prepared(), id, 1)


def test_worker_upload_uses_parser_subprocess_and_publishes_readiness(
    client, monkeypatch
):
    async def prepare(*a, **kw):
        return prepared()

    async def refresh(session, schemes, **kw):
        with db.get_session() as check:
            job = jobs.active_job(check)
            assert job.ready and job.status == "processing" and job.payload is None

    async def noop(*a, **kw):
        pass

    monkeypatch.setattr(worker, "prepare_schemes", prepare)
    monkeypatch.setattr(worker.enrichment_bridge, "refresh_enrichment", refresh)
    monkeypatch.setattr(worker.benchmark_service, "refresh_nifty50_proxy_nav", noop)
    response = client.post(
        "/api/upload-cas",
        files={"file": ("t.json", parsed().model_dump_json(by_alias=True))},
    )
    assert worker.run_once()
    status = client.get("/api/upload-status/" + str(response.json()["job_id"])).json()
    assert status["status"] == "ok", status
    assert status["ready"] and status["dataset_id"]
    assert client.get("/api/portfolio?include_exposure=true").json()["exposure"][
        "top_funds"
    ]


def test_failed_enrichment_chunk_replays_nav_and_metadata(monkeypatch):
    import enrichment_bridge, nav_service
    from models import EnrichmentCache

    worker.replace_statement(parsed(), b"one", prepared())
    with db.get_session() as session:
        sid = session.scalar(select(Scheme.scheme_id))
    original = nav_service.store_nav_points
    calls = 0

    def fail_once(*a, **kw):
        nonlocal calls
        calls += 1
        result = original(*a, **kw)
        if calls == 1:
            raise RuntimeError("transient commit failure")
        return result

    monkeypatch.setattr(nav_service, "store_nav_points", fail_once)
    payload = {
        "nav_status": "ok",
        "metadata_status": "ok",
        "analytics_status": "ok",
        "nav_as_of": "2024-01-02",
    }
    with db.get_session() as session:
        enrichment_bridge._persist_chunk(
            session, [(sid, payload, [(date(2024, 1, 2), D("13"))], None)]
        )
    with db.get_session() as session:
        assert nav_service.get_latest_nav(session, sid).nav == 13
        assert session.scalar(select(EnrichmentCache)).payload == payload
    assert calls == 2


def test_capital_gains_pages_keep_full_year_totals(client, monkeypatch):
    from types import SimpleNamespace as NS
    import gains_service_db

    rows = [
        NS(
            fy="FY2024-25",
            scheme_name="Test",
            isin="INF",
            fund_type="EQUITY",
            advisor_arn=None,
            acquired_date=date(2023, 1, 1),
            sold_date=date(2024, 6, 1),
            units=D(1),
            acquisition_value=D(10),
            sale_value=D(12),
            gain=D(2),
            gain_type="LTCG",
            ltcg=D(2),
            stcg=D(0),
        )
        for _ in range(150)
    ]
    monkeypatch.setattr(gains_service_db, "realized_gains", lambda *a: (rows, []))
    result = client.get("/api/capital-gains?page=2").json()
    assert len(result["gains"]) == 50 and result["total"] == 150
    assert D(str(result["summary"]["net"])) == 300


def test_real_disposal_financial_year_roundtrips_to_export(client):
    from casparser.types import TransactionData

    statement = parsed()
    statement.statement_period.to = "2024-06-01"
    fund = statement.folios[0].schemes[0]
    fund.close = D(0)
    fund.close_calculated = D(0)
    fund.transactions.append(
        TransactionData(
            date=date(2024, 6, 1),
            description="Redemption",
            type="REDEMPTION",
            units=D(-100),
            amount=D(-1200),
            nav=D(12),
            balance=D(0),
        )
    )
    worker.replace_statement(statement, b"gain-fixture", prepared())
    response = client.get("/api/capital-gains").json()
    assert response["selected_fy"] == "FY2024-25", response
    assert response["total"] == 1
    assert client.get("/api/capital-gains?fy=FY2024-25").status_code == 200
    exported = client.get("/api/capital-gains/112a.csv?fy=FY2024-25")
    assert exported.status_code == 200, exported.text
    assert "INF_TEST" in exported.text


def test_confirming_legacy_identity_replaces_unverified_cached_nav():
    worker.replace_statement(parsed(), b"one", prepared())
    with db.get_session() as session:
        scheme = session.scalar(select(Scheme))
        scheme.identity_confirmed = False
        nav = session.scalar(select(NavCache))
        nav.nav = D(999)
    worker.replace_statement(parsed(), b"two", prepared())
    with db.get_session() as session:
        assert session.scalar(select(NavCache.nav)) == 12
