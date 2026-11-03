"""Phase 2 accounts on real PostgreSQL with the restricted roles.

Covers the narrow policies on global tables, single-use email links, the four SECURITY DEFINER
functions (including attempts to abuse them), the status trigger, Go live, rate limits and the
isolation of two businesses created through sign-up.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from sales_platform.accounts.application.api_keys import generate_api_key
from sales_platform.accounts.domain.accounts import (
    AccountEmailPurpose,
    LastAdministrator,
    SetupItemKey,
)
from sales_platform.accounts.persistence.accounts import (
    SqlAccountEmailStore,
    SqlBusinessStore,
    SqlInvitationStore,
    SqlMemberStore,
    SqlUserStore,
)
from sales_platform.accounts.persistence.api_keys import SqlApiKeyAdminStore
from sales_platform.accounts.ports.accounts import (
    InvitationNotUsable,
    NewBusiness,
    NotAllowed,
    PricingSettings,
)
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.domain.rate_limits import RateRule
from sales_platform.platform.domain.security import hash_secret
from sales_platform.platform.domain.tenancy import Role, TenantContext, TenantStatus
from sales_platform.platform.persistence.rate_limits import SqlRateLimiter
from sales_platform.platform.persistence.sessions import SqlSessionStore
from sales_platform.platform.persistence.tenancy import SqlMembershipReader, tenant_transaction
from sales_platform.platform.ports.identity import VerifiedIdentity
from sales_platform.quotations.persistence.pricing_policy import SqlPricingPolicyReader
from sales_platform.quotations.ports.pricing_policy import PricingPolicyNotConfigured

pytestmark = pytest.mark.usefixtures("fresh_accounts")

HASH = "$argon2id$v=19$m=8,t=1,p=1$c2FsdHNhbHQ$synthetic"
NEW = NewBusiness("Acme Traders", "IN", "Asia/Kolkata", 7, "INR", Decimal("0.18"))


def jobs(owner_engine: Engine) -> list[tuple[UUID | None, str, dict[str, str]]]:
    with owner_engine.begin() as connection:
        connection.execute(text("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY"))
        rows = connection.execute(
            text("SELECT tenant_id, kind, payload FROM jobs ORDER BY created_at")
        ).all()
        connection.execute(text("ALTER TABLE jobs FORCE ROW LEVEL SECURITY"))
    return [(row.tenant_id, row.kind, row.payload) for row in rows]


def send_link(
    worker_engine: Engine, purpose: AccountEmailPurpose, ref_id: str, tenant: UUID | None = None
) -> str:
    """What the worker does: mint a token, store its hash, mark it sent."""
    emails = SqlAccountEmailStore(worker_engine)
    token = uuid4().hex
    work = emails.prepare(
        purpose, UUID(ref_id), tenant, hash_secret(token), datetime.now(UTC) + timedelta(hours=1)
    )
    assert work is not None
    emails.finish(purpose, UUID(ref_id), tenant, "sent")
    return token


def person(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine, email: str
) -> tuple[UUID, str]:
    """A signed-up person with a verified email."""
    users = SqlUserStore(runtime_engine)
    user_id = uuid4()
    assert users.sign_up(user_id, email, email, None, HASH, notify_existing=lambda: False)
    ref = next(p["ref_id"] for _, k, p in jobs(owner_engine) if p["purpose"] == "verify_email"
               and _ref_user(owner_engine, p["ref_id"]) == user_id)  # fmt: skip
    token = send_link(worker_engine, AccountEmailPurpose.VERIFY_EMAIL, ref)
    assert users.verify_email(hash_secret(token), datetime.now(UTC))
    return user_id, f"user:{user_id}"


def _ref_user(owner_engine: Engine, ref_id: str) -> UUID:
    with owner_engine.begin() as connection:
        connection.execute(text("ALTER TABLE email_verifications NO FORCE ROW LEVEL SECURITY"))
        user_id = connection.execute(
            text("SELECT user_id FROM email_verifications WHERE id = :id"), {"id": ref_id}
        ).scalar_one()
        connection.execute(text("ALTER TABLE email_verifications FORCE ROW LEVEL SECURITY"))
    return UUID(str(user_id))


def admin_of(tenant_id: UUID, subject: str) -> TenantContext:
    return TenantContext(tenant_id, subject, frozenset({Role.ADMINISTRATOR, Role.APPROVER}))


def test_sign_up_verify_and_single_use_links(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    users = SqlUserStore(runtime_engine)
    user_id = uuid4()
    assert users.sign_up(user_id, "Ana@Shop.example", "ana@shop.example", "Ana", HASH,
                         notify_existing=lambda: True)  # fmt: skip
    [(tenant, kind, payload)] = jobs(owner_engine)
    assert tenant is None and kind == JobKind.SEND_ACCOUNT_EMAIL
    assert set(payload) == {"purpose", "ref_id"}  # references only, never address or token
    token = send_link(worker_engine, AccountEmailPurpose.VERIFY_EMAIL, payload["ref_id"])
    assert users.by_subject(f"user:{user_id}").email_verified is False  # type: ignore[union-attr]
    later = datetime.now(UTC) + timedelta(hours=2)
    assert not users.verify_email(hash_secret(token), later)  # expired
    assert users.verify_email(hash_secret(token), datetime.now(UTC))
    assert not users.verify_email(hash_secret(token), datetime.now(UTC))  # used
    assert users.by_subject(f"user:{user_id}").email_verified  # type: ignore[union-attr]
    # The same address again: nothing changes, the owner gets a notice instead.
    assert not users.sign_up(uuid4(), "ANA@shop.example", "ana@shop.example", None, HASH,
                             notify_existing=lambda: True)  # fmt: skip
    assert [p["purpose"] for _, _, p in jobs(owner_engine)] == ["verify_email", "account_exists"]
    found = users.find_for_login("ana@shop.example")
    assert found is not None and found.password_hash == HASH


def test_people_and_passwords_are_hidden_from_everyone_else(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    with runtime_engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM user_credentials")).scalar_one() == 0
    for statement in ("SELECT * FROM user_credentials", "SELECT * FROM users"):
        with pytest.raises(ProgrammingError), worker_engine.begin() as connection:
            connection.execute(text(statement))


def test_password_reset_signs_out_everywhere(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    user_id, subject = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    sessions = SqlSessionStore(runtime_engine)
    expires = datetime.now(UTC) + timedelta(hours=1)
    for token in ("one", "two"):
        sessions.create(uuid4(), hash_secret(token), VerifiedIdentity(subject, "password"), expires)
    sessions.create(uuid4(), hash_secret("other"), VerifiedIdentity("user:x", "password"), expires)
    users = SqlUserStore(runtime_engine)
    users.request_password_reset("ana@shop.example")
    users.request_password_reset("nobody@shop.example")  # nothing happens, no error
    ref = [p for _, _, p in jobs(owner_engine) if p["purpose"] == "password_reset"][-1]["ref_id"]
    token = send_link(worker_engine, AccountEmailPurpose.PASSWORD_RESET, ref)
    assert users.reset_email(hash_secret(token), datetime.now(UTC)) == "ana@shop.example"
    new_hash = HASH.replace("synthetic", "changed")
    assert users.reset_password(hash_secret(token), new_hash, datetime.now(UTC)) == subject
    assert users.reset_password(hash_secret(token), new_hash, datetime.now(UTC)) is None
    assert sessions.revoke_all_for(subject, keep_token_sha256=hash_secret("two")) == 1
    assert sessions.find(hash_secret("one")).revoked  # type: ignore[union-attr]
    assert not sessions.find(hash_secret("two")).revoked  # type: ignore[union-attr]
    assert not sessions.find(hash_secret("other")).revoked  # type: ignore[union-attr]
    assert users.password_hash(subject) == new_hash
    assert "password_changed" in [p["purpose"] for _, _, p in jobs(owner_engine)]


def test_create_business_makes_a_setting_up_business_for_the_caller(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    user_id, subject = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    businesses = SqlBusinessStore(runtime_engine)
    tenant_id = businesses.create(user_id, subject, NEW)
    [membership] = SqlMembershipReader(runtime_engine).tenants_for(subject)
    assert membership.tenant_id == tenant_id and membership.status is TenantStatus.SETTING_UP
    assert membership.roles == {Role.ADMINISTRATOR, Role.APPROVER}
    context = admin_of(tenant_id, subject)
    stored = businesses.pricing(context)
    assert (stored.settings.currency, stored.settings.tax_rate) == ("INR", Decimal("0.180000"))
    assert not stored.settings.complete  # the rest is "set later"
    with pytest.raises(PricingPolicyNotConfigured):
        SqlPricingPolicyReader(runtime_engine).get(context)
    with tenant_transaction(runtime_engine, tenant_id) as connection:
        actions = connection.execute(text("SELECT action FROM admin_events")).scalars().all()
    assert actions == ["business_created"]


def test_create_business_cannot_be_abused(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    user_id, subject = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    other_id, other = person(runtime_engine, worker_engine, owner_engine, "bo@shop.example")
    businesses = SqlBusinessStore(runtime_engine)
    with pytest.raises(NotAllowed):  # another person's ID
        businesses.create(other_id, subject, NEW)
    unverified = uuid4()
    SqlUserStore(runtime_engine).sign_up(unverified, "new@shop.example", "new@shop.example",
                                         None, HASH, notify_existing=lambda: False)  # fmt: skip
    with pytest.raises(NotAllowed):
        businesses.create(unverified, f"user:{unverified}", NEW)
    with pytest.raises(ValueError, match="time zone"):
        businesses.create(user_id, subject, NewBusiness("X", "IN", "Mars/Base", 7, None, None))
    with pytest.raises(ProgrammingError), runtime_engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id, name) VALUES (gen_random_uuid(), 'x')"))
    with pytest.raises(ProgrammingError), worker_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.subject', :s, true)"), {"s": subject})
        connection.execute(
            text("SELECT create_business(:u, 'x', 'IN', 'UTC', NULL, NULL, 7)"), {"u": user_id}
        )
    with owner_engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT p.proname, p.prosecdef, p.proconfig, r.rolname FROM pg_proc p "
                "JOIN pg_roles r ON r.oid = p.proowner WHERE p.proname IN ('create_business', "
                "'accept_invitation', 'change_member_roles', 'remove_member')"
            )
        ).all()
    assert len(rows) == 4
    for row in rows:
        assert row.prosecdef and row.rolname == "sales_owner"
        assert row.proconfig == ["search_path=pg_catalog, public"]
    # The caller's business context is restored after the call.
    with runtime_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.subject', :s, true)"), {"s": subject})
        connection.execute(text("SELECT set_config('app.tenant_id', 'keep-me', true)"))
        connection.execute(
            text("SELECT create_business(:u, 'Y', 'IN', 'UTC', NULL, NULL, 7)"), {"u": user_id}
        )
        kept = connection.execute(text("SELECT current_setting('app.tenant_id')")).scalar_one()
    assert kept == "keep-me"
    del other


def test_two_self_created_businesses_are_isolated(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    a_id, a = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    b_id, b = person(runtime_engine, worker_engine, owner_engine, "bo@shop.example")
    businesses = SqlBusinessStore(runtime_engine)
    tenant_a = businesses.create(a_id, a, NEW)
    tenant_b = businesses.create(b_id, b, replace_name(NEW, "Bo Supplies"))
    members = SqlMemberStore(runtime_engine)
    assert [m.subject for m in members.list(admin_of(tenant_a, a))] == [a]
    assert [m.subject for m in members.list(admin_of(tenant_b, b))] == [b]
    assert [t.tenant_id for t in SqlMembershipReader(runtime_engine).tenants_for(a)] == [tenant_a]
    # An administrator of A acting on B through the functions is refused.
    with pytest.raises(NotAllowed):
        members.remove(admin_of(tenant_b, a), b_id)
    with tenant_transaction(runtime_engine, tenant_a) as connection:
        for table in ("tenants", "tenant_profiles", "tenant_pricing_policies", "admin_events"):
            seen = set(
                connection.execute(text(f"SELECT DISTINCT {_id(table)} FROM {table}")).scalars()
            )
            assert seen == {tenant_a}, table
        assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 1


def _id(table: str) -> str:
    return "id" if table == "tenants" else "tenant_id"


def replace_name(business: NewBusiness, name: str) -> NewBusiness:
    return NewBusiness(name, *list(vars(business).values())[1:])


def test_invitations_join_only_the_invited_address_once(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    a_id, a = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    b_id, b = person(runtime_engine, worker_engine, owner_engine, "bo@shop.example")
    c_id, c = person(runtime_engine, worker_engine, owner_engine, "cy@shop.example")
    tenant = SqlBusinessStore(runtime_engine).create(a_id, a, NEW)
    invitations = SqlInvitationStore(runtime_engine)
    context = admin_of(tenant, a)
    roles = frozenset({Role.SALES_OPERATOR, Role.APPROVER})
    first = invitations.invite(context, "Bo@shop.example", "bo@shop.example", roles)
    second = invitations.invite(context, "Bo@shop.example", "bo@shop.example", roles)
    statuses = {i.invitation_id: i.revoked_at is None for i in invitations.list(context)}
    assert statuses == {first: False, second: True}  # re-inviting replaced the older one
    tenant_jobs = [(t, p) for t, _, p in jobs(owner_engine) if p["purpose"] == "invitation"]
    assert all(t == tenant for t, _ in tenant_jobs)
    token = send_link(worker_engine, AccountEmailPurpose.INVITATION, str(second), tenant)
    preview = invitations.preview(hash_secret(token), datetime.now(UTC))
    assert preview is not None and preview.business_name == "Acme Traders"
    with pytest.raises(InvitationNotUsable):
        invitations.accept(hash_secret(token), c_id, c)  # another address
    with pytest.raises(InvitationNotUsable):
        invitations.accept(hash_secret(token), b_id, c)  # someone else's ID
    assert invitations.accept(hash_secret(token), b_id, b) == tenant
    with pytest.raises(InvitationNotUsable):
        invitations.accept(hash_secret(token), b_id, b)  # single use
    joined = {m.subject: m.roles for m in SqlMemberStore(runtime_engine).list(context)}
    assert joined[b] == roles
    with pytest.raises(Exception, match="already a member"):
        invitations.invite(context, "bo@shop.example", "bo@shop.example", roles)
    with pytest.raises(ProgrammingError), runtime_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        connection.execute(
            text("INSERT INTO memberships (id, tenant_id, subject, role) "
                 "VALUES (gen_random_uuid(), :t, :s, 'administrator')"),
            {"t": tenant, "s": c},
        )  # fmt: skip


def test_last_administrator_and_concurrent_removal(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    a_id, a = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    b_id, b = person(runtime_engine, worker_engine, owner_engine, "bo@shop.example")
    tenant = SqlBusinessStore(runtime_engine).create(a_id, a, NEW)
    members = SqlMemberStore(runtime_engine)
    with pytest.raises(LastAdministrator):
        members.change_roles(admin_of(tenant, a), a_id, frozenset({Role.VIEWER}))
    with pytest.raises(LastAdministrator):
        members.remove(admin_of(tenant, a), a_id)
    invitations = SqlInvitationStore(runtime_engine)
    invitation = invitations.invite(admin_of(tenant, a), "bo@shop.example", "bo@shop.example",
                                    frozenset({Role.ADMINISTRATOR}))  # fmt: skip
    token = send_link(worker_engine, AccountEmailPurpose.INVITATION, str(invitation), tenant)
    invitations.accept(hash_secret(token), b_id, b)

    def remove(actor: str, target: UUID) -> str:
        try:
            members.remove(admin_of(tenant, actor), target)
        except (NotAllowed, LastAdministrator) as error:
            return type(error).__name__
        return "removed"

    with ThreadPoolExecutor(2) as pool:
        outcomes = sorted(pool.map(remove, [a, b], [b_id, a_id]))
    assert outcomes[1] == "removed" and outcomes[0] in {"NotAllowed", "LastAdministrator"}
    remaining = members.list(admin_of(tenant, a))  # the store reads by business, not by person
    assert [Role.ADMINISTRATOR in m.roles for m in remaining] == [True]
    with tenant_transaction(owner_engine, tenant) as connection, pytest.raises(DBAPIError):
        connection.execute(text("DELETE FROM admin_events"))  # append-only, even for the owner


def test_go_live_needs_the_checklist_and_status_rules_hold(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    a_id, a = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    tenant = SqlBusinessStore(runtime_engine).create(a_id, a, NEW)
    context = admin_of(tenant, a)
    businesses = SqlBusinessStore(runtime_engine)
    missing = businesses.go_live(context)
    assert missing == [
        SetupItemKey.PDF_DETAILS,
        SetupItemKey.PRICING,
        SetupItemKey.CUSTOMER_CHANNEL,
        SetupItemKey.TEST_ENQUIRY,
    ]
    from sales_platform.accounts.ports.accounts import ProfileUpdate

    version = businesses.settings(context).profile.version
    businesses.update_profile(context, ProfileUpdate("Acme Pvt Ltd", "1 Road", None, None), version)
    pricing = PricingSettings("INR", 2, "half_up", Decimal("0.18"), 900, 300, 7)
    businesses.update_pricing(context, pricing, businesses.pricing(context).version)
    key = generate_api_key()
    SqlApiKeyAdminStore(runtime_engine).create(context, key.key_id, key.secret_sha256, "Site",
                                              Role.INTAKE_SERVICE)  # fmt: skip
    assert businesses.go_live(context) == [SetupItemKey.TEST_ENQUIRY]
    with tenant_transaction(owner_engine, tenant) as connection:
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, connection_ref, "
                "provider_event_id, payload_sha256, envelope, received_at, correlation_id) "
                "SELECT gen_random_uuid(), :t, 'api', 'k', id, 'e1', :h, '{}', now(), "
                "gen_random_uuid() FROM connections WHERE provider_key = 'api'"
            ),
            {"t": tenant, "h": "0" * 64},
        )
    assert businesses.go_live(context) == []
    assert businesses.status(tenant) is TenantStatus.ACTIVE
    assert businesses.go_live(context) == []  # already live: same answer
    for status in ("setting_up", "suspended"):
        with pytest.raises(DBAPIError, match="not allowed"), \
                tenant_transaction(runtime_engine, tenant) as connection:  # fmt: skip
            connection.execute(text("UPDATE tenants SET status = :s"), {"s": status})
    with tenant_transaction(owner_engine, tenant) as connection:  # the operator may
        connection.execute(text("UPDATE tenants SET status = 'suspended'"))
    assert businesses.status(tenant) is TenantStatus.SUSPENDED


def test_portal_api_keys_stay_in_their_business(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    a_id, a = person(runtime_engine, worker_engine, owner_engine, "ana@shop.example")
    tenant = SqlBusinessStore(runtime_engine).create(a_id, a, NEW)
    keys = SqlApiKeyAdminStore(runtime_engine)
    key = generate_api_key()
    keys.create(admin_of(tenant, a), key.key_id, key.secret_sha256, "Site", Role.INTAKE_SERVICE)
    [summary] = keys.list(admin_of(tenant, a))
    assert summary.created_by == a and summary.revoked_at is None
    other = admin_of(UUID("11111111-1111-4111-8111-111111111111"), a)
    assert keys.list(other) == [] and not keys.revoke(other, key.key_id)
    assert keys.revoke(admin_of(tenant, a), key.key_id)
    with pytest.raises(ProgrammingError), runtime_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        connection.execute(text("UPDATE api_keys SET secret_sha256 = 'x'"))


def test_rate_limits_are_shared_and_windows_reset(runtime_engine: Engine) -> None:
    rule = RateRule("test_rule", 2, timedelta(minutes=10))
    limiter, again = SqlRateLimiter(runtime_engine), SqlRateLimiter(runtime_engine)
    now = datetime.now(UTC)
    assert limiter.hit(rule, "203.0.113.7", now).allowed
    assert again.hit(rule, "203.0.113.7", now).allowed  # another process, same count
    blocked = limiter.hit(rule, "203.0.113.7", now + timedelta(minutes=1))
    assert not blocked.allowed and blocked.retry_after == timedelta(minutes=9)
    assert limiter.hit(rule, "203.0.113.8", now).allowed  # another key
    assert limiter.hit(rule, "203.0.113.7", now + timedelta(minutes=11)).allowed
    with runtime_engine.begin() as connection:
        keys = connection.execute(text("SELECT key_sha256 FROM rate_limit_counters")).scalars()
        assert all("203.0.113" not in key for key in keys)
