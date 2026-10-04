"""
PortfolioIQ — db.py

SQLAlchemy engine/session setup for the Neon Postgres migration (spec
section 5.2/18). Replaces the JSON-file storage
(cas_data.json/config.json/gains_data.json/enrichment_cache.json) that
didn't survive Render's ephemeral-disk restarts.

DATABASE_URL is read from the environment (backend/.env locally via
python-dotenv, a real Render env var in production — see main.py's
load_dotenv() call, which must run before this module is imported).
No fallback to SQLite/JSON here: once this module is wired in, Postgres
is the only store, matching the spec's "no CAS valuation as source of
truth" philosophy — a partial migration with two stores would just
reintroduce the exact staleness bugs this whole rewrite exists to kill.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from models import Base


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy backend/.env.example to backend/.env "
            "and paste in your Neon connection string (or set it as a real env "
            "var in Render's dashboard for production)."
        )
    # SQLAlchemy's psycopg3 dialect needs the "+psycopg" driver marker;
    # Neon's own connection strings (and most docs/consoles) hand out a
    # plain "postgresql://" URL, so this is a compatibility rewrite, not
    # a behaviour change — same database, same credentials, same host.
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# pool_pre_ping: Neon's free tier suspends the compute endpoint after a
# period of inactivity and wakes it on the next connection — a pooled
# connection that's been idle across that suspend would otherwise come
# back as a dead socket and fail the first real query with a confusing
# error instead of transparently reconnecting.
engine = create_engine(_database_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    from migrations import migrate

    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(74102001)"))
        Base.metadata.create_all(bind=connection)
        migrate(connection)


@contextmanager
def get_session(*, consistent=False) -> Iterator[Session]:
    session = SessionLocal()
    try:
        if consistent:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
