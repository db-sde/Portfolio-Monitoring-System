"""
PortfolioIQ — models.py

SQLAlchemy ORM schema implementing spec section 5.2's recommended
persistent tables, plus a `holdings` table that materialises the
holding_key composite (investor + folio + scheme + plan + option, spec
5.1) as a real foreign key rather than duplicating that 5-column tuple
across transactions/purchase_lots/disposal_allocations.

Also includes config_groups/config_investors (replacing config.json's
groups -> investors -> ARN-labels tree) so Settings persists through a
Render redeploy the same way everything else here now does.

Decimal precision throughout per spec 5.3 — Numeric(28, 8) for
units/NAV, Numeric(28, 4) for money amounts, never Float. Postgres's
NUMERIC is arbitrary-precision and round-trips through Python's Decimal
natively via psycopg3, so this isn't just "fewer rounding errors than
float" — it's the same guarantee the spec asks for.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


UNITS = Numeric(28, 8)
NAV = Numeric(28, 8)
MONEY = Numeric(28, 4)
PCT = Numeric(9, 4)


def _now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------- config ----
# Replaces config.json's groups -> investors -> arns/arn_labels tree.

class ConfigGroup(Base):
    __tablename__ = "config_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_name: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    investors: Mapped[list["ConfigInvestor"]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="ConfigInvestor.sort_order"
    )


class ConfigInvestor(Base):
    __tablename__ = "config_investors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("config_groups.id", ondelete="CASCADE"))
    investor_name: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    group: Mapped[ConfigGroup] = relationship(back_populates="investors")
    arns: Mapped[list["ConfigInvestorArn"]] = relationship(back_populates="investor", cascade="all, delete-orphan")


class ConfigInvestorArn(Base):
    """One row per ARN code attributed to a config investor, with its
    optional display label — the arns[]/arn_labels{} pair in config.json
    collapsed into one row instead of two parallel structures that could
    drift out of sync with each other."""
    __tablename__ = "config_investor_arns"
    __table_args__ = (UniqueConstraint("investor_id", "arn", name="uq_investor_arn"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investor_id: Mapped[int] = mapped_column(ForeignKey("config_investors.id", ondelete="CASCADE"))
    arn: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String)

    investor: Mapped[ConfigInvestor] = relationship(back_populates="arns")


class Preference(Base):
    """Single-row-per-key preferences (show_zero_value_funds, primary_benchmark,
    show_benchmark_comparison, ...) — a key/value table rather than one
    fixed-column row, so a new preference doesn't need a schema change."""
    __tablename__ = "preferences"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class IngestJob(Base):
    """Tracks one upload's wipe+ingest as it runs in the background.
    upload_cas used to hold the HTTP connection open for the entire
    ingest — fine for a small statement, but a real one with 50+ schemes
    means minutes of sequential mfapi.in resolution calls (each call's
    own latency is highly variable, 1-15s, with retries on failure), long
    enough that Render's own reverse proxy gave up and returned a 502 to
    the client before the ingest even finished — independent of whether
    the ingest itself was correct. upload_cas now creates this row and
    returns almost immediately (status "processing"); the real wipe+
    ingest work happens after, off the main event loop, and the frontend
    polls GET /api/upload-status/{job_id} until status leaves
    "processing". No foreign keys in or out — deliberately standalone so
    it never needs to participate in the FK-ordered wipe/delete
    sequences the rest of the schema does; upload_cas simply clears any
    previous row before creating a new one.

    The partial unique index below is the actual guarantee behind
    upload_cas's concurrent-upload lock, not just the application-level
    check it also does for a fast, friendly rejection. Reproduced live:
    a client-side timeout on one upload didn't stop it running server-
    side, and a retry a moment later raced against it — two concurrent
    wipes on the same tables, one hit a foreign-key violation mid-
    delete. An in-process check alone has the same race (two requests
    can both see "nothing running yet" before either has written its
    own row); Postgres enforcing "at most one processing row, ever" at
    the index level is what actually closes it — the loser's INSERT
    fails with an IntegrityError upload_cas catches and turns into a
    clean 409, regardless of how the two requests happened to interleave."""
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        Index(
            "uq_ingest_jobs_one_processing", "status",
            unique=True, postgresql_where=text("status = 'processing'"),
        ),
    )

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String, default="processing")  # processing | ok | error
    result_json: Mapped[Optional[dict]] = mapped_column(JSON)
    error_detail: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# ---------------------------------------------------------- CAS ingestion ----

class CasUpload(Base):
    __tablename__ = "cas_uploads"
    __table_args__ = (UniqueConstraint("file_hash", name="uq_cas_upload_file_hash"),)

    upload_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("config_investors.id"))
    file_hash: Mapped[str] = mapped_column(String, nullable=False)
    issuer: Mapped[Optional[str]] = mapped_column(String)
    period_from: Mapped[Optional[date]] = mapped_column(Date)
    period_to: Mapped[Optional[date]] = mapped_column(Date)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    parse_status: Mapped[str] = mapped_column(String, default="OK")
    # Full unmodified casparser output, kept for audit (spec 6.2 step 2) —
    # never read back into a live calculation, only for support/debugging.
    raw_parsed_json: Mapped[Optional[dict]] = mapped_column(JSON)


