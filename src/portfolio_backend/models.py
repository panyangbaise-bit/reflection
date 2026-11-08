from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from portfolio_backend.db import now

AMOUNT = Numeric(28, 8)
QUANTITY = Numeric(28, 10)
PRICE = Numeric(28, 12)


class Base(DeclarativeBase):
    pass


class Identified:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Tenant:
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)


class User(Identified, Base):
    __tablename__ = "users"
    apple_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    phone: Mapped[str | None] = mapped_column(String(16), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_setup_required: Mapped[bool] = mapped_column(default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(24), default="active")
    store_environment: Mapped[str] = mapped_column(String(16))


class LoginChallenge(Identified, Base):
    __tablename__ = "login_challenges"
    nonce_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(default=False)


class AuthSession(Identified, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    family_id: Mapped[UUID] = mapped_column(index=True)
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Portfolio(Identified, Tenant, Base):
    __tablename__ = "portfolios"
    __table_args__ = (UniqueConstraint("user_id", "id"), UniqueConstraint("user_id"))
    name: Mapped[str] = mapped_column(String(100), default="我的组合")
    version: Mapped[int] = mapped_column(default=0)


class Account(Identified, Tenant, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "name"),
        ForeignKeyConstraint(["user_id", "portfolio_id"], ["portfolios.user_id", "portfolios.id"]),
    )
    portfolio_id: Mapped[UUID]
    name: Mapped[str] = mapped_column(String(100))


class Instrument(Identified, Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("market", "symbol", "asset_class"),
        CheckConstraint("asset_class in ('stock','etf','fund','listed_fund','gold')"),
        CheckConstraint("market in ('CN','HK','US')"),
        CheckConstraint("currency in ('CNY','HKD','USD')"),
        CheckConstraint("freshness_seconds > 0"),
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(160))
    search_aliases: Mapped[str] = mapped_column(Text, default="", server_default="")
    market: Mapped[str] = mapped_column(String(8))
    asset_class: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    provider: Mapped[str] = mapped_column(String(64))
    provider_symbol: Mapped[str | None] = mapped_column(String(64))
    quote_kind: Mapped[str] = mapped_column(String(24))
    enabled: Mapped[bool] = mapped_column(default=False)
    commercial_display: Mapped[bool] = mapped_column(default=False)
    freshness_seconds: Mapped[int] = mapped_column(default=120)
    unsupported_reason: Mapped[str | None] = mapped_column(String(255))


class CatalogSyncState(Base):
    __tablename__ = "catalog_sync_states"
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(64))


class LatestQuote(Base):
    __tablename__ = "latest_quotes"
    __table_args__ = (CheckConstraint("price > 0"),)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    price: Mapped[Decimal] = mapped_column(PRICE)
    previous_price: Mapped[Decimal | None] = mapped_column(PRICE)
    previous_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(24))
    provider: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    market_state: Mapped[str] = mapped_column(String(16), default="open")


class FXRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (CheckConstraint("rate > 0"),)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(PRICE)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(64))


class NewsItem(Identified, Base):
    __tablename__ = "news_items"
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(String(2048))
    provider: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(300))
    excerpt: Mapped[str] = mapped_column(String(600))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class InstrumentNews(Base):
    __tablename__ = "instrument_news"
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    news_id: Mapped[UUID] = mapped_column(
        ForeignKey("news_items.id", ondelete="CASCADE"), primary_key=True
    )


class ExternalBudget(Base):
    __tablename__ = "external_budgets"
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    reserved_calls: Mapped[int] = mapped_column(default=0)


class Position(Identified, Tenant, Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("account_id", "instrument_id"),
        ForeignKeyConstraint(["user_id", "account_id"], ["accounts.user_id", "accounts.id"]),
        CheckConstraint("quantity >= 0 and cost_pool >= 0 and version > 0"),
        CheckConstraint("position_type in ('core','satellite')", name="ck_positions_type"),
    )
    account_id: Mapped[UUID]
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), index=True)
    position_type: Mapped[str] = mapped_column(String(16), default="core", server_default="core")
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    cost_pool: Mapped[Decimal] = mapped_column(AMOUNT)
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, default=0)
    version: Mapped[int] = mapped_column(default=1)
    last_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history_complete: Mapped[bool] = mapped_column(default=False)
    opening_cost_source: Mapped[str | None] = mapped_column(String(64))
    opening_quote_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LedgerEvent(Identified, Tenant, Base):
    __tablename__ = "ledger_events"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "position_id"], ["positions.user_id", "positions.id"]),
        UniqueConstraint("position_id", "position_version"),
        CheckConstraint("kind in ('opening_snapshot','buy','sell')"),
        CheckConstraint("quantity > 0 and price > 0 and fee >= 0"),
        Index("ix_ledger_user_time", "user_id", "created_at", "id"),
    )
    position_id: Mapped[UUID]
    position_version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(24))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fee: Mapped[Decimal] = mapped_column(AMOUNT, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Idempotency(Identified, Tenant, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "scope", "key"),)
    scope: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)


