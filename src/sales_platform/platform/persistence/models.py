"""Table metadata. Tenant-owned tables carry tenant_id and are protected by forced RLS."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


ROLE_CHECK = "role IN ('administrator', 'sales_operator', 'approver', 'viewer', 'intake_service')"


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
