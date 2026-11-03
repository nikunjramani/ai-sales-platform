"""Conversations tables, split from platform/persistence/models.py (ADR 0026).

They register on the one Base.metadata that Alembic reads (migrations/env.py imports them).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import Base


class Contact(Base):
    """One customer of one business. Email/phone are claimed until a *_verified_at is set."""

    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_contacts_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "merged_into_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_contacts_merged",
        ),
        CheckConstraint(
            "merged_into_id IS NULL OR merged_into_id <> id", name="ck_contacts_not_self"
        ),
        CheckConstraint(
            "(merged_into_id IS NULL) = (merged_at IS NULL)", name="ck_contacts_merged"
        ),
        Index("ix_contacts_tenant_email", "tenant_id", func.lower(text("email"))),
        Index("ix_contacts_tenant_phone", "tenant_id", "phone"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    display_name: Mapped[str | None] = mapped_column(String(200))
    company: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone: Mapped[str | None] = mapped_column(String(32))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_into_id: Mapped[UUID | None] = mapped_column()
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContactIdentity(Base):
    """One address of a contact on one connection, with its consent state (D15)."""

    __tablename__ = "contact_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "connection_ref", "external_id", name="uq_contact_identities_external"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_contact_identities_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_contact_identities_contact",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_contact_identities_connection",
        ),
        CheckConstraint(
            "consent_status IN ('implied', 'opted_in', 'opted_out')",
            name="ck_contact_identities_consent",
        ),
        Index("ix_contact_identities_contact", "tenant_id", "contact_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[UUID] = mapped_column()
    connection_ref: Mapped[UUID] = mapped_column()
    external_id: Mapped[str] = mapped_column(String(320))
    display_hint: Mapped[str | None] = mapped_column(String(200))
    verified: Mapped[bool] = mapped_column(Boolean)
    consent_status: Mapped[str] = mapped_column(String(16), server_default=text("'implied'"))
    consent_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_source: Mapped[str | None] = mapped_column(String(100))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Conversation(Base):
    """One per identity; its row lock serialises one customer's messages on one tool."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "identity_id", name="uq_conversations_identity"),
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_conversations_contact",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["contact_identities.tenant_id", "contact_identities.id"],
            name="fk_conversations_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_conversations_connection",
        ),
        Index("ix_conversations_inbox", "tenant_id", text("last_message_at DESC")),
        Index("ix_conversations_contact", "tenant_id", "contact_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    contact_id: Mapped[UUID] = mapped_column()
    identity_id: Mapped[UUID] = mapped_column()
    connection_ref: Mapped[UUID] = mapped_column()
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationMessage(Base):
    """Append-only thread entry: an inbound inbox event or an outbound outbox message."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        UniqueConstraint("inbox_event_id", name="uq_conversation_messages_inbox"),
        UniqueConstraint("outbound_message_id", name="uq_conversation_messages_outbound"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_conversation_messages_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "enquiry_id"],
            ["enquiries.tenant_id", "enquiries.id"],
            name="fk_conversation_messages_enquiry",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "inbox_event_id"],
            ["inbox_events.tenant_id", "inbox_events.id"],
            name="fk_conversation_messages_inbox_event",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "outbound_message_id"],
            ["outbound_messages.tenant_id", "outbound_messages.id"],
            name="fk_conversation_messages_outbound",
        ),
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="ck_conversation_messages_direction"
        ),
        CheckConstraint(
            "author IN ('customer', 'system', 'staff')", name="ck_conversation_messages_author"
        ),
        CheckConstraint(
            "(direction = 'inbound' AND inbox_event_id IS NOT NULL "
            "AND outbound_message_id IS NULL) OR (direction = 'outbound' "
            "AND outbound_message_id IS NOT NULL AND inbox_event_id IS NULL)",
            name="ck_conversation_messages_source",
        ),
        CheckConstraint(
            "conversation_id IS NOT NULL OR enquiry_id IS NOT NULL",
            name="ck_conversation_messages_owner",
        ),
        CheckConstraint("length(text) <= 20000", name="ck_conversation_messages_text"),
        Index("ix_conversation_messages_conversation", "tenant_id", "conversation_id", "seq"),
        Index("ix_conversation_messages_enquiry", "tenant_id", "enquiry_id", "seq"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    # Declared before `text`, which would otherwise shadow sqlalchemy.text in this class body.
    attachment_ids: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    conversation_id: Mapped[UUID | None] = mapped_column()
    enquiry_id: Mapped[UUID | None] = mapped_column()
    direction: Mapped[str] = mapped_column(String(8))
    author: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    template_key: Mapped[str | None] = mapped_column(String(64))
    inbox_event_id: Mapped[UUID | None] = mapped_column()
    outbound_message_id: Mapped[UUID | None] = mapped_column()
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
