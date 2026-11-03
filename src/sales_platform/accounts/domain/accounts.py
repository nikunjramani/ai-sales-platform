"""People, sign-up rules, invitations and the business setup checklist (phase 2).

A *user* is a person who can sign in. Sessions, memberships and approvals name people by a
stable *subject* string: `user:<uuid>` for people who signed up, the email for the seeded demo
people (kept so existing rows still match). Subjects are never parsed for meaning.

Every number here was decided by the user on 2026-10-27 (phase 2 plan, "User answers"). They
are constants, not settings, so they cannot drift between installations.
"""

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sales_platform.platform.domain.rate_limits import RateRule
from sales_platform.platform.domain.tenancy import PEOPLE_ROLES, Role
from sales_platform.platform.domain.validation import require_aware

MAX_EMAIL_LENGTH = 254
MAX_LOCAL_PART_LENGTH = 64  # RFC 5321
MAX_DISPLAY_NAME_LENGTH = 100


class InvalidEmail(ValueError):
    """The text is not an email address we accept."""


def normalize_email(raw: str) -> str:
    """The one spelling used to compare addresses (Q18).

    Trim, Unicode NFKC, lower-case the whole address, exactly one "@", plain ASCII before the
    "@", international domains as punycode, at most 254 characters. Gmail dots and "+tags" are
    not folded: that would be provider-specific guessing.
    """
    if not isinstance(raw, str):
        raise InvalidEmail("An email address is required")
    text = unicodedata.normalize("NFKC", raw.strip()).lower()
    if text.count("@") != 1:
        raise InvalidEmail("An email address needs exactly one @")
    local, domain = text.split("@")
    if not local or not domain:
        raise InvalidEmail("An email address needs a name and a domain")
    if any(c.isspace() or not c.isprintable() for c in text):
        raise InvalidEmail("An email address cannot contain spaces or control characters")
    if not local.isascii() or len(local) > MAX_LOCAL_PART_LENGTH:
        raise InvalidEmail("The part before @ must be plain ASCII, at most 64 characters")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        raise InvalidEmail("The domain is not valid") from None
    result = f"{local}@{domain}"
    if len(result) > MAX_EMAIL_LENGTH:
        raise InvalidEmail("An email address can have at most 254 characters")
    return result


