"""Table metadata. Tenant-owned tables carry tenant_id and are protected by forced RLS."""

from datetime import datetime
from decimal import Decimal
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
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "status IN ('setting_up', 'active', 'suspended')", name="ck_tenants_status"
        ),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="ck_tenants_country"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Phase 2: setting_up -> active (Go live); suspended only by the operator (owner role).
    status: Mapped[str] = mapped_column(String(16), server_default=text("'setting_up'"))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    time_zone: Mapped[str | None] = mapped_column(String(64))  # IANA name
    created_by: Mapped[str | None] = mapped_column(String(200))
    went_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Phase 4 (D14): send customers "we are preparing your quotation" on a new enquiry.
    acknowledge_enquiries: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Phase 7 (Q1): "staff" press Create order (default) or "automatic" on acceptance.
    order_creation: Mapped[str] = mapped_column(String(16), server_default=text("'staff'"))


class User(Base):
    """A person who can sign in. Global: one person may belong to several businesses.

    subject is what sessions and memberships use ("user:<uuid>" for sign-ups, the email for the
    seeded demo people). Narrow RLS policies show the API only the rows it is entitled to.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index(
            "uq_users_email_normalized",
            "email_normalized",
            unique=True,
            postgresql_where=text("email_normalized IS NOT NULL"),
        ),
        CheckConstraint(
            "email_normalized = lower(email_normalized)", name="ck_users_email_normalized"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(200), unique=True)
    email: Mapped[str | None] = mapped_column(String(254))  # as typed, for display
    email_normalized: Mapped[str | None] = mapped_column(String(254))
    display_name: Mapped[str | None] = mapped_column(String(100))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserCredential(Base):
    """Argon2id password hash. The worker has no access at all."""

    __tablename__ = "user_credentials"
    __table_args__ = (
        CheckConstraint("password_hash LIKE '$argon2id$%'", name="ck_user_credentials_argon2id"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


EMAIL_STATUS_CHECK = "email_status IN ('pending', 'sending', 'sent', 'failed', 'unknown')"


class _EmailLink:
    """Shared columns of email_verifications and password_resets.

    token_sha256 and expires_at are written by the worker when it sends the email; the token
    itself only ever exists in the email.
    """

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    email_normalized: Mapped[str] = mapped_column(String(254))
    token_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    email_status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _one_open(table: str) -> Index:
    """At most one usable link per person at a time."""
    return Index(
        f"uq_{table}_one_open",
        "user_id",
        unique=True,
        postgresql_where=text("consumed_at IS NULL AND revoked_at IS NULL"),
    )


class EmailVerification(_EmailLink, Base):
    __tablename__ = "email_verifications"
    __table_args__ = (
        _one_open("email_verifications"),
        CheckConstraint(EMAIL_STATUS_CHECK, name="ck_email_verifications_email_status"),
    )


class PasswordReset(_EmailLink, Base):
    __tablename__ = "password_resets"
    __table_args__ = (
        _one_open("password_resets"),
        CheckConstraint(EMAIL_STATUS_CHECK, name="ck_password_resets_email_status"),
    )


class AccountNotice(Base):
    """An account email without a link (someone tried to sign up; password changed)."""

    __tablename__ = "account_notices"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('account_exists', 'password_changed')", name="ck_account_notices_purpose"
        ),
        CheckConstraint(EMAIL_STATUS_CHECK, name="ck_account_notices_email_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    email_normalized: Mapped[str] = mapped_column(String(254))
    purpose: Mapped[str] = mapped_column(String(32))
    email_status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TenantProfile(Base):
    """The business as printed on its quotation PDFs (phase 2, Q16)."""

    __tablename__ = "tenant_profiles"
    __table_args__ = (
        CheckConstraint(
            "(logo_storage_key IS NULL) = (logo_sha256 IS NULL)", name="ck_tenant_profiles_logo"
        ),
        CheckConstraint("version >= 1", name="ck_tenant_profiles_version"),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(500))
    tax_id_label: Mapped[str | None] = mapped_column(String(32))
    tax_id: Mapped[str | None] = mapped_column(String(64))
    logo_storage_key: Mapped[str | None] = mapped_column(String(300))
    logo_sha256: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    updated_by: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


PEOPLE_ROLES_ARRAY = "ARRAY['administrator', 'sales_operator', 'approver', 'viewer']::text[]"


class InvitationRow(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        Index(
            "uq_invitations_one_open",
            "tenant_id",
            "email_normalized",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
        CheckConstraint(
            f"cardinality(roles) >= 1 AND roles <@ {PEOPLE_ROLES_ARRAY}",
            name="ck_invitations_roles",
        ),
        CheckConstraint(EMAIL_STATUS_CHECK, name="ck_invitations_email_status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    email: Mapped[str] = mapped_column(String(254))
    email_normalized: Mapped[str] = mapped_column(String(254))
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text()))
    invited_by: Mapped[str] = mapped_column(String(200))
    token_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    email_status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[str | None] = mapped_column(String(200))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminEvent(Base):
    """Append-only audit of business administration (never tokens, hashes or passwords)."""

    __tablename__ = "admin_events"
    __table_args__ = (Index("ix_admin_events_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    actor_subject: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RateLimitCounter(Base):
    """Fixed-window attempt counter shared by all API processes (Q5). Keys are hashed."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = (Index("ix_rate_limit_counters_expires", "expires_at"),)

    rule: Mapped[str] = mapped_column(String(32), primary_key=True)
    key_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Membership(Base):
    """One role held by an authenticated subject within one tenant."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "subject", "role", name="uq_memberships_tenant_subject_role"),
        CheckConstraint(
            "role IN ('administrator', 'sales_operator', 'approver', 'viewer')",
            name="ck_memberships_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    subject: Mapped[str] = mapped_column(
        String(200), ForeignKey("users.subject", name="fk_memberships_subject_users")
    )
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TenantPricingPolicy(Base):
    """Current pricing policy per tenant; version increases whenever values change.

    The commercial values may be missing ("set later", phase 2 Q14); the business can price
    only once all six are set.
    """

    __tablename__ = "tenant_pricing_policies"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_tenant_pricing_policies_version"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_tenant_pricing_policies_currency"),
        CheckConstraint(
            "minor_units BETWEEN 0 AND 4", name="ck_tenant_pricing_policies_minor_units"
        ),
        CheckConstraint(
            "rounding_mode IN ('half_up', 'half_even')",
            name="ck_tenant_pricing_policies_rounding_mode",
        ),
        CheckConstraint(
            "tax_rate >= 0 AND tax_rate < 1", name="ck_tenant_pricing_policies_tax_rate"
        ),
        CheckConstraint(
            "max_price_age_seconds > 0 AND max_stock_age_seconds > 0",
            name="ck_tenant_pricing_policies_ages",
        ),
        CheckConstraint(
            "quote_validity_days >= 1", name="ck_tenant_pricing_policies_quote_validity"
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))
    minor_units: Mapped[int | None] = mapped_column(SmallInteger)
    rounding_mode: Mapped[str | None] = mapped_column(String(16))
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 6))
    max_price_age_seconds: Mapped[int | None] = mapped_column(Integer)
    max_stock_age_seconds: Mapped[int | None] = mapped_column(Integer)
    # Days an issued quotation can be accepted, counted from delivery (M6; default 7).
    quote_validity_days: Mapped[int] = mapped_column(Integer, server_default=text("7"))
    updated_by: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


ENQUIRY_STATUS_CHECK = (
    "status IN ('received', 'ready_for_extraction', 'needs_review', "
    "'ready_for_quotation', 'needs_clarification')"
)
ROLE_CHECK = "role IN ('administrator', 'sales_operator', 'approver', 'viewer', 'intake_service')"


class ApiKey(Base):
    """Service-account credential. Only a SHA-256 hash of the secret is stored."""

    __tablename__ = "api_keys"
    __table_args__ = (CheckConstraint(ROLE_CHECK, name="ck_api_keys_role"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(32))
    secret_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(200))  # phase 2: who, in the portal
    revoked_by: Mapped[str | None] = mapped_column(String(200))


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
        # And one place its orders go (phase 7, migration 0015).
        Index(
            "uq_connections_one_orders",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active' AND purposes @> ARRAY['orders']::text[]"),
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


class Job(Base):
    """Durable work item. Payload holds references only, never documents or secrets."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_at"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'dead')", name="ck_jobs_status"
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_jobs_attempts"),
        CheckConstraint(
            "tenant_id IS NOT NULL OR kind = 'send_account_email'", name="ck_jobs_tenant"
        ),
        # Phase 6: at most one waiting "Sync now" run per business (migration 0014).
        Index(
            "uq_jobs_manual_sync",
            "tenant_id",
            unique=True,
            postgresql_where=text(
                "kind = 'sync_catalogue' AND status = 'pending' AND payload->>'trigger' = 'manual'"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # NULL only for account emails (sign-up, reset), which belong to no business.
    tenant_id: Mapped[UUID | None] = mapped_column(ForeignKey("tenants.id"))
    kind: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    correlation_id: Mapped[UUID] = mapped_column()
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


class UserSession(Base):
    """Portal session for a person. Not tenant-owned: one person may belong to many tenants.

    Only the SHA-256 hash of the token is stored; RLS shows the API role only the row whose
    hash it presents (like API keys).
    """

    __tablename__ = "user_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    subject: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


# --- Our own product catalogue (phase 3, migration 0011, ADR 0020) ---------------------------
# Numbers are unconstrained numeric (with scale checks in the migration) so stored decimals are
# kept exactly: quotations show and compare prices as text.


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


# --- Customers and conversations (phase 4, migration 0012, ADR 0021) -------------------------


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


class CustomerSession(Base):
    """A website chat visitor's session: one connection, identity and conversation."""

    __tablename__ = "customer_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "connection_ref"],
            ["connections.tenant_id", "connections.id"],
            name="fk_customer_sessions_connection",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["contact_identities.tenant_id", "contact_identities.id"],
            name="fk_customer_sessions_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_customer_sessions_conversation",
        ),
        Index("ix_customer_sessions_connection", "tenant_id", "connection_ref"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    connection_ref: Mapped[UUID] = mapped_column()
    identity_id: Mapped[UUID] = mapped_column()
    conversation_id: Mapped[UUID] = mapped_column()
    secret_sha256: Mapped[str] = mapped_column(String(64))
    ip_hmac: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


# --- Orders (phase 7, migration 0015, ADR 0024) -----------------------------------------------


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
