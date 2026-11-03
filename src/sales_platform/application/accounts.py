"""Sign-up, login, businesses, members, invitations, portal API keys and account emails.

Rules every service here follows (phase 2 plan, user answers 2026-10-27):
- Rate limits are checked before any expensive or revealing work (Q4; no lockout, Q3).
- Answers never reveal whether an email has an account: sign-up always says "check your
  email", login always says "email or password is incorrect", forgot-password always accepts.
  Unknown emails still pay for one password hash, so timing does not tell either.
- Tokens from emails are only ever compared by their SHA-256.
- A suspended business is read-only; only Go live changes setting_up to active.
"""

import base64
import hashlib
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sales_platform.application.api_keys import NewApiKey, generate_api_key, hash_secret
from sales_platform.application.sessions import utc_now
from sales_platform.domain.accounts import (
    PASSWORD_POLICY,
    TOKEN_LIFETIME,
    AccountEmailPurpose,
    InvalidEmail,
    Invitation,
    PasswordProblem,
    RateRule,
    RateRules,
    SetupItem,
    SetupItemKey,
    User,
    check_administrator_remains,
    clean_display_name,
    email_hint,
    normalize_email,
    password_problems,
    validate_people_roles,
)
from sales_platform.domain.documents import printable_text
from sales_platform.domain.jobs import PermanentJobError
from sales_platform.domain.messages import ACCOUNT_TEMPLATES, render_template
from sales_platform.domain.money import RoundingMode, validate_currency
from sales_platform.domain.tenancy import (
    Permission,
    Role,
    TenantContext,
    allows_changes,
)
from sales_platform.ports.accounts import (
    AccountEmailStore,
    ApiKeyAdminStore,
    ApiKeySummary,
    BusinessSettings,
    BusinessStore,
    InvitationPreview,
    InvitationStore,
    LogoNormalizer,
    Mailer,
    MailRejected,
    Member,
    MemberStore,
    NewBusiness,
    NotAllowed,
    OutgoingEmail,
    PasswordHasher,
    PricingSettings,
    ProfileUpdate,
    RateLimiter,
    StoredPricing,
    UserStore,
)
from sales_platform.ports.identity import IdentityRejected, SessionStore, VerifiedIdentity
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.ports.storage import ObjectNotFound, ObjectStorage

logger = logging.getLogger(__name__)

PASSWORD_PROVIDER = "password"
LOGIN_FAILED = "Email or password is incorrect"
MAX_ADDRESS_LINES = 4


@dataclass(frozen=True)
class ClientInfo:
    """Who is calling, for rate limits (the client IP; see SALES_TRUSTED_PROXY_COUNT)."""

    ip: str


class RateLimited(Exception):
    def __init__(self, retry_after: timedelta) -> None:
        super().__init__("Too many attempts; try again later")
        self.retry_after = retry_after


class PasswordRejected(ValueError):
    def __init__(self, problems: list[PasswordProblem]) -> None:
        super().__init__("The password does not meet the rules")
        self.problems = problems


class InvalidLink(Exception):
    """An email link that is unknown, used, revoked or expired. One message for all."""


def _token_hash(token: str) -> str | None:
    """None for anything that cannot be one of our tokens (no lookup is made then)."""
    if not token or len(token) > 100:
        return None
    return hash_secret(token)


class _Limits:
    def __init__(self, limiter: RateLimiter, clock: Callable[[], datetime]) -> None:
        self._limiter = limiter
        self._clock = clock

    def check(self, rule: RateRule, key: str) -> None:
        decision = self._limiter.hit(rule, key, self._clock())
        if not decision.allowed:
            raise RateLimited(decision.retry_after)

    def allowed(self, rule: RateRule, key: str) -> bool:
        return self._limiter.hit(rule, key, self._clock()).allowed


def _check_password(password: str, email: str, common: frozenset[str]) -> None:
    problems = password_problems(password, email=email, policy=PASSWORD_POLICY, common=common)
    if problems:
        raise PasswordRejected(problems)


