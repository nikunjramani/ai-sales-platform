"""Orders tables, split from platform/persistence/models.py (ADR 0026).

They register on the one Base.metadata that Alembic reads (migrations/env.py imports them).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import Base


class OrderRow(Base):
    """One order per quotation; copies the accepted version's totals (lines in order_lines)."""

    __tablename__ = "orders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "quotation_id", "version"],
            [
                "quotation_versions.tenant_id",
                "quotation_versions.quotation_id",
                "quotation_versions.version",
            ],
            name="fk_orders_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "erp_connection_id"],
            ["connections.tenant_id", "connections.id"],
            name="fk_orders_connection",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_orders_tenant_id_id"),
        UniqueConstraint("tenant_id", "quotation_id", name="uq_orders_quotation"),
        UniqueConstraint("tenant_id", "order_number", name="uq_orders_number"),
        Index("ix_orders_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    order_number: Mapped[str] = mapped_column(String(16))
    quotation_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    response_id: Mapped[UUID | None] = mapped_column(ForeignKey("quotation_responses.id"))
    status: Mapped[str] = mapped_column(String(16))
    hold_reason: Mapped[str | None] = mapped_column(String(500))
    fulfilment: Mapped[str] = mapped_column(String(8))
    erp_connection_id: Mapped[UUID | None] = mapped_column()
    erp_customer: Mapped[str | None] = mapped_column(String(140))
    erp_order_id: Mapped[str | None] = mapped_column(String(140))
    currency: Mapped[str] = mapped_column(String(3))
    subtotal: Mapped[Decimal] = mapped_column(Numeric())
    tax: Mapped[Decimal] = mapped_column(Numeric())
    total: Mapped[Decimal] = mapped_column(Numeric())
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderLineRow(Base):
    """Immutable copy of one accepted quotation line."""

    __tablename__ = "order_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["orders.tenant_id", "orders.id"],
            name="fk_order_lines_order",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(primary_key=True)
    line_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[Decimal] = mapped_column(Numeric())
    unit: Mapped[str] = mapped_column(String(32))
    unit_price: Mapped[Decimal] = mapped_column(Numeric())
    line_total: Mapped[Decimal] = mapped_column(Numeric())


class OrderEventRow(Base):
    """Append-only order timeline."""

    __tablename__ = "order_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["orders.tenant_id", "orders.id"],
            name="fk_order_events_order",
        ),
        Index("ix_order_events_order", "tenant_id", "order_id", "occurred_at"),
        Index(
            "uq_order_events_external",
            "tenant_id",
            "source",
            "external_event_id",
            unique=True,
            postgresql_where=text("external_event_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[UUID] = mapped_column()
    type: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    external_event_id: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderOperationRow(Base):
    """The ERP write of one order, recorded before the call (intent before call)."""

    __tablename__ = "order_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["orders.tenant_id", "orders.id"],
            name="fk_order_operations_order",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["connections.tenant_id", "connections.id"],
            name="fk_order_operations_connection",
        ),
        UniqueConstraint("tenant_id", "external_reference", name="uq_order_operations_reference"),
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "external_reference",
            name="uq_order_operations_connection_reference",
        ),
        Index("ix_order_operations_status", "tenant_id", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    order_id: Mapped[UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(32))
    external_reference: Mapped[str] = mapped_column(String(64))
    connection_id: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(16))
    erp_order_id: Mapped[str | None] = mapped_column(String(140))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(500))
    unknown_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
