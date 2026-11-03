"""Accounts tables, split from platform/persistence/models.py (ADR 0026).

They register on the one Base.metadata that Alembic reads (migrations/env.py imports them).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sales_platform.platform.persistence.models import ROLE_CHECK, Base


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