class Folio(Base):
    __tablename__ = "folios"
    __table_args__ = (UniqueConstraint("investor_id", "normalized_folio", "amc", name="uq_folio_identity"),)

    folio_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("config_investors.id"))
    normalized_folio: Mapped[str] = mapped_column(String, nullable=False)
    amc: Mapped[Optional[str]] = mapped_column(String)
    pan_hash: Mapped[Optional[str]] = mapped_column(String)


class Scheme(Base):
    """Canonical scheme master — one row per real fund/plan/option, the
    target every CAS scheme reference and every scheme_alias resolves to."""
    __tablename__ = "schemes"

    scheme_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    isin: Mapped[Optional[str]] = mapped_column(String, index=True)
    amfi_code: Mapped[Optional[str]] = mapped_column(String, index=True)
    rta_code: Mapped[Optional[str]] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plan: Mapped[Optional[str]] = mapped_column(String)  # Direct / Regular
    option: Mapped[Optional[str]] = mapped_column(String)  # Growth / IDCW
    asset_class: Mapped[Optional[str]] = mapped_column(String)  # EQUITY / DEBT / OTHER
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SchemeAlias(Base):
    """Maps a historical name/AMFI-code/RTA-code to its canonical scheme —
    what enrichment.py's ISIN-based stale-code recovery persists here
    instead of re-discovering it on every enrichment run (spec 7.2:
    'Persist the alias mapping only after confirming the ISIN and
    plan/option match... the recovery path must be logged')."""
    __tablename__ = "scheme_aliases"
    __table_args__ = (UniqueConstraint("alias_type", "alias_value", name="uq_scheme_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias_type: Mapped[str] = mapped_column(String, nullable=False)  # "amfi_code" | "rta_code" | "name"
    alias_value: Mapped[str] = mapped_column(String, nullable=False)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("schemes.scheme_id"))
    confidence: Mapped[str] = mapped_column(String, default="confirmed")  # confirmed | needs_review
    source: Mapped[Optional[str]] = mapped_column(String)  # e.g. "isin_recovery" | "manual"
    resolved_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Holding(Base):
    """Materialised holding_key (spec 5.1): one row per distinct
    investor + folio + scheme + plan + option combination. Direct/Regular
    and Growth/IDCW are always different holdings, never merged."""
    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("folio_id", "scheme_id", "plan", "option", name="uq_holding_key"),
    )

    holding_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folio_id: Mapped[int] = mapped_column(ForeignKey("folios.folio_id"))
    scheme_id: Mapped[int] = mapped_column(ForeignKey("schemes.scheme_id"))
    plan: Mapped[Optional[str]] = mapped_column(String)
    option: Mapped[Optional[str]] = mapped_column(String)
    advisor_arn: Mapped[Optional[str]] = mapped_column(String)
    reconciliation_status: Mapped[str] = mapped_column(String, default="reconciled")
    # reconciled | review_required | incomplete_opening_history
    # data_quality_code: the specific spec-17 error code behind a
    # review_required status (SCHEME_UNRESOLVED | FIFO_SHORTFALL |
    # CAS_RECONCILIATION_FAILED) — kept separate from
    # reconciliation_status so "why" isn't collapsed into one generic
    # bucket the API can't distinguish between.
    data_quality_code: Mapped[Optional[str]] = mapped_column(String)
    data_quality_detail: Mapped[Optional[str]] = mapped_column(Text)

    folio: Mapped[Folio] = relationship()
    scheme: Mapped[Scheme] = relationship()


