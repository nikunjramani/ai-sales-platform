"""Enquiries tables, split from platform/persistence/models.py (ADR 0026).

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import Base

ENQUIRY_STATUS_CHECK = (
    "status IN ('received', 'ready_for_extraction', 'needs_review', "
    "'ready_for_quotation', 'needs_clarification')"
)


class Enquiry(Base):
    __tablename__ = "enquiries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "inbox_event_id"],
            ["inbox_events.tenant_id", "inbox_events.id"],
            name="fk_enquiries_inbox_event",
        ),
        UniqueConstraint("inbox_event_id", name="uq_enquiries_inbox_event_id"),
        UniqueConstraint("tenant_id", "id", name="uq_enquiries_tenant_id_id"),
        CheckConstraint(
            ENQUIRY_STATUS_CHECK,
            name="ck_enquiries_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_enquiries_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_enquiries_contact",
        ),
        Index("ix_enquiries_conversation_status", "tenant_id", "conversation_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    inbox_event_id: Mapped[UUID] = mapped_column()
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    customer_reference: Mapped[str | None] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)  # the first message; all are in conversation_messages
    correlation_id: Mapped[UUID] = mapped_column()
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Phase 4: NULL for API enquiries without a customer_reference (Q5).
    conversation_id: Mapped[UUID | None] = mapped_column()
    contact_id: Mapped[UUID | None] = mapped_column()


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "enquiry_id"],
            ["enquiries.tenant_id", "enquiries.id"],
            name="fk_attachments_enquiry",
        ),
        Index("ix_attachments_tenant_enquiry", "tenant_id", "enquiry_id"),
        CheckConstraint(
            "status IN ('quarantined', 'clean', 'rejected')", name="ck_attachments_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    enquiry_id: Mapped[UUID] = mapped_column()
    filename: Mapped[str] = mapped_column(String(255))
    declared_content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    detected_content_type: Mapped[str | None] = mapped_column(String(100))
    rejection_reason: Mapped[str | None] = mapped_column(String(50))
    storage_key: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


WORKFLOW_RUN_STATUS_CHECK = (
    "status IN ('running', 'ready_for_quotation', 'needs_clarification', 'needs_review', "
    "'approved', 'rejected', 'accepted', 'declined', 'expired', 'withdrawn')"
)


class WorkflowRun(Base):
    """Maps one tenant's enquiry to a LangGraph thread (run ID = thread ID) and its outcome.

    Checkpoint tables have no tenant column, so they are only ever opened through this mapping.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "enquiry_id"],
            ["enquiries.tenant_id", "enquiries.id"],
            name="fk_workflow_runs_enquiry",
        ),
        UniqueConstraint(
            "tenant_id",
            "enquiry_id",
            "graph_version",
            "round",
            name="uq_workflow_runs_enquiry_round",
        ),
        CheckConstraint(WORKFLOW_RUN_STATUS_CHECK, name="ck_workflow_runs_status"),
        CheckConstraint("round >= 1", name="ck_workflow_runs_round"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    enquiry_id: Mapped[UUID] = mapped_column()
    graph_version: Mapped[str] = mapped_column(String(50))
    # Phase 4: each reading of an enquiry (first message, then each follow-up) is one round.
    round: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
