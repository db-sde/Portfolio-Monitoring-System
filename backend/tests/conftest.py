"""Tests never load the application .env or use its DATABASE_URL."""

import os
import sys
from pathlib import Path
import pytest
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "casparser"))
url = os.environ.get("TEST_DATABASE_URL")
if url and not (make_url(url).database or "").startswith("portfolioiq_test"):
    raise RuntimeError(
        "TEST_DATABASE_URL must name a disposable portfolioiq_test* database."
    )
os.environ["DATABASE_URL"] = (
    url or "postgresql://localhost:1/portfolioiq_test_unavailable"
)
os.environ["APP_SECRET"] = "isolated-test-secret-not-for-production-123456789"
os.environ["OWNER_PASSWORD"] = "isolated-test-owner-password"
os.environ["COOKIE_SECURE"] = "false"
os.environ["ACCESS_MODE"] = "password"


@pytest.fixture(scope="session", autouse=True)
def schema():
    if url:
        import db

        db.init_db()


@pytest.fixture(autouse=True)
def offline_providers(monkeypatch):
    import httpx

    async def blocked(self, *args, **kwargs):
        raise AssertionError("Tests must mock external providers.")

    monkeypatch.setattr(httpx.AsyncClient, "get", blocked)