class Transaction(Base):
    """Immutable normalised CAS ledger row (spec 6, 8.1). Never mutated
    after import except an explicit REVERSAL transaction referencing it —
    correction happens by appending, not by editing history in place."""
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint(
            "holding_id", "date", "type", "amount", "units", "nav", "occurrence_index",
            name="uq_transaction_fingerprint",
        ),
        Index("ix_transactions_holding_date", "holding_id", "date"),
    )

    transaction_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holding_id: Mapped[int] = mapped_column(ForeignKey("holdings.holding_id"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    amount: Mapped[Optional[Decimal]] = mapped_column(MONEY)
    units: Mapped[Optional[Decimal]] = mapped_column(UNITS)
    nav: Mapped[Optional[Decimal]] = mapped_column(NAV)
    balance: Mapped[Optional[Decimal]] = mapped_column(UNITS)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # GIFT_IN/GIFT_OUT only: the counterparty folio named in the CAS
    # description, letting a donor's statement be linked to the donee's
    # across two separate CAS uploads (spec 8.3). Null for every other
    # transaction type.
    gift_folio: Mapped[Optional[str]] = mapped_column(String)
    source_upload_id: Mapped[Optional[int]] = mapped_column(ForeignKey("cas_uploads.upload_id"))
    # Deterministic tie-breaker for legitimate same-day identical SIP rows
    # (spec 6.3) — never used to distinguish real differences, only to
    # keep two truly-identical rows from colliding into one unique key.
    occurrence_index: Mapped[int] = mapped_column(Integer, default=0)
    reverses_transaction_id: Mapped[Optional[int]] = mapped_column(ForeignKey("transactions.transaction_id"))

    holding: Mapped[Holding] = relationship(foreign_keys=[holding_id])


class PurchaseLot(Base):
    """One open or closed FIFO acquisition lot (spec 8.2). remaining_units
    decreases as disposal_allocations consume it; remaining_cost tracks
    proportionally so a partially-consumed lot's cost basis stays exact."""
    __tablename__ = "purchase_lots"

    lot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holding_id: Mapped[int] = mapped_column(ForeignKey("holdings.holding_id"))
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.transaction_id"))
    acquired_date: Mapped[date] = mapped_column(Date, nullable=False)
    original_units: Mapped[Decimal] = mapped_column(UNITS, nullable=False)
    remaining_units: Mapped[Decimal] = mapped_column(UNITS, nullable=False)
    purchase_nav: Mapped[Decimal] = mapped_column(NAV, nullable=False)
    purchase_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    remaining_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stamp_duty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    # PURCHASE_SIP/PURCHASE -> normal FIFO lot; SWITCH_IN/_MERGER -> lot
    # created at the destination; DIVIDEND_REINVEST -> lot with 0 cash cost.
    origin_type: Mapped[str] = mapped_column(String, nullable=False)

    holding: Mapped[Holding] = relationship()


class DisposalAllocation(Base):
    """One FIFO link between a disposal transaction and the lot(s) it
    consumed (spec 8.2, 12.2) — a single redemption order can span
    several rows here if it consumed multiple lots, which is exactly why
    Capital Gains' row grain is "one allocation," not "one redemption.\""""
    __tablename__ = "disposal_allocations"

    allocation_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disposal_transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.transaction_id"))
    lot_id: Mapped[int] = mapped_column(ForeignKey("purchase_lots.lot_id"))
    allocated_units: Mapped[Decimal] = mapped_column(UNITS, nullable=False)
    allocated_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    sale_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realized_gain: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    sold_date: Mapped[date] = mapped_column(Date, nullable=False)

    lot: Mapped[PurchaseLot] = relationship()


# --------------------------------------------------------------- NAV data ----

class NavCache(Base):
    """Resolved scheme NAV points (spec 7, 7.3) — immutable once a
    historical point is stored; only the latest date's row is ever
    refreshed."""
    __tablename__ = "nav_cache"
    __table_args__ = (UniqueConstraint("scheme_id", "nav_date", name="uq_nav_cache_point"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("schemes.scheme_id"))
    nav_date: Mapped[date] = mapped_column(Date, nullable=False)
    nav: Mapped[Decimal] = mapped_column(NAV, nullable=False)
    source: Mapped[str] = mapped_column(String, default="mfapi.in")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class EnrichmentCache(Base):
    """Optional per-scheme metadata (category, expense ratio, fund
    manager, risk ratios) and — when a source ever provides it — cap
    allocation. `payload` is the full computed/fetched blob; `status`
    lets a consumer distinguish "fetched, has data" from "fetched,
    genuinely unavailable" without re-parsing payload."""
    __tablename__ = "enrichment_cache"
    __table_args__ = (UniqueConstraint("scheme_id", "provider", name="uq_enrichment_cache_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("schemes.scheme_id"))
    provider: Mapped[str] = mapped_column(String, nullable=False)  # mfapi.in | captnemo | mfdata.in
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    data_as_of: Mapped[Optional[date]] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    status: Mapped[str] = mapped_column(String, default="ok")  # ok | unavailable | error


# --------------------------------------------------------------- benchmark ----

class BenchmarkDefinition(Base):
    __tablename__ = "benchmark_definitions"

    benchmark_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "Nifty 50"
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "index_fund_proxy" | "tri_index" (not available yet)
    source_code: Mapped[Optional[str]] = mapped_column(String)  # e.g. AMFI code "120716" for the UTI proxy fund
    # Spec 14.3: must never let a proxy be mistaken for the real TRI series.
    proxy_disclosure: Mapped[Optional[str]] = mapped_column(Text)


class SchemeBenchmarkMap(Base):
    __tablename__ = "scheme_benchmark_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("schemes.scheme_id"))
    benchmark_id: Mapped[int] = mapped_column(ForeignKey("benchmark_definitions.benchmark_id"))
    effective_from: Mapped[Optional[date]] = mapped_column(Date)
    effective_to: Mapped[Optional[date]] = mapped_column(Date)
    source: Mapped[Optional[str]] = mapped_column(String)


class BenchmarkPoint(Base):
    __tablename__ = "benchmark_points"
    __table_args__ = (UniqueConstraint("benchmark_id", "date", name="uq_benchmark_point"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    benchmark_id: Mapped[int] = mapped_column(ForeignKey("benchmark_definitions.benchmark_id"))
    date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(NAV, nullable=False)
    source: Mapped[str] = mapped_column(String, default="mfapi.in")


# -------------------------------------------------------------- gains cache --
# Capital gains are derived from disposal_allocations at query time
# (holding/scheme/fund_type join), so no separate gains_data.json-style
# cache table is needed — one fewer place for the same number to drift.
