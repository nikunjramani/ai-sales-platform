"""Contracts for people, businesses, members, invitations, API keys and account emails (phase 2).

Rules that hold for every implementation:
- Passwords and email tokens are never stored or returned in clear: passwords as Argon2id
  hashes, tokens as SHA-256 (like API keys). Tokens are created by the worker at send time.
- "Not found", "not allowed", "used" and "expired" look the same to callers wherever telling
  them apart would reveal who has an account or what another business contains.
- Membership changes go through the database's own narrow functions, never plain inserts.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from sales_platform.accounts.domain.accounts import (
    AccountEmailPurpose,
    Invitation,
    SetupItem,
    SetupItemKey,
    User,
)
from sales_platform.platform.domain.tenancy import Role, TenantContext, TenantStatus


class NotAllowed(Exception):
    """The person may not do this (unverified email, wrong person, suspended business...)."""


class InvitationNotUsable(Exception):
    """Unknown, expired, revoked or already used, or for another email. One message for all."""


class AlreadyMember(Exception):
    """The invited email already belongs to a member of this business."""


class VersionConflict(Exception):
    """Someone else saved this form in the meantime; reload and try again."""


# --- Passwords --------------------------------------------------------------------------------
class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool:
        """Constant-time; False on mismatch or a malformed hash, never raises for those."""
        ...

    def needs_rehash(self, password_hash: str) -> bool: ...

    @property
    def dummy_hash(self) -> str:
        """A real hash of a random password, verified when the email is unknown so the answer
        takes as long as for a known email."""
        ...


# --- People -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class LoginRecord:
    user: User
    password_hash: str | None  # None: no password (cannot sign in with one)


class UserStore(Protocol):
    def sign_up(
        self,
        user_id: UUID,
        email: str,
        email_normalized: str,
        display_name: str | None,
        password_hash: str,
        *,
        notify_existing: Callable[[], bool],
    ) -> bool:
        """One transaction: user + credential + verification row + email job. Returns False,
        and changes nothing, if the email already has an account; then, if notify_existing()
        allows it (asked only in that case), an "account exists" notice is queued instead."""
        ...

    def find_for_login(self, email_normalized: str) -> LoginRecord | None: ...

    def password_hash(self, subject: str) -> str | None: ...

    def store_rehash(self, user_id: UUID, password_hash: str) -> None:
        """After a successful login with outdated hash parameters."""
        ...

    def by_subject(self, subject: str) -> User | None: ...

    def update_profile(self, subject: str, display_name: str | None) -> User: ...

    def verify_email(self, token_sha256: str, now: datetime) -> bool:
        """Consume the token and mark the email verified, atomically. False for any token that
        is unknown, used, revoked or expired."""
        ...

    def request_verification(self, subject: str) -> bool:
        """Revoke open verification rows and queue a new email. False if already verified."""
        ...

    def request_password_reset(self, email_normalized: str) -> None:
        """Queue a reset email when the account exists and has a password; otherwise nothing."""
        ...

    def reset_email(self, token_sha256: str, now: datetime) -> str | None:
        """The address of a usable reset link (to check the new password against), else None."""
        ...

    def reset_password(self, token_sha256: str, password_hash: str, now: datetime) -> str | None:
        """Consume the token, set the hash and queue a "password changed" notice. Returns the
        subject (to sign out all its sessions) or None for an unusable token."""
        ...

    def change_password(self, subject: str, password_hash: str) -> None:
        """Set the hash and queue a "password changed" notice."""
        ...


# --- Businesses -------------------------------------------------------------------------------
@dataclass(frozen=True)
class PricingSettings:
    """Commercial rules of a business. All six "commercial" fields are set together or not at
    all ("set later", Q14); quote_validity_days always has a value."""

    currency: str | None
    minor_units: int | None
    rounding_mode: str | None
    tax_rate: Decimal | None
    max_price_age_seconds: int | None
    max_stock_age_seconds: int | None
    quote_validity_days: int

    @property
    def complete(self) -> bool:
        return None not in (
            self.currency,
            self.minor_units,
            self.rounding_mode,
            self.tax_rate,
            self.max_price_age_seconds,
            self.max_stock_age_seconds,
        )


@dataclass(frozen=True)
class NewBusiness:
    name: str
    country: str
    time_zone: str
    quote_validity_days: int
    currency: str | None  # None: set later
    tax_rate: Decimal | None  # None: set later


@dataclass(frozen=True)
class DefaultSource:
    """The product source a new business starts with (phase 3; from settings, never a name
    written in code): its catalogue connection is created together with the business."""

    provider_key: str
    display_name: str


@dataclass(frozen=True)
class BusinessProfile:
    """The PDF details (Q16)."""

    legal_name: str | None
    address: str | None  # up to 4 lines
    tax_id_label: str | None
    tax_id: str | None
    has_logo: bool
    version: int


@dataclass(frozen=True)
class BusinessSettings:
    tenant_id: UUID
    name: str
    country: str | None
    time_zone: str | None
    status: TenantStatus
    went_live_at: datetime | None
    profile: BusinessProfile


@dataclass(frozen=True)
class ProfileUpdate:
    legal_name: str | None
    address: str | None
    tax_id_label: str | None
    tax_id: str | None


@dataclass(frozen=True)
class StoredPricing:
    settings: PricingSettings
    version: int


class BusinessStore(Protocol):
    def create(self, user_id: UUID, subject: str, business: NewBusiness) -> UUID:
        """Through the database's create_business function. NotAllowed when the person is not
        verified or not the caller."""
        ...

    def status(self, tenant_id: UUID) -> TenantStatus: ...

    def settings(self, context: TenantContext) -> BusinessSettings: ...

    def update_details(
        self, context: TenantContext, name: str, country: str, time_zone: str
    ) -> None: ...

    def update_profile(
        self, context: TenantContext, profile: ProfileUpdate, expected_version: int
    ) -> int:
        """Returns the new version; VersionConflict when expected_version is outdated."""
        ...

    def logo_key(self, context: TenantContext) -> tuple[str, str] | None:
        """(storage key, sha256) of the current logo, if any."""
        ...

    def set_logo(
        self, context: TenantContext, storage_key: str | None, sha256: str | None
    ) -> None: ...

    def pricing(self, context: TenantContext) -> StoredPricing: ...

    def update_pricing(
        self, context: TenantContext, pricing: PricingSettings, expected_version: int
    ) -> int: ...

    def setup_state(self, context: TenantContext) -> list[SetupItem]:
        """Computed from real rows every time, never a stored "done" flag."""
        ...

    def go_live(self, context: TenantContext) -> list[SetupItemKey]:
        """Under a lock: recompute; [] means the business is now (or already was) active."""
        ...


# --- Members and invitations ------------------------------------------------------------------
@dataclass(frozen=True)
class Member:
    user_id: UUID
    subject: str
    email: str | None
    display_name: str | None
    roles: frozenset[Role]


@dataclass(frozen=True)
class InvitationPreview:
    business_name: str
    roles: frozenset[Role]
    email: str  # normalised; callers show only a hint of it
    expires_at: datetime


class MemberStore(Protocol):
    def list(self, context: TenantContext) -> list[Member]: ...

    def change_roles(self, context: TenantContext, user_id: UUID, roles: frozenset[Role]) -> None:
        """Through change_member_roles(); LastAdministrator if none would remain."""
        ...

    def remove(self, context: TenantContext, user_id: UUID) -> None:
        """Through remove_member(); also revokes their approval-channel links here."""
        ...


class InvitationStore(Protocol):
    def invite(
        self, context: TenantContext, email: str, email_normalized: str, roles: frozenset[Role]
    ) -> UUID:
        """Revokes an older open invitation for the same email, then stores this one, its email
        job and the audit row. AlreadyMember if that email is a member."""
        ...

    def list(self, context: TenantContext) -> list[Invitation]: ...

    def resend(self, context: TenantContext, invitation_id: UUID) -> bool: ...

    def revoke(self, context: TenantContext, invitation_id: UUID) -> bool: ...

    def preview(self, token_sha256: str, now: datetime) -> InvitationPreview | None: ...

    def accept(self, token_sha256: str, user_id: UUID, subject: str) -> UUID:
        """Through accept_invitation(); returns the business. InvitationNotUsable otherwise."""
        ...


# --- API keys from the portal -----------------------------------------------------------------
@dataclass(frozen=True)
class ApiKeySummary:
    key_id: UUID
    name: str
    role: Role
    created_at: datetime
    created_by: str | None
    revoked_at: datetime | None


class ApiKeyAdminStore(Protocol):
    def create(
        self, context: TenantContext, key_id: UUID, secret_sha256: str, name: str, role: Role
    ) -> None:
        """Stores the key, its api connection and the audit row in one transaction."""
        ...

    def list(self, context: TenantContext) -> list[ApiKeySummary]: ...

    def revoke(self, context: TenantContext, key_id: UUID) -> bool: ...


# --- Account emails (worker side) -------------------------------------------------------------
@dataclass(frozen=True)
class AccountEmailWork:
    """What one send_account_email job needs. Only references are in the job payload."""

    to: str
    business_name: str | None  # invitations only
    roles: frozenset[Role]  # invitations only


class AccountEmailStore(Protocol):
    def prepare(
        self,
        purpose: AccountEmailPurpose,
        ref_id: UUID,
        tenant_id: UUID | None,
        token_sha256: str | None,
        expires_at: datetime | None,
    ) -> AccountEmailWork | None:
        """Compare-and-set pending -> sending, storing the token hash and expiry for purposes
        with a link. None when the row is consumed, revoked or already handed over."""
        ...

    def finish(
        self,
        purpose: AccountEmailPurpose,
        ref_id: UUID,
        tenant_id: UUID | None,
        status: Literal["sent", "failed", "unknown"],
    ) -> None: ...

    def mark_unknown_if_sending(
        self, purpose: AccountEmailPurpose, ref_id: UUID, tenant_id: UUID | None
    ) -> bool:
        """A previous attempt crashed after handing the email over: record unknown, never send
        a second copy (same rule as deliveries)."""
        ...


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    sender: str
    subject: str
    body: str
    idempotency_key: str


class MailRejected(Exception):
    """The mail service refused the email; retrying will not help."""


class MailUncertain(Exception):
    """The mail service may or may not have it (e.g. a timeout). Never retried."""


class Mailer(Protocol):
    def send(self, message: OutgoingEmail) -> None: ...


@dataclass(frozen=True)
class MailerEnvironment:
    """What a mailer's factory (entry point group sales_platform.mailers) is given.

    Secrets (a real sender's API key) are read through the SecretStore, never from options.
    """

    environment: str
    storage_dir: Path
    options: Mapping[str, object]


# --- Logos ------------------------------------------------------------------------------------
class InvalidLogo(ValueError):
    """Not a PNG/JPEG we accept (type, size, pixels or a broken file)."""


class LogoNormalizer(Protocol):
    def normalize(self, data: bytes) -> bytes:
        """A fresh, small PNG without metadata made from the upload, or InvalidLogo."""
        ...
