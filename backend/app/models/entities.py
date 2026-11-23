import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120))
    service_identifier: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64))
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    sample_rate: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ModelPricing(Base):
    __tablename__ = "model_pricing"
    __table_args__ = (
        UniqueConstraint("project_id", "provider", "model", "effective_date"),
        Index("ix_pricing_lookup", "project_id", "provider", "model", "effective_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(255))
    effective_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    input_per_million: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    output_per_million: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    cached_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    source: Mapped[str] = mapped_column(String(255), default="custom")
    version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("project_id", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    period: Mapped[str] = mapped_column(String(16), default="monthly")
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    metric: Mapped[str] = mapped_column(String(32))
    threshold: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (Index("ix_alert_project_created", "project_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True
    )
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    metric: Mapped[str] = mapped_column(String(32))
    observed_value: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    threshold: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Trace(Base):
    __tablename__ = "traces"
    __table_args__ = (
        UniqueConstraint("project_id", "trace_id"),
        Index("ix_trace_project_started", "project_id", "started_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    root_operation: Mapped[str] = mapped_column(String(255), default="unknown")
    service_name: Mapped[str] = mapped_column(String(255), default="unknown")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default="UNSET")
    spans: Mapped[list["Span"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (
        UniqueConstraint("project_id", "trace_id", "span_id"),
        Index("ix_span_trace", "project_id", "trace_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    trace_pk: Mapped[int] = mapped_column(ForeignKey("traces.id", ondelete="CASCADE"))
    trace_id: Mapped[str] = mapped_column(String(32))
    span_id: Mapped[str] = mapped_column(String(16))
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    operation_name: Mapped[str] = mapped_column(String(255))
    span_kind: Mapped[str] = mapped_column(String(32), default="INTERNAL")
    service_name: Mapped[str] = mapped_column(String(255), default="unknown")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default="UNSET")
    error_information: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    pricing_version: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DiagnosticFinding(Base):
    __tablename__ = "diagnostic_findings"
    __table_args__ = (UniqueConstraint("project_id", "trace_id", "rule_id", "span_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rule_id: Mapped[str] = mapped_column(String(8))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, object]] = mapped_column(JSON)
    recommendation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
