"""Ordered, transactional schema changes; no production data is deleted."""

from sqlalchemy import text

MIGRATIONS = [
    (
        1,
        [
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS debug_stage VARCHAR",
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS kind VARCHAR DEFAULT 'upload' NOT NULL",
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP",
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0 NOT NULL",
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS payload TEXT",
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS dataset_id INTEGER",
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS ready BOOLEAN DEFAULT false NOT NULL",
            "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN DEFAULT false NOT NULL",
            "ALTER TABLE schemes ADD COLUMN IF NOT EXISTS identity_confirmed BOOLEAN DEFAULT false NOT NULL",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS opening_units NUMERIC(28,8) DEFAULT 0 NOT NULL",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS opening_date DATE",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS closing_units NUMERIC(28,8)",
            "ALTER TABLE holdings ADD COLUMN IF NOT EXISTS coverage_date DATE",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ledger_position INTEGER",
            "UPDATE transactions SET ledger_position = transaction_id WHERE ledger_position IS NULL",
            "CREATE INDEX IF NOT EXISTS ix_lots_holding ON purchase_lots(holding_id)",
            "CREATE INDEX IF NOT EXISTS ix_holdings_advisor ON holdings(advisor_arn)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_ingest_jobs_one_processing ON ingest_jobs(status) WHERE status = 'processing'",
        ],
    )
]


def migrate(conn):
    conn.execute(text("SELECT pg_advisory_xact_lock(74102001)"))
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_versions (version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    versions = set(conn.execute(text("SELECT version FROM schema_versions")).scalars())
    for version, statements in MIGRATIONS:
        if version not in versions:
            for statement in statements:
                conn.execute(text(statement))
            conn.execute(
                text("INSERT INTO schema_versions(version) VALUES (:v)"), {"v": version}
            )