class Consent(Identified, Tenant, Base):
    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("user_id", "purpose"),)
    purpose: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    disclosure_version: Mapped[str] = mapped_column(String(32))
    granted: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Report(Identified, Tenant, Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "kind", "period"),
        Index("ix_reports_user_time", "user_id", "created_at", "id"),
    )
    kind: Mapped[str] = mapped_column(String(16))
    period: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSON)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    free_used: Mapped[int] = mapped_column(default=0)
    free_reserved: Mapped[int] = mapped_column(default=0)


class Conversation(Identified, Tenant, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        UniqueConstraint("user_id", "report_id"),
        ForeignKeyConstraint(["user_id", "report_id"], ["reports.user_id", "reports.id"]),
    )
    report_id: Mapped[UUID | None]
    title: Mapped[str] = mapped_column(String(100))


class Message(Identified, Tenant, Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "conversation_id"], ["conversations.user_id", "conversations.id"]
        ),
        Index("ix_messages_conversation_time", "conversation_id", "created_at", "id"),
        Index("ix_messages_user_time", "user_id", "created_at", "id"),
    )
    conversation_id: Mapped[UUID]
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    charts: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")


class AIRun(Identified, Tenant, Base):
    __tablename__ = "ai_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "id"),
        ForeignKeyConstraint(
            ["user_id", "conversation_id"], ["conversations.user_id", "conversations.id"]
        ),
        ForeignKeyConstraint(["user_id", "report_id"], ["reports.user_id", "reports.id"]),
    )
    conversation_id: Mapped[UUID]
    report_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(24), default="queued")
    used_free_credit: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    answer: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentToolCall(Identified, Tenant, Base):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "run_id"], ["ai_runs.user_id", "ai_runs.id"]),
        Index("ix_tool_calls_run_time", "run_id", "created_at", "id"),
    )
    run_id: Mapped[UUID | None]
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    round: Mapped[int]
    tool_name: Mapped[str] = mapped_column(String(100))
    call_id: Mapped[str] = mapped_column(String(200))
    triggered_by: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(default=0)


class WatchlistItem(Identified, Tenant, Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id"),
        CheckConstraint("priority between 1 and 5"),
        CheckConstraint("target_price is null or target_price > 0"),
    )
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"))
    priority: Mapped[int] = mapped_column(default=3)
    target_price: Mapped[Decimal | None] = mapped_column(PRICE)
    note: Mapped[str] = mapped_column(String(1000), default="")


class MemoryEvidence(Identified, Tenant, Base):
    __tablename__ = "memory_evidence"
    __table_args__ = (UniqueConstraint("user_id", "source_type", "source_id", "version"),)
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Device(Identified, Tenant, Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("token_hash"),)
    token_hash: Mapped[str] = mapped_column(String(64))
    token: Mapped[str] = mapped_column(String(255))
    environment: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(default=True)


class Preferences(Tenant, Base):
    __tablename__ = "preferences"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    overview_order: Mapped[list | None] = mapped_column(JSON)
    layout_version: Mapped[int] = mapped_column(default=0, server_default="0")
    daily_report: Mapped[bool] = mapped_column(default=False)
    weekly_report: Mapped[bool] = mapped_column(default=False)
    report_push: Mapped[bool] = mapped_column(default=False)
    news_push: Mapped[bool] = mapped_column(default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")


class TradingDiscipline(Tenant, Base):
    __tablename__ = "trading_discipline"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Identified, Base):
    """Control-plane metadata. Payload contains IDs, never financial or chat content."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_due", "status", "due_at"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    attempt: Mapped[int] = mapped_column(default=0)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fence: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(64))


class Outbox(Identified, Tenant, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (UniqueConstraint("dedupe_key"),)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeletionRequest(Identified, Base):
    __tablename__ = "deletion_requests"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt_hash: Mapped[str] = mapped_column(String(64), unique=True)


class AuditLog(Identified, Base):
    __tablename__ = "admin_audit_logs"
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


TENANT_TABLES = [
    table.name
    for table in Base.metadata.sorted_tables
    if any(
        isinstance(base, type)
        and issubclass(base, Tenant)
        and getattr(base, "__tablename__", None) == table.name
        for base in Base.__subclasses__()
    )
]