def email_hint(email: str) -> str:
    """'a***@example.com': enough for a person to recognise their address, not to learn it."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def user_subject(user_id: UUID) -> str:
    return f"user:{user_id}"


def clean_display_name(raw: str | None) -> str | None:
    """Single line, at most 100 characters; empty means "no name". Used in email subjects, so
    line breaks are never kept (header injection)."""
    if raw is None:
        return None
    text = " ".join(raw.split())
    if not text:
        return None
    if len(text) > MAX_DISPLAY_NAME_LENGTH or not text.isprintable():
        raise ValueError("A name can have at most 100 printable characters")
    return text


class PasswordProblem(StrEnum):
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    TOO_COMMON = "too_common"
    CONTAINS_EMAIL = "contains_email"


@dataclass(frozen=True)
class PasswordPolicy:
    min_length: int
    max_length: int


# Q1: 12 to 128 characters, no composition rules (NIST SP 800-63B style).
PASSWORD_POLICY = PasswordPolicy(min_length=12, max_length=128)
# The email name is only checked when it is long enough to mean something; otherwise a
# one-letter address would refuse almost every password.
_MIN_EMAIL_NAME_CHECKED = 3


def password_problems(
    password: str, *, email: str, policy: PasswordPolicy, common: frozenset[str]
) -> list[PasswordProblem]:
    """Every reason the password is refused; empty means it is acceptable.

    `common` holds lower-case common passwords (Q25: a bundled offline list).
    """
    problems: list[PasswordProblem] = []
    if len(password) < policy.min_length:
        problems.append(PasswordProblem.TOO_SHORT)
    if len(password) > policy.max_length:
        problems.append(PasswordProblem.TOO_LONG)
    lowered = password.lower()
    if lowered in common:
        problems.append(PasswordProblem.TOO_COMMON)
    name = email.partition("@")[0].lower()
    if len(name) >= _MIN_EMAIL_NAME_CHECKED and name in lowered:
        problems.append(PasswordProblem.CONTAINS_EMAIL)
    return problems


@dataclass(frozen=True)
class User:
    user_id: UUID
    subject: str
    email: str | None
    display_name: str | None
    email_verified: bool
    disabled: bool


class AccountEmailPurpose(StrEnum):
    VERIFY_EMAIL = "verify_email"
    PASSWORD_RESET = "password_reset"
    INVITATION = "invitation"
    # Notices without a link:
    ACCOUNT_EXISTS = "account_exists"  # someone tried to sign up with this address (Q7)
    PASSWORD_CHANGED = "password_changed"


# Q6: how long the link in each email works. Purposes without a link have no entry.
TOKEN_LIFETIME: Mapping[AccountEmailPurpose, timedelta] = MappingProxyType(
    {
        AccountEmailPurpose.VERIFY_EMAIL: timedelta(hours=24),
        AccountEmailPurpose.PASSWORD_RESET: timedelta(minutes=30),
        AccountEmailPurpose.INVITATION: timedelta(days=7),
    }
)


class RateRules:
    """Q4 (as proposed). There is no account lockout (Q3): these limits are the guard."""

    LOGIN_PER_IP = RateRule("login_ip", 30, timedelta(minutes=10))
    LOGIN_PER_EMAIL = RateRule("login_email", 10, timedelta(minutes=10))
    SIGNUP_PER_IP = RateRule("signup_ip", 10, timedelta(hours=1))
    FORGOT_PER_EMAIL = RateRule("forgot_email", 5, timedelta(hours=1))
    FORGOT_PER_IP = RateRule("forgot_ip", 20, timedelta(hours=1))
    RESEND_PER_MINUTE = RateRule("resend_minute", 1, timedelta(minutes=1))
    RESEND_PER_DAY = RateRule("resend_day", 5, timedelta(days=1))
    INVITES_PER_BUSINESS = RateRule("invite_business", 50, timedelta(days=1))
    INVITATION_PER_IP = RateRule("invitation_ip", 30, timedelta(minutes=10))
    CREATE_BUSINESS_PER_USER = RateRule("create_business_user", 5, timedelta(days=1))


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Invitation:
    invitation_id: UUID
    tenant_id: UUID
    email: str
    roles: frozenset[Role]
    invited_by: str
    created_at: datetime
    expires_at: datetime | None  # set when the email (and its link) is created
    accepted_at: datetime | None
    revoked_at: datetime | None
    email_status: str

    def status(self, now: datetime) -> InvitationStatus:
        require_aware(now, "now")
        if self.accepted_at is not None:
            return InvitationStatus.ACCEPTED
        if self.revoked_at is not None:
            return InvitationStatus.REVOKED
        if self.expires_at is not None and self.expires_at <= now:
            return InvitationStatus.EXPIRED
        return InvitationStatus.PENDING


def validate_people_roles(roles: Iterable[Role | str]) -> frozenset[Role]:
    """At least one role, and only roles a person may hold (never intake_service)."""
    try:
        result = frozenset(Role(role) for role in roles)
    except ValueError:
        raise ValueError("Unknown role") from None
    if not result:
        raise ValueError("Choose at least one role")
    if not result <= PEOPLE_ROLES:
        raise ValueError("That role cannot be given to a person")
    return result


class LastAdministrator(Exception):
    """The change would leave the business without any administrator."""


def check_administrator_remains(
    roles_by_subject: Mapping[str, frozenset[Role]], subject: str, new_roles: frozenset[Role]
) -> None:
    """Raise if giving `subject` the `new_roles` (empty = removal) leaves no administrator.

    The database functions enforce the same rule under a lock; this gives the answer earlier.
    """
    after = dict(roles_by_subject)
    after[subject] = new_roles
    if not any(Role.ADMINISTRATOR in roles for roles in after.values()):
        raise LastAdministrator("A business needs at least one administrator")


class SetupItemKey(StrEnum):
    BUSINESS_DETAILS = "business_details"
    PDF_DETAILS = "pdf_details"
    PRICING = "pricing"
    APPROVER = "approver"
    PRODUCT_SOURCE = "product_source"
    CUSTOMER_CHANNEL = "customer_channel"
    TEST_ENQUIRY = "test_enquiry"


# Q11: everything but the product source (products arrive in phase 3).
REQUIRED_FOR_GO_LIVE: frozenset[SetupItemKey] = frozenset(SetupItemKey) - {
    SetupItemKey.PRODUCT_SOURCE
}


@dataclass(frozen=True)
class SetupItem:
    key: SetupItemKey
    done: bool

    @property
    def required(self) -> bool:
        return self.key in REQUIRED_FOR_GO_LIVE


def missing_for_go_live(items: Sequence[SetupItem]) -> list[SetupItemKey]:
    """Required items that are not done, in checklist order. Absent items count as missing."""
    done = {item.key for item in items if item.done}
    return [key for key in SetupItemKey if key in REQUIRED_FOR_GO_LIVE and key not in done]
