"""Quotations tables, split from platform/persistence/models.py (ADR 0026).

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import Base

QUOTATION_STATUS_CHECK = (
    "status IN ('pricing_failed', 'pending_approval', 'revision_requested', "
    "'revision_submitted', 'approved', 'rejected', 'accepted', 'declined', 'expired', "
    "'withdrawn')"
)


class Quotation(Base):
    """One quotation per enquiry; its versions are immutable snapshots."""

    __tablename__ = "quotations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "enquiry_id"],
            ["enquiries.tenant_id", "enquiries.id"],
            name="fk_quotations_enquiry",
        ),
        UniqueConstraint("tenant_id", "enquiry_id", name="uq_quotations_tenant_enquiry"),
        UniqueConstraint("tenant_id", "id", name="uq_quotations_tenant_id_id"),
        CheckConstraint(QUOTATION_STATUS_CHECK, name="ck_quotations_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    enquiry_id: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(32))
    current_version: Mapped[int | None] = mapped_column(Integer)
    pricing_issues: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuotationVersion(Base):
    """Immutable: no role may UPDATE or DELETE (grants and a trigger enforce it)."""

    __tablename__ = "quotation_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "quotation_id"],
            ["quotations.tenant_id", "quotations.id"],
            name="fk_quotation_versions_quotation",
        ),
        UniqueConstraint(
            "tenant_id", "quotation_id", "version", name="uq_quotation_versions_number"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_quotation_versions_tenant_id_id"),
        UniqueConstraint("tenant_id", "command_id", name="uq_quotation_versions_command"),
        CheckConstraint("version >= 1", name="ck_quotation_versions_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    quotation_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, object]] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    total: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    created_by: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(String(1000))
    command_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalTask(Base):
    __tablename__ = "approval_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["quotation_versions.tenant_id", "quotation_versions.id"],
            name="fk_approval_tasks_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quotation_id"],
            ["quotations.tenant_id", "quotations.id"],
            name="fk_approval_tasks_quotation",
        ),
        UniqueConstraint("tenant_id", "version_id", name="uq_approval_tasks_version"),
        UniqueConstraint("tenant_id", "id", name="uq_approval_tasks_tenant_id_id"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'revision_requested')",
            name="ck_approval_tasks_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    quotation_id: Mapped[UUID] = mapped_column()
    version_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalDecision(Base):
    """Audit record of one decision. Immutable; at most one per task."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["approval_tasks.tenant_id", "approval_tasks.id"],
            name="fk_approval_decisions_task",
        ),
        UniqueConstraint("tenant_id", "task_id", name="uq_approval_decisions_task"),
        UniqueConstraint("tenant_id", "command_id", name="uq_approval_decisions_command"),
        CheckConstraint(
            "action IN ('approve', 'reject', 'request_revision')",
            name="ck_approval_decisions_action",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[UUID] = mapped_column()
    command_id: Mapped[UUID] = mapped_column()
    quotation_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    actor_subject: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(String(1000))
    transport: Mapped[str] = mapped_column(String(32))
    evidence_ref: Mapped[str | None] = mapped_column(String(300))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelEnrollment(Base):
    """A one-time code proving that the signed-in manager controls a channel account."""

    __tablename__ = "channel_enrollments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code_sha256", name="uq_channel_enrollments_code"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_channel_enrollments_connection",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    subject: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(32))
    # NULL: any connection of that provider (old endpoint); set: only that connection.
    connection_ref: Mapped[UUID | None] = mapped_column()
    code_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChannelBindingRow(Base):
    """An internal manager's enrolled account on one approval channel connection."""

    __tablename__ = "channel_bindings"
    __table_args__ = (
        Index(
            "uq_channel_bindings_active_account",
            "tenant_id",
            "connection_ref",
            "external_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        UniqueConstraint("tenant_id", "id", name="uq_channel_bindings_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_channel_bindings_connection",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    subject: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(32))
    connection_id: Mapped[str] = mapped_column(String(100))  # legacy key, for display
    connection_ref: Mapped[UUID] = mapped_column()
    external_user_id: Mapped[str] = mapped_column(String(200))
    enrollment_id: Mapped[UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalActionRef(Base):
    """Opaque single-use action reference sent to one bound account for one task."""

    __tablename__ = "approval_action_refs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["approval_tasks.tenant_id", "approval_tasks.id"],
            name="fk_approval_action_refs_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "binding_id"],
            ["channel_bindings.tenant_id", "channel_bindings.id"],
            name="fk_approval_action_refs_binding",
        ),
        UniqueConstraint("tenant_id", "task_id", "binding_id", name="uq_approval_action_refs_task"),
        UniqueConstraint("token_sha256", name="uq_approval_action_refs_token"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[UUID] = mapped_column()
    binding_id: Mapped[UUID] = mapped_column()
    token_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


DELIVERY_STATUS_CHECK = "status IN ('pending', 'sending', 'available', 'sent', 'failed', 'unknown')"


class QuotationDocument(Base):
    """Rendered PDF of one approved version. Immutable (grants and a trigger enforce it)."""

    __tablename__ = "quotation_documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "quotation_id", "version"],
            [
                "quotation_versions.tenant_id",
                "quotation_versions.quotation_id",
                "quotation_versions.version",
            ],
            name="fk_quotation_documents_version",
        ),
        UniqueConstraint(
            "tenant_id", "quotation_id", "version", "kind", name="uq_quotation_documents_version"
        ),
        UniqueConstraint("tenant_id", "id", name="uq_quotation_documents_tenant_id_id"),
        CheckConstraint("kind IN ('approved')", name="ck_quotation_documents_kind"),
        CheckConstraint("size_bytes > 0", name="ck_quotation_documents_size"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    quotation_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    content_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    renderer: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QuotationDelivery(Base):
    """Outbox row: one approved version to one recipient on one channel, with its outcome."""

    __tablename__ = "quotation_deliveries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "quotation_id", "version"],
            [
                "quotation_versions.tenant_id",
                "quotation_versions.quotation_id",
                "quotation_versions.version",
            ],
            name="fk_quotation_deliveries_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["quotation_documents.tenant_id", "quotation_documents.id"],
            name="fk_quotation_deliveries_document",
        ),
        UniqueConstraint(
            "tenant_id",
            "quotation_id",
            "version",
            "channel",
            "recipient",
            name="uq_quotation_deliveries_target",
        ),
        CheckConstraint(DELIVERY_STATUS_CHECK, name="ck_quotation_deliveries_status"),
        ForeignKeyConstraint(
            ["tenant_id", "outbound_message_id"],
            ["outbound_messages.tenant_id", "outbound_messages.id"],
            name="fk_quotation_deliveries_message",
        ),
        UniqueConstraint("outbound_message_id", name="uq_quotation_deliveries_message"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    quotation_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))
    document_id: Mapped[UUID | None] = mapped_column()
    provider_reference: Mapped[str | None] = mapped_column(String(200))
    last_error: Mapped[str | None] = mapped_column(String(300))
    outbound_message_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


RESPONSE_OUTCOME_CHECK = (
    "outcome IN ('accepted', 'declined', 'refused_changed', 'withdrawn', 'expired')"
)


class QuotationResponse(Base):
    """An answer to an issued version: customer accept/decline, staff withdrawal or expiry.

    Append-only (grants and a trigger). At most one closing outcome per quotation.
    """

    __tablename__ = "quotation_responses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "quotation_id", "version"],
            [
                "quotation_versions.tenant_id",
                "quotation_versions.quotation_id",
                "quotation_versions.version",
            ],
            name="fk_quotation_responses_version",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_quotation_responses_key"),
        CheckConstraint(RESPONSE_OUTCOME_CHECK, name="ck_quotation_responses_outcome"),
        Index(
            "uq_quotation_responses_closing",
            "tenant_id",
            "quotation_id",
            unique=True,
            postgresql_where=text("outcome <> 'refused_changed'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    quotation_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(String(1000))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64))
    differences: Mapped[list[str]] = mapped_column(JSONB)
    revised_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CustomerActionRef(Base):
    """Accept/Decline reference for one offered version and one identity (answered once).

    Buttons send the ID (the sender is checked); a secure link carries a token whose SHA-256 is
    stored here. The token itself is never stored.
    """

    __tablename__ = "customer_action_refs"
    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_customer_action_refs_token"),
        UniqueConstraint(
            "tenant_id",
            "quotation_id",
            "version",
            "identity_id",
            name="uq_customer_action_refs_offer",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quotation_id", "version"],
            [
                "quotation_versions.tenant_id",
                "quotation_versions.quotation_id",
                "quotation_versions.version",
            ],
            name="fk_customer_action_refs_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["contact_identities.tenant_id", "contact_identities.id"],
            name="fk_customer_action_refs_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_customer_action_refs_conversation",
        ),
        ForeignKeyConstraint(
            ["response_id"], ["quotation_responses.id"], name="fk_customer_action_refs_response"
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (response_id IS NULL)",
            name="ck_customer_action_refs_consumed",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    quotation_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(Integer)
    content_sha256: Mapped[str] = mapped_column(String(64))
    identity_id: Mapped[UUID] = mapped_column()
    conversation_id: Mapped[UUID] = mapped_column()
    outbound_message_id: Mapped[UUID | None] = mapped_column()
    token_sha256: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalReplyPrompt(Base):
    """A question to a linked manager about one approval request (phase 5, D10).

    kind reason: "reply with the reason" after a Reject/Change button; the reply supplies the
    reason for the stored action. kind confirm: "You want to <action> Q-... version N?" after
    the AI read a written reply (P5-13); Yes records the stored action and reason. At most one
    open prompt per request.
    """

    __tablename__ = "approval_reply_prompts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["reference_id"], ["approval_action_refs.id"], name="fk_approval_reply_prompts_ref"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "outbound_message_id"],
            ["outbound_messages.tenant_id", "outbound_messages.id"],
            name="fk_approval_reply_prompts_message",
        ),
        UniqueConstraint("token_sha256", name="uq_approval_reply_prompts_token"),
        UniqueConstraint("outbound_message_id", name="uq_approval_reply_prompts_message"),
        ForeignKeyConstraint(
            ["tenant_id", "inbox_event_id"],
            ["inbox_events.tenant_id", "inbox_events.id"],
            name="fk_approval_reply_prompts_event",
        ),
        UniqueConstraint("inbox_event_id", name="uq_approval_reply_prompts_event"),
        CheckConstraint("kind IN ('reason', 'confirm')", name="ck_approval_reply_prompts_kind"),
        CheckConstraint(
            "action IN ('approve', 'reject', 'request_revision')",
            name="ck_approval_reply_prompts_action",
        ),
        CheckConstraint(
            "(closed_at IS NULL) = (outcome IS NULL)", name="ck_approval_reply_prompts_closed"
        ),
        CheckConstraint(
            "reason IS NULL OR length(reason) <= 1000", name="ck_approval_reply_prompts_reason"
        ),
        Index(
            "uq_approval_reply_prompts_open",
            "tenant_id",
            "reference_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    reference_id: Mapped[UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    token_sha256: Mapped[str | None] = mapped_column(String(64))
    outbound_message_id: Mapped[UUID | None] = mapped_column()
    inbox_event_id: Mapped[UUID | None] = mapped_column()  # the reply that led to it
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManagerReplyClassification(Base):
    """Append-only audit of one written manager reply and what was understood (phase 5)."""

    __tablename__ = "manager_reply_classifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "inbox_event_id"],
            ["inbox_events.tenant_id", "inbox_events.id"],
            name="fk_manager_reply_classifications_event",
        ),
        ForeignKeyConstraint(
            ["reference_id"],
            ["approval_action_refs.id"],
            name="fk_manager_reply_classifications_ref",
        ),
        UniqueConstraint("inbox_event_id", name="uq_manager_reply_classifications_event"),
        CheckConstraint(
            "intent IN ('approve', 'reject', 'revise', 'unclear')",
            name="ck_manager_reply_classifications_intent",
        ),
        CheckConstraint("length(text) <= 4000", name="ck_manager_reply_classifications_text"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    inbox_event_id: Mapped[UUID] = mapped_column()
    reference_id: Mapped[UUID | None] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    intent: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(200))
    guard: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MediaLink(Base):
    """A short-lived public link to one approved PDF (phase 5, C6); token SHA-256 only."""

    __tablename__ = "media_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["quotation_documents.tenant_id", "quotation_documents.id"],
            name="fk_media_links_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "outbound_message_id"],
            ["outbound_messages.tenant_id", "outbound_messages.id"],
            name="fk_media_links_message",
        ),
        UniqueConstraint("token_sha256", name="uq_media_links_token"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    token_sha256: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[UUID] = mapped_column()
    content_sha256: Mapped[str] = mapped_column(String(64))
    outbound_message_id: Mapped[UUID | None] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