class SignupService:
    def __init__(
        self,
        users: UserStore,
        hasher: PasswordHasher,
        limiter: RateLimiter,
        common_passwords: frozenset[str],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._limits = _Limits(limiter, clock)
        self._common = common_passwords
        self._clock = clock

    def sign_up(
        self, email: str, password: str, display_name: str | None, client: ClientInfo
    ) -> None:
        """Same result for a new and an existing address (Q7); no session is started."""
        self._limits.check(RateRules.SIGNUP_PER_IP, client.ip)
        normalized = normalize_email(email)  # InvalidEmail: the form shows it, reveals nothing
        name = clean_display_name(display_name)
        _check_password(password, normalized, self._common)
        password_hash = self._hasher.hash(password)  # always, so timing does not differ

        def notify() -> bool:
            """Asked only when the address exists: at most the resend allowance of notices."""
            key = f"exists:{normalized}"
            return self._limits.allowed(RateRules.RESEND_PER_MINUTE, key) and self._limits.allowed(
                RateRules.RESEND_PER_DAY, key
            )

        self._users.sign_up(
            uuid4(), email.strip(), normalized, name, password_hash, notify_existing=notify
        )

    def verify_email(self, token: str) -> None:
        token_hash = _token_hash(token)
        if token_hash is None or not self._users.verify_email(token_hash, self._clock()):
            raise InvalidLink("This link is not valid any more")

    def resend_verification(self, subject: str) -> None:
        self._limits.check(RateRules.RESEND_PER_MINUTE, subject)
        self._limits.check(RateRules.RESEND_PER_DAY, subject)
        self._users.request_verification(subject)


class LoginService:
    def __init__(
        self,
        users: UserStore,
        hasher: PasswordHasher,
        limiter: RateLimiter,
        sessions: SessionStore,
        common_passwords: frozenset[str],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._limits = _Limits(limiter, clock)
        self._sessions = sessions
        self._common = common_passwords
        self._clock = clock

    def login(self, email: str, password: str, client: ClientInfo) -> VerifiedIdentity:
        self._limits.check(RateRules.LOGIN_PER_IP, client.ip)
        try:
            normalized = normalize_email(email)
        except InvalidEmail:
            normalized = None
        if normalized is not None:
            self._limits.check(RateRules.LOGIN_PER_EMAIL, normalized)
        if len(password) > PASSWORD_POLICY.max_length:
            raise IdentityRejected(LOGIN_FAILED)  # never hash unbounded input
        record = None if normalized is None else self._users.find_for_login(normalized)
        if record is None or record.password_hash is None or record.user.disabled:
            self._hasher.verify(self._hasher.dummy_hash, password)  # same work as a real check
            raise IdentityRejected(LOGIN_FAILED)
        if not self._hasher.verify(record.password_hash, password):
            raise IdentityRejected(LOGIN_FAILED)
        if self._hasher.needs_rehash(record.password_hash):
            self._users.store_rehash(record.user.user_id, self._hasher.hash(password))
        return VerifiedIdentity(record.user.subject, PASSWORD_PROVIDER)

    def forgot_password(self, email: str, client: ClientInfo) -> None:
        """Always accepted; an email is sent only when the account exists."""
        self._limits.check(RateRules.FORGOT_PER_IP, client.ip)
        try:
            normalized = normalize_email(email)
        except InvalidEmail:
            return
        self._limits.check(RateRules.FORGOT_PER_EMAIL, normalized)
        self._users.request_password_reset(normalized)

    def reset_password(self, token: str, new_password: str) -> None:
        """Signs the person out everywhere and emails a "password changed" notice."""
        token_hash = _token_hash(token)
        email = None if token_hash is None else self._users.reset_email(token_hash, self._clock())
        if token_hash is None or email is None:
            raise InvalidLink("This link is not valid any more")
        _check_password(new_password, email, self._common)
        subject = self._users.reset_password(
            token_hash, self._hasher.hash(new_password), self._clock()
        )
        if subject is None:
            raise InvalidLink("This link is not valid any more")
        self._sessions.revoke_all_for(subject)

    def change_password(
        self, subject: str, current: str, new_password: str, session_token_sha256: str
    ) -> None:
        """Other sessions are signed out; this one stays."""
        self._limits.check(RateRules.LOGIN_PER_EMAIL, f"change:{subject}")
        user = self._users.by_subject(subject)
        stored = self._users.password_hash(subject)
        if user is None or len(current) > PASSWORD_POLICY.max_length:
            raise IdentityRejected("The current password is incorrect")
        if stored is None or not self._hasher.verify(stored, current):
            raise IdentityRejected("The current password is incorrect")
        _check_password(new_password, user.email or "", self._common)
        self._users.change_password(subject, self._hasher.hash(new_password))
        self._sessions.revoke_all_for(subject, keep_token_sha256=session_token_sha256)


class ProfileService:
    def __init__(self, users: UserStore) -> None:
        self._users = users

    def get(self, subject: str) -> User | None:
        return self._users.by_subject(subject)

    def update(self, subject: str, display_name: str | None) -> User:
        return self._users.update_profile(subject, clean_display_name(display_name))


def _single_line(value: str, field: str, max_length: int) -> str:
    text = " ".join(value.split())
    if not text or len(text) > max_length or not text.isprintable():
        raise ValueError(f"{field} must be 1 to {max_length} printable characters")
    return text


def _optional_pdf_text(value: str | None, field: str, max_length: int) -> str | None:
    if value is None or not value.strip():
        return None
    text = _single_line(value, field, max_length)
    printable_text(text, field)  # UnrenderableContent if the PDF could not show it
    return text


def _country(value: str) -> str:
    country = value.strip().upper()
    if len(country) != 2 or not country.isascii() or not country.isalpha():
        raise ValueError("Country must be a two-letter code, e.g. IN")
    return country


def _tax_rate(value: Decimal) -> Decimal:
    if not value.is_finite() or not Decimal(0) <= value < Decimal(1):
        raise ValueError("Tax rate is a fraction between 0 and 1, e.g. 0.18")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -6:
        raise ValueError("Tax rate can have at most 6 decimal places")
    return value


def _validity(days: int) -> int:
    if days < 1:
        raise ValueError("Quotations must be valid for at least 1 day")
    return days


def validate_pricing(pricing: PricingSettings) -> PricingSettings:
    """The pricing screen saves every field (Q14), with the calculator's own rules."""
    if not pricing.complete:
        raise ValueError("Fill in every pricing field")
    assert pricing.currency is not None and pricing.tax_rate is not None
    validate_currency(pricing.currency)
    _tax_rate(pricing.tax_rate)
    if pricing.minor_units is None or not 0 <= pricing.minor_units <= 4:
        raise ValueError("Decimal places must be between 0 and 4")
    try:
        RoundingMode(pricing.rounding_mode or "")
    except ValueError:
        raise ValueError("Unknown rounding mode") from None
    for seconds in (pricing.max_price_age_seconds, pricing.max_stock_age_seconds):
        if seconds is None or seconds <= 0:
            raise ValueError("Freshness limits must be positive")
    _validity(pricing.quote_validity_days)
    return pricing


def logo_key(tenant_id: UUID, sha256: str) -> str:
    """Server-generated key; the uploaded file name is never used."""
    return f"tenants/{tenant_id}/profile/logo-{sha256}.png"


class BusinessService:
    def __init__(
        self,
        businesses: BusinessStore,
        users: UserStore,
        limiter: RateLimiter,
        storage: ObjectStorage,
        logos: LogoNormalizer,
        clock: Callable[[], datetime] = utc_now,
        product_data: Callable[[TenantContext], bool] | None = None,
    ) -> None:
        """`product_data` decides the checklist's product step (phase 3): our product list
        needs a priced product; without it, an active product source is enough."""
        self._businesses = businesses
        self._users = users
        self._limits = _Limits(limiter, clock)
        self._storage = storage
        self._logos = logos
        self._product_data = product_data

    def create(self, subject: str, business: NewBusiness) -> UUID:
        user = self._users.by_subject(subject)
        if user is None or not user.email_verified or user.disabled:
            raise NotAllowed("Confirm your email address before creating a business")
        self._limits.check(RateRules.CREATE_BUSINESS_PER_USER, subject)
        cleaned = NewBusiness(
            name=_single_line(business.name, "Business name", 200),
            country=_country(business.country),
            time_zone=business.time_zone.strip(),
            quote_validity_days=_validity(business.quote_validity_days),
            currency=None if business.currency is None else business.currency.strip().upper(),
            tax_rate=None if business.tax_rate is None else _tax_rate(business.tax_rate),
        )
        if cleaned.currency is not None:
            validate_currency(cleaned.currency)
        return self._businesses.create(user.user_id, subject, cleaned)

    def settings(self, context: TenantContext) -> BusinessSettings:
        return self._businesses.settings(context)

    def update_details(
        self, context: TenantContext, name: str, country: str, time_zone: str
    ) -> None:
        self._writable(context)
        self._businesses.update_details(
            context, _single_line(name, "Business name", 200), _country(country), time_zone.strip()
        )

    def update_profile(
        self, context: TenantContext, profile: ProfileUpdate, expected_version: int
    ) -> int:
        self._writable(context)
        address = None
        if profile.address is not None and profile.address.strip():
            lines = [line.strip() for line in profile.address.splitlines() if line.strip()]
            if len(lines) > MAX_ADDRESS_LINES:
                raise ValueError("The address can have at most 4 lines")
            for line in lines:
                printable_text(line, "address")
            address = "\n".join(lines)
            if len(address) > 500:
                raise ValueError("The address can have at most 500 characters")
        cleaned = ProfileUpdate(
            legal_name=_optional_pdf_text(profile.legal_name, "Legal name", 200),
            address=address,
            tax_id_label=_optional_pdf_text(profile.tax_id_label, "Tax ID label", 32),
            tax_id=_optional_pdf_text(profile.tax_id, "Tax ID", 64),
        )
        return self._businesses.update_profile(context, cleaned, expected_version)

    def upload_logo(self, context: TenantContext, content_base64: str) -> None:
        self._writable(context)
        try:
            data = base64.b64decode(content_base64, validate=True)
        except ValueError:
            raise ValueError("The logo must be sent as base64") from None
        png = self._logos.normalize(data)
        digest = hashlib.sha256(png).hexdigest()
        self._storage.put(logo_key(context.tenant_id, digest), png)
        self._businesses.set_logo(context, logo_key(context.tenant_id, digest), digest)

    def remove_logo(self, context: TenantContext) -> None:
        """The file stays: PDFs already issued may need it to be re-checked or re-rendered."""
        self._writable(context)
        self._businesses.set_logo(context, None, None)

    def logo(self, context: TenantContext) -> bytes | None:
        stored = self._businesses.logo_key(context)
        if stored is None:
            return None
        key, digest = stored
        try:
            data = self._storage.get(key)
        except ObjectNotFound:
            return None
        return data if hashlib.sha256(data).hexdigest() == digest else None

    def pricing(self, context: TenantContext) -> StoredPricing:
        return self._businesses.pricing(context)

    def update_pricing(
        self, context: TenantContext, pricing: PricingSettings, expected_version: int
    ) -> int:
        self._writable(context)
        cleaned = PricingSettings(
            currency=None if pricing.currency is None else pricing.currency.strip().upper(),
            minor_units=pricing.minor_units,
            rounding_mode=pricing.rounding_mode,
            tax_rate=pricing.tax_rate,
            max_price_age_seconds=pricing.max_price_age_seconds,
            max_stock_age_seconds=pricing.max_stock_age_seconds,
            quote_validity_days=pricing.quote_validity_days,
        )
        return self._businesses.update_pricing(context, validate_pricing(cleaned), expected_version)

    def checklist(self, context: TenantContext) -> list[SetupItem]:
        items = self._businesses.setup_state(context)
        if self._product_data is None:
            return items
        return [
            SetupItem(item.key, item.done and self._product_data(context))
            if item.key is SetupItemKey.PRODUCT_SOURCE
            else item
            for item in items
        ]

    def go_live(self, context: TenantContext) -> list[SetupItemKey]:
        context.require(Permission.MANAGE_TENANT)
        return self._businesses.go_live(context)

    def _writable(self, context: TenantContext) -> None:
        context.require(Permission.MANAGE_TENANT)
        if not allows_changes(self._businesses.status(context.tenant_id)):
            raise NotAllowed("This business is suspended")


def parse_decimal(value: str, field: str) -> Decimal:
    """Decimals arrive as strings (never floats) and are parsed exactly."""
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError(f"{field} must be a number") from None
    if not result.is_finite():
        raise ValueError(f"{field} must be a number")
    return result


class MemberService:
    def __init__(self, members: MemberStore, businesses: BusinessStore) -> None:
        self._members = members
        self._businesses = businesses

    def list(self, context: TenantContext) -> list[Member]:
        context.require(Permission.MANAGE_MEMBERS)
        return self._members.list(context)

    def change_roles(self, context: TenantContext, user_id: UUID, roles: Sequence[str]) -> None:
        context.require(Permission.MANAGE_MEMBERS)
        self._writable(context)
        wanted = validate_people_roles(roles)
        self._check_last_administrator(context, user_id, wanted)
        self._members.change_roles(context, user_id, wanted)

    def remove(self, context: TenantContext, user_id: UUID, self_user_id: UUID | None) -> None:
        """Administrators remove anyone; everyone may leave (Q22)."""
        if user_id != self_user_id:
            context.require(Permission.MANAGE_MEMBERS)
        self._writable(context)
        if Permission.MANAGE_MEMBERS in context.permissions:
            self._check_last_administrator(context, user_id, frozenset())
        self._members.remove(context, user_id)

    def _check_last_administrator(
        self, context: TenantContext, user_id: UUID, roles: frozenset[Role]
    ) -> None:
        members = self._members.list(context)
        target = next((m for m in members if m.user_id == user_id), None)
        if target is None:
            raise NotAllowed("This person is not a member")
        check_administrator_remains({m.subject: m.roles for m in members}, target.subject, roles)

    def _writable(self, context: TenantContext) -> None:
        if not allows_changes(self._businesses.status(context.tenant_id)):
            raise NotAllowed("This business is suspended")


class InvitationService:
    def __init__(
        self,
        invitations: InvitationStore,
        businesses: BusinessStore,
        users: UserStore,
        limiter: RateLimiter,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._invitations = invitations
        self._businesses = businesses
        self._users = users
        self._limits = _Limits(limiter, clock)
        self._clock = clock

    def invite(self, context: TenantContext, email: str, roles: Sequence[str]) -> UUID:
        context.require(Permission.MANAGE_MEMBERS)
        if not allows_changes(self._businesses.status(context.tenant_id)):
            raise NotAllowed("This business is suspended")
        normalized = normalize_email(email)
        wanted = validate_people_roles(roles)
        self._limits.check(RateRules.INVITES_PER_BUSINESS, str(context.tenant_id))
        return self._invitations.invite(context, email.strip(), normalized, wanted)

    def list(self, context: TenantContext) -> list[Invitation]:
        context.require(Permission.MANAGE_MEMBERS)
        return self._invitations.list(context)

    def resend(self, context: TenantContext, invitation_id: UUID) -> bool:
        context.require(Permission.MANAGE_MEMBERS)
        self._limits.check(RateRules.INVITES_PER_BUSINESS, str(context.tenant_id))
        return self._invitations.resend(context, invitation_id)

    def revoke(self, context: TenantContext, invitation_id: UUID) -> bool:
        context.require(Permission.MANAGE_MEMBERS)
        return self._invitations.revoke(context, invitation_id)

    def preview(self, token: str, client: ClientInfo) -> InvitationPreview:
        self._limits.check(RateRules.INVITATION_PER_IP, client.ip)
        token_hash = _token_hash(token)
        found = None if token_hash is None else self._invitations.preview(token_hash, self._clock())
        if found is None:
            raise InvalidLink("This invitation cannot be used")
        return InvitationPreview(
            found.business_name, found.roles, email_hint(found.email), found.expires_at
        )

    def accept(self, subject: str, token: str, client: ClientInfo) -> UUID:
        """The signed-in person must use the invited address (Q9); the link verifies it."""
        self._limits.check(RateRules.INVITATION_PER_IP, client.ip)
        token_hash = _token_hash(token)
        user = self._users.by_subject(subject)
        if token_hash is None or user is None:
            raise InvalidLink("This invitation cannot be used")
        return self._invitations.accept(token_hash, user.user_id, subject)


class ApiKeyAdminService:
    """Intake keys from the portal (Q17): shown once, revocable, audited."""

    def __init__(self, keys: ApiKeyAdminStore, businesses: BusinessStore) -> None:
        self._keys = keys
        self._businesses = businesses

    def create(self, context: TenantContext, name: str) -> NewApiKey:
        context.require(Permission.MANAGE_INTEGRATIONS)
        if not allows_changes(self._businesses.status(context.tenant_id)):
            raise NotAllowed("This business is suspended")
        key = generate_api_key()
        self._keys.create(
            context,
            key.key_id,
            key.secret_sha256,
            _single_line(name, "Key name", 100),
            Role.INTAKE_SERVICE,
        )
        return key

    def list(self, context: TenantContext) -> list[ApiKeySummary]:
        context.require(Permission.MANAGE_INTEGRATIONS)
        return self._keys.list(context)

    def revoke(self, context: TenantContext, key_id: UUID) -> bool:
        context.require(Permission.MANAGE_INTEGRATIONS)
        return self._keys.revoke(context, key_id)


# Where each email's link points in the portal (hash routes; the token stays in the fragment,
# so it never reaches server logs or Referer headers).
_LINK_PATHS = {
    AccountEmailPurpose.VERIFY_EMAIL: "#/verify-email?token=",
    AccountEmailPurpose.PASSWORD_RESET: "#/reset-password?token=",
    AccountEmailPurpose.INVITATION: "#/invitations/accept?token=",
    AccountEmailPurpose.ACCOUNT_EXISTS: "#/login",
    AccountEmailPurpose.PASSWORD_CHANGED: "#/forgot-password",
}
_ROLE_NAMES = {
    Role.ADMINISTRATOR: "administrator",
    Role.SALES_OPERATOR: "sales operator",
    Role.APPROVER: "approver",
    Role.VIEWER: "viewer",
}


class AccountEmailHandler:
    """Job send_account_email: payload {purpose, ref_id} only, never an address or token.

    For emails with a link, the single-use token is created here, its hash stored together with
    the pending -> sending compare-and-set, then the email is handed to the mailer. Outcomes are
    honest (ADR 0015 rules): a crash after handing over becomes `unknown` and is never sent
    again automatically; the person can press "resend", which makes a fresh link.
    """

    def __init__(
        self,
        store: AccountEmailStore,
        mailer: Mailer,
        portal_url: str,
        sender: str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._mailer = mailer
        self._portal_url = portal_url
        self._sender = sender
        self._clock = clock

    def __call__(self, job: ClaimedJob) -> None:
        try:
            purpose = AccountEmailPurpose(job.payload["purpose"])
            ref_id = UUID(job.payload["ref_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid purpose and ref_id") from None
        tenant_id = job.tenant_id
        if (purpose is AccountEmailPurpose.INVITATION) != (tenant_id is not None):
            raise PermanentJobError("Only invitation emails belong to a business")
        if self._store.mark_unknown_if_sending(purpose, ref_id, tenant_id):
            logger.warning("Account email %s %s: earlier attempt unknown", purpose, ref_id)
            return
        lifetime = TOKEN_LIFETIME.get(purpose)
        token = secrets.token_urlsafe(32) if lifetime is not None else None
        token_hash = None if token is None else hash_secret(token)
        expires_at = None if lifetime is None else self._clock() + lifetime
        work = self._store.prepare(purpose, ref_id, tenant_id, token_hash, expires_at)
        if work is None:
            return  # used, revoked, replaced or already handed over
        rendered = render_template(
            ACCOUNT_TEMPLATES[purpose],
            {
                "link": self._portal_url + _LINK_PATHS[purpose] + (token or ""),
                "business": work.business_name or "",
                "roles": ", ".join(sorted(_ROLE_NAMES[role] for role in work.roles)),
            },
        )
        message = OutgoingEmail(
            to=work.to,
            sender=self._sender,
            subject=rendered.metadata["subject"],
            body=rendered.text,
            idempotency_key=f"{purpose}:{ref_id}:{(token_hash or 'notice')[:12]}",
        )
        try:
            self._mailer.send(message)
        except MailRejected:
            self._store.finish(purpose, ref_id, tenant_id, "failed")
            return
        except Exception:
            # MailUncertain or anything unexpected after handing over: it may have gone out.
            logger.exception("Account email %s %s: outcome unknown", purpose, ref_id)
            self._store.finish(purpose, ref_id, tenant_id, "unknown")
            return
        self._store.finish(purpose, ref_id, tenant_id, "sent")
