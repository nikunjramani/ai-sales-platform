"""Integrations tables, split from platform/persistence/models.py (ADR 0026).

They register on the one Base.metadata that Alembic reads (migrations/env.py imports them).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import Base


class ConnectionRow(Base):
    """One business's configured tool (ADR 0018). Secrets are references only."""

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_connections_tenant_id_id"),
        UniqueConstraint("tenant_id", "provider_key", "account_ref", name="uq_connections_account"),
        Index("ix_connections_tenant_status", "tenant_id", "status"),
        # At most one active product source per business (P1-Q7).
        Index(
            "uq_connections_one_catalogue",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active' AND purposes @> ARRAY['catalogue']::text[]"),
        ),
        UniqueConstraint("public_handle", name="uq_connections_public_handle"),
        CheckConstraint(
            "public_handle IS NULL OR public_handle ~ '^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$'",
            name="ck_connections_public_handle",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    provider_key: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(100))
    purposes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    config: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'"))
    secret_refs: Mapped[dict[str, str]] = mapped_column(JSONB, server_default=text("'{}'"))
    account_ref: Mapped[str | None] = mapped_column(String(200))
    webhook_token_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Phase 4 (Q13): the public address of a customer tool, e.g. /chat/<handle>.
    public_handle: Mapped[str | None] = mapped_column(String(64))
    public_handle_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 5: the step the business still has to do (one-time values are never stored).
    pending_action: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class ConnectionRoute(Base):
    """A routing key naming one connection, for platform-wide webhooks (phase 5).

    Unique across all businesses; the connection_route_lookup policy reveals only the row whose
    exact (provider_key, routing_key) was presented.
    """

    __tablename__ = "connection_routes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["connections.tenant_id", "connections.id"],
            name="fk_connection_routes_connection",
        ),
        UniqueConstraint("provider_key", "routing_key", name="uq_connection_routes_key"),
        Index("ix_connection_routes_connection", "tenant_id", "connection_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    connection_id: Mapped[UUID] = mapped_column()
    provider_key: Mapped[str] = mapped_column(String(32))
    routing_key: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConnectionEventRow(Base):
    """Append-only audit of connection changes; never contains secrets."""

    __tablename__ = "connection_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["connections.tenant_id", "connections.id"],
            name="fk_connection_events_connection",
        ),
        Index("ix_connection_events_connection", "tenant_id", "connection_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column()
    connection_id: Mapped[UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(200))
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InboxEvent(Base):
    """Durable receipt of one provider event; the unique key makes replays detectable."""

    __tablename__ = "inbox_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "connection_ref",
            "provider_event_id",
            "event_type",
            name="uq_inbox_events_provider_event",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_inbox_events_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_inbox_events_connection",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    # Deprecated since 0008 (still filled): the provider key and the legacy connection string.
    channel: Mapped[str] = mapped_column(String(32))
    connection_id: Mapped[str] = mapped_column(String(100))
    connection_ref: Mapped[UUID] = mapped_column()
    event_type: Mapped[str] = mapped_column(String(32), server_default="customer_message")
    provider_event_id: Mapped[str] = mapped_column(String(200))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[dict[str, object]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[UUID] = mapped_column()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(300))


class OutboundMessageRow(Base):
    """One message we send (ADR 0018). Stores fixed wording and IDs, never action tokens."""

    __tablename__ = "outbound_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_outbound_messages_connection",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["quotation_documents.tenant_id", "quotation_documents.id"],
            name="fk_outbound_messages_document",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_outbound_messages_tenant_id_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_outbound_messages_idempotency"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'available', 'sent', 'delivered', 'failed', "
            "'unknown')",
            name="ck_outbound_messages_status",
        ),
        Index(
            "uq_outbound_messages_provider_message",
            "tenant_id",
            "connection_ref",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index("ix_outbound_messages_status", "tenant_id", "status"),
        Index("ix_outbound_messages_quotation", "tenant_id", "quotation_id"),
        Index("ix_outbound_messages_task", "tenant_id", "approval_task_id"),
        Index("ix_outbound_messages_action_ref", "tenant_id", "action_ref_id"),
        ForeignKeyConstraint(
            ["tenant_id", "resent_from_id"],
            ["outbound_messages.tenant_id", "outbound_messages.id"],
            name="fk_outbound_messages_resent_from",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    connection_ref: Mapped[UUID] = mapped_column()
    provider_key: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(32))
    template_key: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, str]] = mapped_column(JSONB)
    body_text: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict[str, str]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'")
    )
    recipient: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(String(300))
    document_id: Mapped[UUID | None] = mapped_column()
    quotation_id: Mapped[UUID | None] = mapped_column()
    version: Mapped[int | None] = mapped_column(Integer)
    approval_task_id: Mapped[UUID | None] = mapped_column()
    binding_id: Mapped[UUID | None] = mapped_column()
    enquiry_id: Mapped[UUID | None] = mapped_column()
    response_id: Mapped[UUID | None] = mapped_column()
    correlation_id: Mapped[UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 5: the approval request this message belongs to (a reply to it finds the request)
    # and the failed message a staff Resend repeats.
    action_ref_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approval_action_refs.id", name="fk_outbound_messages_action_ref")
    )
    resent_from_id: Mapped[UUID | None] = mapped_column()
