"""Account services with in-memory fakes: enumeration safety, limits, links and permissions."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import pytest

from sales_platform.application.accounts import (
    LOGIN_FAILED,
    AccountEmailHandler,
    ApiKeyAdminService,
    BusinessService,
    ClientInfo,
    InvalidLink,
    InvitationService,
    LoginService,
    MemberService,
    PasswordRejected,
    RateLimited,
    SignupService,
    validate_pricing,
)
from sales_platform.application.api_keys import hash_secret
from sales_platform.domain.accounts import (
    AccountEmailPurpose,
    InvalidEmail,
    LastAdministrator,
    PasswordProblem,
    RateRule,
    SetupItem,
    SetupItemKey,
    User,
)
from sales_platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.domain.tenancy import PermissionDenied, Role, TenantContext, TenantStatus
from sales_platform.ports.accounts import (
    AccountEmailWork,
    LoginRecord,
    MailRejected,
    MailUncertain,
    Member,
    NewBusiness,
    NotAllowed,
    OutgoingEmail,
    PricingSettings,
    RateDecision,
)
from sales_platform.ports.jobs import ClaimedJob

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
CLIENT = ClientInfo("203.0.113.7")
GOOD = "a long passphrase"


class FakeHasher:
    def __init__(self) -> None:
        self.hashed = 0
        self.verified: list[str] = []
        self.dummy_hash = "$argon2id$dummy"

    def hash(self, password: str) -> str:
        self.hashed += 1
        return f"$argon2id$v2${password}"

    def verify(self, password_hash: str, password: str) -> bool:
        self.verified.append(password_hash)
        return password_hash.endswith(f"${password}") and password_hash != self.dummy_hash

    def needs_rehash(self, password_hash: str) -> bool:
        return "$v1$" in password_hash


class FakeLimiter:
    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], int] = {}

    def hit(self, rule: RateRule, key: str, now: datetime) -> RateDecision:
        count = self.counts.get((rule.name, key), 0) + 1
        self.counts[(rule.name, key)] = count
        return RateDecision(count <= rule.limit, timedelta(minutes=1))


@dataclass
class FakeUsers:
    records: dict[str, LoginRecord] = field(default_factory=dict)
    signups: list[tuple[str, bool]] = field(default_factory=list)
    resets: dict[str, tuple[str, str]] = field(default_factory=dict)  # token hash -> (email, subj)
    changed: list[tuple[str, str]] = field(default_factory=list)
    rehashed: list[str] = field(default_factory=list)
    verified_tokens: set[str] = field(default_factory=set)

    def add(self, email: str, password_hash: str | None, **user: object) -> User:
        values: dict[str, object] = {
            "user_id": uuid4(),
            "subject": f"user:{email}",
            "email": email,
            "display_name": None,
            "email_verified": True,
            "disabled": False,
        }
        values.update(user)
        person = User(**values)  # type: ignore[arg-type]
        self.records[email] = LoginRecord(person, password_hash)
        return person

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
        exists = email_normalized in self.records
        self.signups.append((email_normalized, notify_existing() if exists else False))
        if not exists:
            self.add(email_normalized, password_hash, email_verified=False)
        return not exists

    def find_for_login(self, email_normalized: str) -> LoginRecord | None:
        return self.records.get(email_normalized)

    def _by_subject(self, subject: str) -> LoginRecord | None:
        return next((r for r in self.records.values() if r.user.subject == subject), None)

    def password_hash(self, subject: str) -> str | None:
        record = self._by_subject(subject)
        return None if record is None else record.password_hash

    def store_rehash(self, user_id: UUID, password_hash: str) -> None:
        self.rehashed.append(password_hash)

    def by_subject(self, subject: str) -> User | None:
        record = self._by_subject(subject)
        return None if record is None else record.user

    def update_profile(self, subject: str, display_name: str | None) -> User:
        raise NotImplementedError

    def verify_email(self, token_sha256: str, now: datetime) -> bool:
        return token_sha256 in self.verified_tokens

    def request_verification(self, subject: str) -> bool:
        return True

    def request_password_reset(self, email_normalized: str) -> None:
        self.signups.append((f"reset:{email_normalized}", False))

    def reset_email(self, token_sha256: str, now: datetime) -> str | None:
        found = self.resets.get(token_sha256)
        return None if found is None else found[0]

    def reset_password(self, token_sha256: str, password_hash: str, now: datetime) -> str | None:
        found = self.resets.pop(token_sha256, None)
        if found is None:
            return None
        self.changed.append((found[1], password_hash))
        return found[1]

    def change_password(self, subject: str, password_hash: str) -> None:
        self.changed.append((subject, password_hash))


class FakeSessions:
    def __init__(self) -> None:
        self.revoked: list[tuple[str, str | None]] = []

    def revoke_all_for(self, subject: str, keep_token_sha256: str | None = None) -> int:
        self.revoked.append((subject, keep_token_sha256))
        return 1


def signup(users: FakeUsers, hasher: FakeHasher, limiter: FakeLimiter) -> SignupService:
    return SignupService(users, hasher, limiter, frozenset({"correcthorse12"}), lambda: NOW)


def login(
    users: FakeUsers, hasher: FakeHasher, sessions: FakeSessions | None = None
) -> LoginService:
    return LoginService(
        users,
        hasher,
        FakeLimiter(),
        sessions or FakeSessions(),  # type: ignore[arg-type]
        frozenset({"correcthorse12"}),
        lambda: NOW,
    )


def test_sign_up_answers_the_same_for_new_and_existing_addresses() -> None:
    users, hasher, limiter = FakeUsers(), FakeHasher(), FakeLimiter()
    users.add("ana@shop.example", "$argon2id$v2$x")
    service = signup(users, hasher, limiter)
    service.sign_up("Ana@Shop.example", GOOD, None, CLIENT)  # no answer either way
    service.sign_up("bo@shop.example", GOOD, "Bo", CLIENT)
    assert hasher.hashed == 2  # existing address still pays for one hash
    assert users.signups == [("ana@shop.example", True), ("bo@shop.example", False)]


def test_existing_owner_gets_at_most_one_notice_a_minute() -> None:
    users, hasher, limiter = FakeUsers(), FakeHasher(), FakeLimiter()
    users.add("ana@shop.example", "$argon2id$v2$x")
    service = signup(users, hasher, limiter)
    service.sign_up("ana@shop.example", GOOD, None, CLIENT)
    service.sign_up("ana@shop.example", GOOD, None, CLIENT)
    assert [notify for _, notify in users.signups] == [True, False]


def test_sign_up_refuses_bad_passwords_and_addresses_and_is_rate_limited() -> None:
    service = signup(FakeUsers(), FakeHasher(), FakeLimiter())
    with pytest.raises(PasswordRejected) as rejected:
        service.sign_up("ana.lopez@shop.example", "ana.lopez-123", None, CLIENT)
    assert rejected.value.problems == [PasswordProblem.CONTAINS_EMAIL]
    with pytest.raises(InvalidEmail):
        service.sign_up("not-an-address", GOOD, None, CLIENT)
    for index in range(8):
        service.sign_up(f"p{index}@shop.example", GOOD, None, CLIENT)
    with pytest.raises(RateLimited):  # 10 per hour per IP, counting the two refused above
        service.sign_up("late@shop.example", GOOD, None, CLIENT)


def test_login_never_tells_which_part_was_wrong() -> None:
    users, hasher = FakeUsers(), FakeHasher()
    users.add("ana@shop.example", f"$argon2id$v2${GOOD}")
    users.add("off@shop.example", f"$argon2id$v2${GOOD}", disabled=True)
    users.add("nopass@shop.example", None)
    service = login(users, hasher)
    for email, password in [
        ("nobody@shop.example", GOOD),
        ("ana@shop.example", "wrong password!"),
        ("off@shop.example", GOOD),
        ("nopass@shop.example", GOOD),
        ("not an email", GOOD),
    ]:
        with pytest.raises(Exception, match=LOGIN_FAILED):
            service.login(email, password, CLIENT)
    # Unknown, disabled and password-less accounts still verified a (dummy) hash.
    assert hasher.verified.count(hasher.dummy_hash) == 4
    identity = service.login("ANA@shop.example", GOOD, CLIENT)
    assert identity.subject == "user:ana@shop.example" and identity.provider == "password"


def test_login_rehashes_outdated_parameters_and_limits_guessing() -> None:
    users, hasher = FakeUsers(), FakeHasher()
    users.add("ana@shop.example", f"$argon2id$v1${GOOD}")
    service = login(users, hasher)
    service.login("ana@shop.example", GOOD, CLIENT)
    assert users.rehashed == [f"$argon2id$v2${GOOD}"]
    for _ in range(9):
        with pytest.raises(Exception, match=LOGIN_FAILED):
            service.login("ana@shop.example", "wrong password!", CLIENT)
    with pytest.raises(RateLimited):  # 10 per 10 minutes per email, no lockout
        service.login("ana@shop.example", GOOD, CLIENT)


def test_reset_checks_the_rules_then_signs_out_everywhere() -> None:
    users, hasher, sessions = FakeUsers(), FakeHasher(), FakeSessions()
    users.resets[hash_secret("reset-token")] = ("ana.lopez@shop.example", "user:ana")
    service = login(users, hasher, sessions)
    with pytest.raises(PasswordRejected):
        service.reset_password("reset-token", "ana.lopez is me")
    service.reset_password("reset-token", GOOD)
    assert sessions.revoked == [("user:ana", None)]
    with pytest.raises(InvalidLink):
        service.reset_password("reset-token", GOOD)  # single use
    with pytest.raises(InvalidLink):
        service.reset_password("x" * 101, GOOD)


def test_change_password_keeps_this_session_only() -> None:
    users, hasher, sessions = FakeUsers(), FakeHasher(), FakeSessions()
    person = users.add("ana@shop.example", f"$argon2id$v2${GOOD}")
    service = login(users, hasher, sessions)
    with pytest.raises(Exception, match="current password"):
        service.change_password(person.subject, "wrong", "another long phrase", "keep")
    service.change_password(person.subject, GOOD, "another long phrase", "keep")
    assert sessions.revoked == [(person.subject, "keep")]


def test_forgot_password_always_accepts() -> None:
    users = FakeUsers()
    service = login(users, FakeHasher())
    service.forgot_password("nobody@shop.example", CLIENT)
    service.forgot_password("not an email", CLIENT)
    assert users.signups == [("reset:nobody@shop.example", False)]


@dataclass
class FakeBusinesses:
    status_value: TenantStatus = TenantStatus.SETTING_UP
    created: list[NewBusiness] = field(default_factory=list)

    def status(self, tenant_id: UUID) -> TenantStatus:
        return self.status_value

    def create(self, user_id: UUID, subject: str, business: NewBusiness) -> UUID:
        self.created.append(business)
        return uuid4()

    def go_live(self, context: TenantContext) -> list[object]:
        return []

    def setup_state(self, context: TenantContext) -> list[SetupItem]:
        return [SetupItem(key, True) for key in SetupItemKey]


def context(*roles: Role, subject: str = "user:admin") -> TenantContext:
    return TenantContext(UUID(int=1), subject, frozenset(roles))


def business_service(users: FakeUsers, businesses: FakeBusinesses) -> BusinessService:
    return BusinessService(
        businesses,  # type: ignore[arg-type]
        users,
        FakeLimiter(),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        lambda: NOW,
    )


NEW = NewBusiness("  Acme  Traders ", "in", "Asia/Kolkata", 7, "inr", Decimal("0.18"))


def test_only_verified_people_create_businesses() -> None:
    users, businesses = FakeUsers(), FakeBusinesses()
    unverified = users.add("new@shop.example", "h", email_verified=False)
    verified = users.add("ana@shop.example", "h")
    service = business_service(users, businesses)
    with pytest.raises(NotAllowed):
        service.create(unverified.subject, NEW)
    service.create(verified.subject, NEW)
    assert businesses.created == [
        NewBusiness("Acme Traders", "IN", "Asia/Kolkata", 7, "INR", Decimal("0.18"))
    ]
    with pytest.raises(ValueError, match="6 decimal"):
        service.create(verified.subject, replace(NEW, tax_rate=Decimal("0.1234567")))
    service.create(verified.subject, replace(NEW, currency=None, tax_rate=None))  # set later


def test_pricing_must_be_complete_and_valid() -> None:
    complete = PricingSettings("INR", 2, "half_up", Decimal("0.18"), 900, 300, 7)
    assert validate_pricing(complete) is complete
    for broken in (
        replace(complete, currency=None),
        replace(complete, rounding_mode="up"),
        replace(complete, minor_units=5),
        replace(complete, tax_rate=Decimal(1)),
        replace(complete, max_stock_age_seconds=0),
        replace(complete, quote_validity_days=0),
    ):
        with pytest.raises(ValueError):
            validate_pricing(broken)


def test_suspended_businesses_are_read_only() -> None:
    service = ApiKeyAdminService(None, FakeBusinesses(TenantStatus.SUSPENDED))  # type: ignore[arg-type]
    with pytest.raises(NotAllowed):
        service.create(context(Role.ADMINISTRATOR), "Shop site")
    with pytest.raises(PermissionDenied):
        service.create(context(Role.APPROVER), "Shop site")


class FakeMembers:
    def __init__(self, members: list[Member]) -> None:
        self.members = members
        self.calls: list[str] = []

    def list(self, context: TenantContext) -> list[Member]:
        return self.members

    def change_roles(self, context: TenantContext, user_id: UUID, roles: frozenset[Role]) -> None:
        self.calls.append("change")

    def remove(self, context: TenantContext, user_id: UUID) -> None:
        self.calls.append("remove")


def test_members_keep_an_administrator_and_anyone_may_leave() -> None:
    admin = Member(uuid4(), "user:admin", "a@x.example", None, frozenset({Role.ADMINISTRATOR}))
    seller = Member(uuid4(), "user:seller", "s@x.example", None, frozenset({Role.SALES_OPERATOR}))
    store = FakeMembers([admin, seller])
    service = MemberService(store, FakeBusinesses())  # type: ignore[arg-type]
    with pytest.raises(LastAdministrator):
        service.change_roles(context(Role.ADMINISTRATOR), admin.user_id, ["viewer"])
    with pytest.raises(LastAdministrator):
        service.remove(context(Role.ADMINISTRATOR), admin.user_id, admin.user_id)
    with pytest.raises(ValueError):
        service.change_roles(context(Role.ADMINISTRATOR), seller.user_id, ["intake_service"])
    with pytest.raises(PermissionDenied):
        service.remove(context(Role.SALES_OPERATOR, subject="user:seller"), admin.user_id, None)
    service.remove(context(Role.SALES_OPERATOR, subject="user:seller"), seller.user_id,
                   seller.user_id)  # fmt: skip
    assert store.calls == ["remove"]


def test_invitations_need_members_manage_and_people_roles() -> None:
    service = InvitationService(
        None,  # type: ignore[arg-type]
        FakeBusinesses(),  # type: ignore[arg-type]
        FakeUsers(),
        FakeLimiter(),
    )
    with pytest.raises(PermissionDenied):
        service.invite(context(Role.APPROVER), "bo@shop.example", ["viewer"])
    with pytest.raises(ValueError):
        service.invite(context(Role.ADMINISTRATOR), "bo@shop.example", ["intake_service"])


class FakeEmails:
    def __init__(self, work: AccountEmailWork | None, sending: bool = False) -> None:
        self.work = work
        self.sending = sending
        self.prepared: list[tuple[str | None, datetime | None]] = []
        self.finished: list[str] = []

    def prepare(
        self,
        purpose: AccountEmailPurpose,
        ref_id: UUID,
        tenant_id: UUID | None,
        token_sha256: str | None,
        expires_at: datetime | None,
    ) -> AccountEmailWork | None:
        self.prepared.append((token_sha256, expires_at))
        return self.work

    def finish(
        self,
        purpose: AccountEmailPurpose,
        ref_id: UUID,
        tenant_id: UUID | None,
        status: Literal["sent", "failed", "unknown"],
    ) -> None:
        self.finished.append(status)

    def mark_unknown_if_sending(
        self, purpose: AccountEmailPurpose, ref_id: UUID, tenant_id: UUID | None
    ) -> bool:
        return self.sending


class FakeMailer:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[OutgoingEmail] = []
        self.error = error

    def send(self, message: OutgoingEmail) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def job(purpose: AccountEmailPurpose, tenant_id: UUID | None = None) -> ClaimedJob:
    return ClaimedJob(
        uuid4(),
        tenant_id,
        JobKind.SEND_ACCOUNT_EMAIL,
        {"purpose": purpose.value, "ref_id": str(uuid4())},
        1,
        5,
        uuid4(),
        uuid4(),
    )


def handler(emails: FakeEmails, mailer: FakeMailer) -> AccountEmailHandler:
    return AccountEmailHandler(
        emails, mailer, "http://localhost:8000/portal/", "no-reply@x.example", lambda: NOW
    )


def test_the_link_token_exists_only_in_the_email() -> None:
    emails = FakeEmails(AccountEmailWork("ana@shop.example", None, frozenset()))
    mailer = FakeMailer()
    work = job(AccountEmailPurpose.VERIFY_EMAIL)
    assert "token" not in str(work.payload)
    handler(emails, mailer)(work)
    [message] = mailer.sent
    token = message.body.split("#/verify-email?token=")[1].split()[0]
    assert emails.prepared == [(hash_secret(token), NOW + timedelta(hours=24))]
    assert emails.finished == ["sent"] and message.to == "ana@shop.example"
    assert token not in message.idempotency_key


def test_invitation_email_names_the_business_and_roles() -> None:
    roles = frozenset({Role.APPROVER, Role.SALES_OPERATOR})
    emails = FakeEmails(AccountEmailWork("bo@shop.example", "Acme", roles))
    mailer = FakeMailer()
    handler(emails, mailer)(job(AccountEmailPurpose.INVITATION, uuid4()))
    assert mailer.sent[0].subject == "You are invited to Acme"
    assert "as approver, sales operator" in mailer.sent[0].body
    assert "#/invitations/accept?token=" in mailer.sent[0].body
    with pytest.raises(PermanentJobError):
        handler(emails, mailer)(job(AccountEmailPurpose.INVITATION))  # no business


def test_notices_have_no_token() -> None:
    emails = FakeEmails(AccountEmailWork("ana@shop.example", None, frozenset()))
    mailer = FakeMailer()
    handler(emails, mailer)(job(AccountEmailPurpose.ACCOUNT_EXISTS))
    assert emails.prepared == [(None, None)]
    assert mailer.sent[0].body.count("http://localhost:8000/portal/#/login") == 1


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (MailRejected("no"), "failed"),
        (MailUncertain("timeout"), "unknown"),
        (RuntimeError("boom"), "unknown"),
    ],  # fmt: skip
)
def test_send_outcomes_are_honest_and_never_retried(error: Exception, status: str) -> None:
    emails = FakeEmails(AccountEmailWork("ana@shop.example", None, frozenset()))
    handler(emails, FakeMailer(error))(job(AccountEmailPurpose.PASSWORD_RESET))
    assert emails.finished == [status]


def test_a_used_link_or_a_crashed_attempt_sends_nothing() -> None:
    mailer = FakeMailer()
    handler(FakeEmails(None), mailer)(job(AccountEmailPurpose.VERIFY_EMAIL))
    crashed = FakeEmails(AccountEmailWork("ana@shop.example", None, frozenset()), sending=True)
    handler(crashed, mailer)(job(AccountEmailPurpose.VERIFY_EMAIL))
    assert mailer.sent == [] and crashed.prepared == []


def test_checklist_product_step_asks_the_product_list() -> None:
    """Phase 3: an active source is not enough for our own list; it needs a priced product."""
    users, businesses = FakeUsers(), FakeBusinesses()
    ready = {"value": False}
    service = BusinessService(
        businesses,  # type: ignore[arg-type]
        users,
        FakeLimiter(),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        lambda: NOW,
        product_data=lambda context: ready["value"],
    )
    steps = {item.key: item.done for item in service.checklist(context(Role.ADMINISTRATOR))}
    assert steps[SetupItemKey.PRODUCT_SOURCE] is False and steps[SetupItemKey.PRICING] is True
    ready["value"] = True
    steps = {item.key: item.done for item in service.checklist(context(Role.ADMINISTRATOR))}
    assert steps[SetupItemKey.PRODUCT_SOURCE] is True
