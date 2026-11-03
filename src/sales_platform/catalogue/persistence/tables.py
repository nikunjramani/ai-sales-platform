"""Catalogue tables, split from platform/persistence/models.py (ADR 0026).

They register on the one Base.metadata that Alembic reads (migrations/env.py imports them).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import Base


class CatalogueCategory(Base):
    __tablename__ = "catalogue_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_catalogue_categories_tenant_id"),
        Index("uq_catalogue_categories_name", "tenant_id", func.lower(text("name")), unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(100))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[str] = mapped_column(String(200))


class CatalogueProductRow(Base):
    """A product typed in by staff. search_terms come from domain.catalogue_search."""

    __tablename__ = "catalogue_products"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_catalogue_products_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "category_id"],
            ["catalogue_categories.tenant_id", "catalogue_categories.id"],
            name="fk_catalogue_products_category",
        ),
        Index("uq_catalogue_products_sku", "tenant_id", func.lower(text("sku")), unique=True),
        Index("ix_catalogue_products_category", "tenant_id", "category_id"),
        Index("ix_catalogue_products_terms", "search_terms", postgresql_using="gin"),
        Index(
            "ix_catalogue_products_sku_trgm",
            func.lower(text("sku")),
            postgresql_using="gin",
            postgresql_ops={"lower(sku)": "gin_trgm_ops"},
        ),
        Index(
            "ix_catalogue_products_name_trgm",
            func.lower(text("name")),
            postgresql_using="gin",
            postgresql_ops={"lower(name)": "gin_trgm_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    category_id: Mapped[UUID | None] = mapped_column()
    unit: Mapped[str] = mapped_column(String(32))
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric())
    quantity_step: Mapped[Decimal] = mapped_column(Numeric())
    search_terms: Mapped[list[str]] = mapped_column(ARRAY(Text))
    source: Mapped[str] = mapped_column(String(16), server_default=text("'manual'"))
    external_id: Mapped[str | None] = mapped_column(String(200))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by: Mapped[str | None] = mapped_column(String(200))
    row_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[str] = mapped_column(String(200))


class CataloguePriceRow(Base):
    """Append-only price history; the price in force is chosen at read time."""

    __tablename__ = "catalogue_prices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["catalogue_products.tenant_id", "catalogue_products.id"],
            name="fk_catalogue_prices_product",
        ),
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "price_list",
            "currency",
            "revision",
            name="uq_catalogue_prices_revision",
        ),
        UniqueConstraint("tenant_id", "command_id", name="uq_catalogue_prices_command"),
        Index(
            "ix_catalogue_prices_lookup",
            "tenant_id",
            "product_id",
            "price_list",
            "currency",
            text("effective_from DESC"),
            text("revision DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    product_id: Mapped[UUID] = mapped_column()
    price_list: Mapped[str] = mapped_column(String(64), server_default=text("'standard'"))
    revision: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    unit: Mapped[str] = mapped_column(String(32))
    unit_price: Mapped[Decimal] = mapped_column(Numeric())
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), server_default=text("'manual'"))
    note: Mapped[str | None] = mapped_column(String(500))
    command_id: Mapped[UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(200))


class CatalogueStockRow(Base):
    """Append-only stock counts; quantity NULL means "not tracked" (unknown)."""

    __tablename__ = "catalogue_stock"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["catalogue_products.tenant_id", "catalogue_products.id"],
            name="fk_catalogue_stock_product",
        ),
        UniqueConstraint("tenant_id", "command_id", name="uq_catalogue_stock_command"),
        Index(
            "ix_catalogue_stock_current",
            "tenant_id",
            "product_id",
            "location_code",
            text("as_of DESC"),
            text("recorded_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    product_id: Mapped[UUID] = mapped_column()
    location_code: Mapped[str] = mapped_column(String(32), server_default=text("'MAIN'"))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric())
    unit: Mapped[str] = mapped_column(String(32))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), server_default=text("'manual'"))
    note: Mapped[str | None] = mapped_column(String(500))
    command_id: Mapped[UUID] = mapped_column()
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    recorded_by: Mapped[str] = mapped_column(String(200))


class CatalogueChange(Base):
    """Append-only audit of product and category edits: {"field": [old, new]}."""

    __tablename__ = "catalogue_changes"
    __table_args__ = (Index("ix_catalogue_changes_entity", "tenant_id", "entity_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    entity: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[UUID] = mapped_column()
    action: Mapped[str] = mapped_column(String(16))
    changes: Mapped[dict[str, object]] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CatalogueSyncRun(Base):
    """One copy of a product source's catalogue into ours (phase 6, migration 0014)."""

    __tablename__ = "catalogue_sync_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["connections.tenant_id", "connections.id"],
            name="fk_catalogue_sync_runs_connection",
        ),
        CheckConstraint(
            "trigger IN ('schedule', 'manual', 'connected')", name="ck_catalogue_sync_runs_trigger"
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_catalogue_sync_runs_status"
        ),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="ck_catalogue_sync_runs_finished"
        ),
        Index(
            "ix_catalogue_sync_runs_latest", "tenant_id", "connection_id", text("started_at DESC")
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    connection_id: Mapped[UUID] = mapped_column()
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    issues: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(String(500))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
