"""PostgreSQL storage for people, businesses, members, invitations and account emails (phase 2).

Global tables (users, credentials, email links) are reached through narrow policies keyed by
transaction-local settings: app.user_email (the address being signed in with), app.user_id (the
person a credential or link belongs to), app.subject (the signed-in person) and
app.account_token_sha256 (a presented link). Membership changes call the database's own
SECURITY DEFINER functions; this module never inserts memberships or tenants itself.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from sales_platform.accounts.domain.accounts import (
    AccountEmailPurpose,
    Invitation,
    LastAdministrator,
    SetupItem,
    SetupItemKey,
    User,
    missing_for_go_live,
    user_subject,
)
from sales_platform.accounts.persistence.tables import (
    AccountNotice,
    AdminEvent,
    EmailVerification,
    InvitationRow,
    PasswordReset,
    TenantPricingPolicy,
    TenantProfile,
    UserCredential,
)
from sales_platform.accounts.persistence.tables import User as UserRow
from sales_platform.accounts.ports.accounts import (
    AccountEmailWork,
    AlreadyMember,
    BusinessProfile,
    BusinessSettings,
    DefaultSource,
    InvitationNotUsable,
    InvitationPreview,
    LoginRecord,
    Member,
    NewBusiness,
    NotAllowed,
    PricingSettings,
    ProfileUpdate,
    StoredPricing,
    VersionConflict,
)
from sales_platform.integrations.domain.connections import ConnectionStatus, Purpose
from sales_platform.integrations.persistence.tables import ConnectionRow, InboxEvent
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.domain.tenancy import Role, TenantContext, TenantStatus
from sales_platform.platform.persistence.jobs import queue_job
from sales_platform.platform.persistence.models import Membership, Tenant
from sales_platform.platform.persistence.tenancy import tenant_transaction

# SQLSTATEs raised by the 0010 functions and the tenant status trigger.
_NOT_ALLOWED = "42501"
_INVALID_VALUE = "22023"
_INVITATION_NOT_USABLE = "QA001"
_LAST_ADMINISTRATOR = "QA002"


def set_settings(connection: Connection, **values: str) -> None:
    """Transaction-local app.<name> settings read by the RLS policies."""
    for name, value in values.items():
        connection.execute(
            text("SELECT set_config(:name, :value, true)"), {"name": f"app.{name}", "value": value}
        )


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _raise_mapped(error: DBAPIError) -> NoReturn:
    """Turn the database functions' error codes into the ports' exceptions."""
    state = _sqlstate(error)
    if state == _NOT_ALLOWED:
        raise NotAllowed("Not allowed") from None
    if state == _INVITATION_NOT_USABLE:
        raise InvitationNotUsable("This invitation cannot be used") from None
    if state == _LAST_ADMINISTRATOR:
        raise LastAdministrator("A business needs at least one administrator") from None
    if state == _INVALID_VALUE:
        diagnostic = getattr(error.orig, "diag", None)
        message = getattr(diagnostic, "message_primary", None) or "Invalid value"
        raise ValueError(message) from None
    raise error


def audit(
    connection: Connection,
    context: TenantContext,
    action: str,
    target: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    connection.execute(
        insert(AdminEvent).values(
            id=uuid4(),
            tenant_id=context.tenant_id,
            actor_subject=context.subject,
            action=action,
            target=target,
            details=details or {},
        )
    )


def _queue_email(
    connection: Connection, purpose: AccountEmailPurpose, ref_id: UUID, tenant_id: UUID | None
) -> None:
    queue_job(
        connection,
        tenant_id,
        JobKind.SEND_ACCOUNT_EMAIL,
        {"purpose": purpose.value, "ref_id": str(ref_id)},
        uuid4(),
    )


def _user(row: object) -> User:
    return User(
        user_id=row.id,  # type: ignore[attr-defined]
        subject=row.subject,  # type: ignore[attr-defined]
        email=row.email,  # type: ignore[attr-defined]
        display_name=row.display_name,  # type: ignore[attr-defined]
        email_verified=row.email_verified_at is not None,  # type: ignore[attr-defined]
        disabled=row.disabled_at is not None,  # type: ignore[attr-defined]
    )


_USER_COLUMNS = (
    UserRow.id,
    UserRow.subject,
    UserRow.email,
    UserRow.email_normalized,
    UserRow.display_name,
    UserRow.email_verified_at,
    UserRow.disabled_at,
)


def _queue_notice(
    connection: Connection, user_id: UUID, email_normalized: str, purpose: AccountEmailPurpose
) -> None:
    set_settings(connection, user_id=str(user_id))
    notice_id = uuid4()
    connection.execute(
        insert(AccountNotice).values(
            id=notice_id, user_id=user_id, email_normalized=email_normalized, purpose=purpose.value
        )
    )
    _queue_email(connection, purpose, notice_id, None)


class SqlUserStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
        with self._engine.begin() as connection:
            set_settings(connection, user_email=email_normalized)
            existing = connection.execute(
                select(UserRow.id).where(UserRow.email_normalized == email_normalized)
            ).scalar_one_or_none()
            if existing is not None:
                if notify_existing():
                    _queue_notice(
                        connection, existing, email_normalized, AccountEmailPurpose.ACCOUNT_EXISTS
                    )
                return False
            set_settings(connection, user_id=str(user_id))
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(UserRow).values(
                            id=user_id,
                            subject=user_subject(user_id),
                            email=email,
                            email_normalized=email_normalized,
                            display_name=display_name,
                        )
                    )
            except IntegrityError:
                return False  # the same address signed up a moment ago
            connection.execute(
                insert(UserCredential).values(user_id=user_id, password_hash=password_hash)
            )
            verification_id = uuid4()
            connection.execute(
                insert(EmailVerification).values(
                    id=verification_id, user_id=user_id, email_normalized=email_normalized
                )
            )
            _queue_email(connection, AccountEmailPurpose.VERIFY_EMAIL, verification_id, None)
        return True

    def find_for_login(self, email_normalized: str) -> LoginRecord | None:
        with self._engine.begin() as connection:
            set_settings(connection, user_email=email_normalized)
            row = connection.execute(
                select(*_USER_COLUMNS).where(UserRow.email_normalized == email_normalized)
            ).one_or_none()
            if row is None:
                return None
            set_settings(connection, user_id=str(row.id))
            password_hash = connection.execute(
                select(UserCredential.password_hash).where(UserCredential.user_id == row.id)
            ).scalar_one_or_none()
        return LoginRecord(_user(row), password_hash)

    def password_hash(self, subject: str) -> str | None:
        with self._engine.begin() as connection:
            user_id = self._id_for(connection, subject)
            if user_id is None:
                return None
            set_settings(connection, user_id=str(user_id))
            return connection.execute(
                select(UserCredential.password_hash).where(UserCredential.user_id == user_id)
            ).scalar_one_or_none()

    def store_rehash(self, user_id: UUID, password_hash: str) -> None:
        with self._engine.begin() as connection:
            set_settings(connection, user_id=str(user_id))
            connection.execute(
                update(UserCredential)
                .where(UserCredential.user_id == user_id)
                .values(password_hash=password_hash)
            )

    def by_subject(self, subject: str) -> User | None:
        with self._engine.begin() as connection:
            set_settings(connection, subject=subject)
            row = connection.execute(
                select(*_USER_COLUMNS).where(UserRow.subject == subject)
            ).one_or_none()
        return None if row is None else _user(row)

    def update_profile(self, subject: str, display_name: str | None) -> User:
        with self._engine.begin() as connection:
            set_settings(connection, subject=subject)
            row = connection.execute(
                update(UserRow)
                .where(UserRow.subject == subject)
                .values(display_name=display_name, updated_at=func.now())
                .returning(*_USER_COLUMNS)
            ).one()
        return _user(row)

    def verify_email(self, token_sha256: str, now: datetime) -> bool:
        with self._engine.begin() as connection:
            set_settings(connection, account_token_sha256=token_sha256)
            row = connection.execute(
                update(EmailVerification)
                .where(
                    EmailVerification.token_sha256 == token_sha256,
                    EmailVerification.consumed_at.is_(None),
                    EmailVerification.revoked_at.is_(None),
                    EmailVerification.expires_at > now,
                )
                .values(consumed_at=now)
                .returning(EmailVerification.user_id, EmailVerification.email_normalized)
            ).one_or_none()
            if row is None:
                return False
            set_settings(connection, user_id=str(row.user_id))
            # Only the address the link was sent to becomes verified.
            connection.execute(
                update(UserRow)
                .where(UserRow.id == row.user_id, UserRow.email_normalized == row.email_normalized)
                .values(
                    email_verified_at=func.coalesce(UserRow.email_verified_at, now),
                    updated_at=func.now(),
                )
            )
        return True

    def request_verification(self, subject: str) -> bool:
        with self._engine.begin() as connection:
            set_settings(connection, subject=subject)
            row = connection.execute(
                select(*_USER_COLUMNS).where(UserRow.subject == subject)
            ).one_or_none()
            if row is None or row.email_verified_at is not None or row.email_normalized is None:
                return False
            self._replace_link(connection, EmailVerification, row.id, row.email_normalized)
            return True

    def request_password_reset(self, email_normalized: str) -> None:
        with self._engine.begin() as connection:
            set_settings(connection, user_email=email_normalized)
            row = connection.execute(
                select(UserRow.id, UserRow.disabled_at).where(
                    UserRow.email_normalized == email_normalized
                )
            ).one_or_none()
            if row is None or row.disabled_at is not None:
                return
            self._replace_link(connection, PasswordReset, row.id, email_normalized)

    def reset_email(self, token_sha256: str, now: datetime) -> str | None:
        with self._engine.begin() as connection:
            set_settings(connection, account_token_sha256=token_sha256)
            return connection.execute(
                select(PasswordReset.email_normalized).where(
                    PasswordReset.token_sha256 == token_sha256,
                    PasswordReset.consumed_at.is_(None),
                    PasswordReset.revoked_at.is_(None),
                    PasswordReset.expires_at > now,
                )
            ).scalar_one_or_none()

    def reset_password(self, token_sha256: str, password_hash: str, now: datetime) -> str | None:
        with self._engine.begin() as connection:
            set_settings(connection, account_token_sha256=token_sha256)
            row = connection.execute(
                update(PasswordReset)
                .where(
                    PasswordReset.token_sha256 == token_sha256,
                    PasswordReset.consumed_at.is_(None),
                    PasswordReset.revoked_at.is_(None),
                    PasswordReset.expires_at > now,
                )
                .values(consumed_at=now)
                .returning(PasswordReset.user_id)
            ).one_or_none()
            if row is None:
                return None
            set_settings(connection, user_id=str(row.user_id))
            user = connection.execute(
                select(UserRow.subject, UserRow.email_normalized, UserRow.disabled_at).where(
                    UserRow.id == row.user_id
                )
            ).one()
            if user.disabled_at is not None:
                return None
            self._set_password(connection, row.user_id, password_hash, now)
            _queue_notice(
                connection,
                row.user_id,
                user.email_normalized,
                AccountEmailPurpose.PASSWORD_CHANGED,
            )
            return str(user.subject)

    def change_password(self, subject: str, password_hash: str) -> None:
        with self._engine.begin() as connection:
            set_settings(connection, subject=subject)
            user = connection.execute(
                select(UserRow.id, UserRow.email_normalized).where(UserRow.subject == subject)
            ).one()
            set_settings(connection, user_id=str(user.id))
            self._set_password(connection, user.id, password_hash, None)
            if user.email_normalized is not None:
                _queue_notice(
                    connection, user.id, user.email_normalized, AccountEmailPurpose.PASSWORD_CHANGED
                )

    @staticmethod
    def _id_for(connection: Connection, subject: str) -> UUID | None:
        set_settings(connection, subject=subject)
        return connection.execute(
            select(UserRow.id).where(UserRow.subject == subject)
        ).scalar_one_or_none()

    @staticmethod
    def _set_password(
        connection: Connection, user_id: UUID, password_hash: str, now: datetime | None
    ) -> None:
        changed_at = func.now() if now is None else now
        updated = connection.execute(
            update(UserCredential)
            .where(UserCredential.user_id == user_id)
            .values(password_hash=password_hash, password_changed_at=changed_at)
        ).rowcount
        if updated == 0:  # a person without a password yet (reset creates it)
            connection.execute(
                insert(UserCredential).values(
                    user_id=user_id, password_hash=password_hash, password_changed_at=changed_at
                )
            )

    @staticmethod
    def _replace_link(
        connection: Connection,
        table: type[EmailVerification] | type[PasswordReset],
        user_id: UUID,
        email_normalized: str,
    ) -> None:
        """Older open links stop working; a new one is emailed."""
        set_settings(connection, user_id=str(user_id))
        connection.execute(
            update(table)
            .where(
                table.user_id == user_id, table.consumed_at.is_(None), table.revoked_at.is_(None)
            )
            .values(revoked_at=func.now())
        )
        link_id = uuid4()
        connection.execute(
            insert(table).values(id=link_id, user_id=user_id, email_normalized=email_normalized)
        )
        purpose = (
            AccountEmailPurpose.VERIFY_EMAIL
            if table is EmailVerification
            else AccountEmailPurpose.PASSWORD_RESET
        )
        _queue_email(connection, purpose, link_id, None)


_COMMERCIAL = (
    "currency",
    "minor_units",
    "rounding_mode",
    "tax_rate",
    "max_price_age_seconds",
    "max_stock_age_seconds",
)


def _pricing(row: object) -> PricingSettings:
    return PricingSettings(
        currency=row.currency,  # type: ignore[attr-defined]
        minor_units=row.minor_units,  # type: ignore[attr-defined]
        rounding_mode=row.rounding_mode,  # type: ignore[attr-defined]
        tax_rate=row.tax_rate,  # type: ignore[attr-defined]
        max_price_age_seconds=row.max_price_age_seconds,  # type: ignore[attr-defined]
        max_stock_age_seconds=row.max_stock_age_seconds,  # type: ignore[attr-defined]
        quote_validity_days=row.quote_validity_days,  # type: ignore[attr-defined]
    )


class SqlBusinessStore:
    def __init__(self, engine: Engine, default_catalogue: DefaultSource | None = None) -> None:
        """`default_catalogue`: the product source a new business gets (phase 3, C6)."""
        self._engine = engine
        self._default_catalogue = default_catalogue

    def create(self, user_id: UUID, subject: str, business: NewBusiness) -> UUID:
        source = self._default_catalogue
        try:
            with self._engine.begin() as connection:
                set_settings(connection, subject=subject)
                tenant_id = connection.execute(
                    text(
                        "SELECT create_business(:user_id, :name, :country, :time_zone, "
                        "CAST(:currency AS text), CAST(:tax_rate AS numeric), :validity, "
                        "CAST(:catalogue_provider AS text), CAST(:catalogue_name AS text))"
                    ),
                    {
                        "user_id": user_id,
                        "name": business.name,
                        "country": business.country,
                        "time_zone": business.time_zone,
                        "currency": business.currency,
                        "tax_rate": business.tax_rate,
                        "validity": business.quote_validity_days,
                        "catalogue_provider": source.provider_key if source else None,
                        "catalogue_name": source.display_name if source else None,
                    },
                ).scalar_one()
        except DBAPIError as error:
            _raise_mapped(error)
        return UUID(str(tenant_id))

    def status(self, tenant_id: UUID) -> TenantStatus:
        with tenant_transaction(self._engine, tenant_id) as connection:
            value = connection.execute(
                select(Tenant.status).where(Tenant.id == tenant_id)
            ).scalar_one()
        return TenantStatus(value)

    def settings(self, context: TenantContext) -> BusinessSettings:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            row = connection.execute(
                select(
                    Tenant.name,
                    Tenant.country,
                    Tenant.time_zone,
                    Tenant.status,
                    Tenant.went_live_at,
                    TenantProfile.legal_name,
                    TenantProfile.address,
                    TenantProfile.tax_id_label,
                    TenantProfile.tax_id,
                    TenantProfile.logo_sha256,
                    TenantProfile.version,
                )
                .join(TenantProfile, TenantProfile.tenant_id == Tenant.id)
                .where(Tenant.id == context.tenant_id)
            ).one()
        return BusinessSettings(
            tenant_id=context.tenant_id,
            name=row.name,
            country=row.country,
            time_zone=row.time_zone,
            status=TenantStatus(row.status),
            went_live_at=row.went_live_at,
            profile=BusinessProfile(
                legal_name=row.legal_name,
                address=row.address,
                tax_id_label=row.tax_id_label,
                tax_id=row.tax_id,
                has_logo=row.logo_sha256 is not None,
                version=row.version,
            ),
        )

    def update_details(
        self, context: TenantContext, name: str, country: str, time_zone: str
    ) -> None:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            known = connection.execute(
                text("SELECT 1 FROM pg_timezone_names WHERE name = :name"), {"name": time_zone}
            ).first()
            if known is None:
                raise ValueError("Unknown time zone")
            old = connection.execute(
                select(Tenant.name, Tenant.country, Tenant.time_zone).where(
                    Tenant.id == context.tenant_id
                )
            ).one()
            connection.execute(
                update(Tenant)
                .where(Tenant.id == context.tenant_id)
                .values(name=name, country=country, time_zone=time_zone, updated_at=func.now())
            )
            audit(
                connection,
                context,
                "details_changed",
                details={
                    "old": {"name": old.name, "country": old.country, "time_zone": old.time_zone},
                    "new": {"name": name, "country": country, "time_zone": time_zone},
                },
            )

    def update_profile(
        self, context: TenantContext, profile: ProfileUpdate, expected_version: int
    ) -> int:
        values = {
            "legal_name": profile.legal_name,
            "address": profile.address,
            "tax_id_label": profile.tax_id_label,
            "tax_id": profile.tax_id,
        }
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            version = connection.execute(
                update(TenantProfile)
                .where(
                    TenantProfile.tenant_id == context.tenant_id,
                    TenantProfile.version == expected_version,
                )
                .values(
                    **values,
                    version=TenantProfile.version + 1,
                    updated_by=context.subject,
                    updated_at=func.now(),
                )
                .returning(TenantProfile.version)
            ).scalar_one_or_none()
            if version is None:
                raise VersionConflict("The PDF details were changed by someone else")
            audit(connection, context, "profile_changed", details={"new": values})
        return int(version)

    def logo_key(self, context: TenantContext) -> tuple[str, str] | None:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            row = connection.execute(
                select(TenantProfile.logo_storage_key, TenantProfile.logo_sha256).where(
                    TenantProfile.tenant_id == context.tenant_id
                )
            ).one()
        if row.logo_storage_key is None:
            return None
        return row.logo_storage_key, row.logo_sha256

    def set_logo(self, context: TenantContext, storage_key: str | None, sha256: str | None) -> None:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            connection.execute(
                update(TenantProfile)
                .where(TenantProfile.tenant_id == context.tenant_id)
                .values(
                    logo_storage_key=storage_key,
                    logo_sha256=sha256,
                    version=TenantProfile.version + 1,
                    updated_by=context.subject,
                    updated_at=func.now(),
                )
            )
            audit(connection, context, "logo_changed", details={"sha256": sha256})

    def pricing(self, context: TenantContext) -> StoredPricing:
        policy = TenantPricingPolicy
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            row = connection.execute(
                select(policy.__table__).where(policy.tenant_id == context.tenant_id)
            ).one()
            return StoredPricing(_pricing(row), row.version)

    def update_pricing(
        self, context: TenantContext, pricing: PricingSettings, expected_version: int
    ) -> int:
        policy = TenantPricingPolicy
        new = {name: getattr(pricing, name) for name in _COMMERCIAL}
        new["quote_validity_days"] = pricing.quote_validity_days
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            old = connection.execute(
                select(policy.__table__).where(policy.tenant_id == context.tenant_id)
            ).one()
            old_values = {name: getattr(old, name) for name in new}
            version = connection.execute(
                update(policy)
                .where(policy.tenant_id == context.tenant_id, policy.version == expected_version)
                .values(
                    **new,
                    version=policy.version + 1,
                    updated_by=context.subject,
                    updated_at=func.now(),
                )
                .returning(policy.version)
            ).scalar_one_or_none()
            if version is None:
                raise VersionConflict("The pricing settings were changed by someone else")
            audit(
                connection,
                context,
                "pricing_changed",
                details={"old": _jsonable(old_values), "new": _jsonable(new)},
            )
        return int(version)

    def setup_state(self, context: TenantContext) -> list[SetupItem]:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            return self._setup_state(connection, context.tenant_id)

    def go_live(self, context: TenantContext) -> list[SetupItemKey]:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            status = connection.execute(
                select(Tenant.status).where(Tenant.id == context.tenant_id).with_for_update()
            ).scalar_one()
            if status == TenantStatus.ACTIVE:
                return []
            if status != TenantStatus.SETTING_UP:
                raise NotAllowed("This business cannot go live")
            missing = missing_for_go_live(self._setup_state(connection, context.tenant_id))
            if missing:
                return missing
            connection.execute(
                update(Tenant)
                .where(Tenant.id == context.tenant_id)
                .values(
                    status=TenantStatus.ACTIVE.value,
                    went_live_at=func.now(),
                    updated_at=func.now(),
                )
            )
            audit(connection, context, "went_live")
        return []

    @staticmethod
    def _setup_state(connection: Connection, tenant_id: UUID) -> list[SetupItem]:
        tenant = connection.execute(
            select(Tenant.country, Tenant.time_zone).where(Tenant.id == tenant_id)
        ).one()
        profile = connection.execute(
            select(TenantProfile.legal_name, TenantProfile.address).where(
                TenantProfile.tenant_id == tenant_id
            )
        ).one()
        pricing = connection.execute(
            select(TenantPricingPolicy.__table__).where(TenantPricingPolicy.tenant_id == tenant_id)
        ).one_or_none()
        approver = connection.execute(
            select(Membership.id)
            .where(Membership.tenant_id == tenant_id, Membership.role == Role.APPROVER.value)
            .limit(1)
        ).first()
        active = ConnectionRow.status == ConnectionStatus.ACTIVE.value
        source = connection.execute(
            select(ConnectionRow.id)
            .where(
                ConnectionRow.tenant_id == tenant_id,
                active,
                ConnectionRow.purposes.contains([Purpose.CATALOGUE.value]),
            )
            .limit(1)
        ).first()
        customer = list(
            connection.execute(
                select(ConnectionRow.id).where(
                    ConnectionRow.tenant_id == tenant_id,
                    active,
                    ConnectionRow.purposes.contains([Purpose.CUSTOMER_MESSAGES.value]),
                )
            ).scalars()
        )
        tested = set(
            connection.execute(
                select(InboxEvent.connection_ref)
                .where(InboxEvent.tenant_id == tenant_id, InboxEvent.connection_ref.in_(customer))
                .distinct()
            ).scalars()
        )
        done = {
            SetupItemKey.BUSINESS_DETAILS: tenant.country is not None
            and tenant.time_zone is not None,
            SetupItemKey.PDF_DETAILS: bool(profile.legal_name) and bool(profile.address),
            SetupItemKey.PRICING: pricing is not None and _pricing(pricing).complete,
            SetupItemKey.APPROVER: approver is not None,
            SetupItemKey.PRODUCT_SOURCE: source is not None,
            SetupItemKey.CUSTOMER_CHANNEL: bool(customer),
            # Every active customer tool has received at least one enquiry.
            SetupItemKey.TEST_ENQUIRY: bool(customer) and tested == set(customer),
        }
        return [SetupItem(key, done[key]) for key in SetupItemKey]


def _jsonable(values: dict[str, object]) -> dict[str, object]:
    return {name: (None if value is None else str(value)) for name, value in values.items()}


@contextmanager
def _as_person(engine: Engine, context: TenantContext) -> Iterator[Connection]:
    """Tenant transaction that also names the person, for the database functions."""
    with tenant_transaction(engine, context.tenant_id) as connection:
        set_settings(connection, subject=context.subject)
        yield connection


class SqlMemberStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list(self, context: TenantContext) -> list[Member]:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            rows = connection.execute(
                select(
                    UserRow.id,
                    UserRow.subject,
                    UserRow.email,
                    UserRow.display_name,
                    Membership.role,
                )
                .join(Membership, Membership.subject == UserRow.subject)
                .where(Membership.tenant_id == context.tenant_id)
                .order_by(UserRow.email, UserRow.id)
            ).all()
        members: dict[UUID, Member] = {}
        for row in rows:
            known = members.get(row.id)
            roles = (known.roles if known else frozenset()) | {Role(row.role)}
            members[row.id] = Member(row.id, row.subject, row.email, row.display_name, roles)
        return list(members.values())

    def change_roles(self, context: TenantContext, user_id: UUID, roles: frozenset[Role]) -> None:
        self._call(
            context,
            "SELECT change_member_roles(:tenant, :user_id, CAST(:roles AS text[]))",
            {"roles": sorted(role.value for role in roles)},
            user_id,
        )

    def remove(self, context: TenantContext, user_id: UUID) -> None:
        self._call(context, "SELECT remove_member(:tenant, :user_id)", {}, user_id)

    def _call(
        self, context: TenantContext, sql: str, extra: dict[str, object], user_id: UUID
    ) -> None:
        try:
            with _as_person(self._engine, context) as connection:
                connection.execute(
                    text(sql), {"tenant": context.tenant_id, "user_id": user_id, **extra}
                )
        except DBAPIError as error:
            _raise_mapped(error)


def _invitation(row: object) -> Invitation:
    return Invitation(
        invitation_id=row.id,  # type: ignore[attr-defined]
        tenant_id=row.tenant_id,  # type: ignore[attr-defined]
        email=row.email,  # type: ignore[attr-defined]
        roles=frozenset(Role(role) for role in row.roles),  # type: ignore[attr-defined]
        invited_by=row.invited_by,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        expires_at=row.expires_at,  # type: ignore[attr-defined]
        accepted_at=row.accepted_at,  # type: ignore[attr-defined]
        revoked_at=row.revoked_at,  # type: ignore[attr-defined]
        email_status=row.email_status,  # type: ignore[attr-defined]
    )


_OPEN = (InvitationRow.accepted_at.is_(None), InvitationRow.revoked_at.is_(None))


class SqlInvitationStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def invite(
        self,
        context: TenantContext,
        email: str,
        email_normalized: str,
        roles: frozenset[Role],
    ) -> UUID:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            member = connection.execute(
                select(UserRow.id)
                .join(Membership, Membership.subject == UserRow.subject)
                .where(
                    Membership.tenant_id == context.tenant_id,
                    UserRow.email_normalized == email_normalized,
                )
                .limit(1)
            ).first()
            if member is not None:
                raise AlreadyMember("This person is already a member")
            older = connection.execute(
                update(InvitationRow)
                .where(
                    InvitationRow.tenant_id == context.tenant_id,
                    InvitationRow.email_normalized == email_normalized,
                    *_OPEN,
                )
                .values(revoked_at=func.now(), revoked_by=context.subject)
                .returning(InvitationRow.id)
            ).scalar_one_or_none()
            if older is not None:
                audit(connection, context, "invitation_revoked", str(older), {"replaced": True})
            invitation_id = uuid4()
            connection.execute(
                insert(InvitationRow).values(
                    id=invitation_id,
                    tenant_id=context.tenant_id,
                    email=email,
                    email_normalized=email_normalized,
                    roles=sorted(role.value for role in roles),
                    invited_by=context.subject,
                )
            )
            _queue_email(
                connection, AccountEmailPurpose.INVITATION, invitation_id, context.tenant_id
            )
            audit(
                connection,
                context,
                "member_invited",
                str(invitation_id),
                {"email": email_normalized, "roles": sorted(role.value for role in roles)},
            )
        return invitation_id

    def list(self, context: TenantContext) -> list[Invitation]:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            rows = connection.execute(
                select(InvitationRow.__table__)
                .where(InvitationRow.tenant_id == context.tenant_id)
                .order_by(InvitationRow.created_at.desc())
                .limit(200)
            ).all()
            return [_invitation(row) for row in rows]

    def resend(self, context: TenantContext, invitation_id: UUID) -> bool:
        """A fresh link: the old one stops working at once."""
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            found = connection.execute(
                update(InvitationRow)
                .where(
                    InvitationRow.tenant_id == context.tenant_id,
                    InvitationRow.id == invitation_id,
                    *_OPEN,
                )
                .values(email_status="pending", token_sha256=None, expires_at=None)
                .returning(InvitationRow.id)
            ).scalar_one_or_none()
            if found is None:
                return False
            _queue_email(
                connection, AccountEmailPurpose.INVITATION, invitation_id, context.tenant_id
            )
            audit(connection, context, "invitation_resent", str(invitation_id))
        return True

    def revoke(self, context: TenantContext, invitation_id: UUID) -> bool:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            found = connection.execute(
                update(InvitationRow)
                .where(
                    InvitationRow.tenant_id == context.tenant_id,
                    InvitationRow.id == invitation_id,
                    *_OPEN,
                )
                .values(revoked_at=func.now(), revoked_by=context.subject)
                .returning(InvitationRow.id)
            ).scalar_one_or_none()
            if found is None:
                return False
            audit(connection, context, "invitation_revoked", str(invitation_id))
        return True

    def preview(self, token_sha256: str, now: datetime) -> InvitationPreview | None:
        with self._engine.begin() as connection:
            set_settings(connection, account_token_sha256=token_sha256)
            row = connection.execute(
                select(InvitationRow.__table__).where(InvitationRow.token_sha256 == token_sha256)
            ).one_or_none()
            if row is None:
                return None
            invitation = _invitation(row)
            if invitation.status(now).value != "pending" or invitation.expires_at is None:
                return None
            # The presented link names this business; read its name with its context.
            set_settings(connection, tenant_id=str(row.tenant_id))
            name = connection.execute(
                select(Tenant.name).where(Tenant.id == row.tenant_id)
            ).scalar_one()
        return InvitationPreview(
            name, invitation.roles, row.email_normalized, invitation.expires_at
        )

    def accept(self, token_sha256: str, user_id: UUID, subject: str) -> UUID:
        try:
            with self._engine.begin() as connection:
                set_settings(connection, subject=subject)
                tenant_id = connection.execute(
                    text("SELECT accept_invitation(:token, :user_id)"),
                    {"token": token_sha256, "user_id": user_id},
                ).scalar_one()
        except DBAPIError as error:
            _raise_mapped(error)
        return UUID(str(tenant_id))


_LINK_TABLES: dict[AccountEmailPurpose, type[EmailVerification] | type[PasswordReset]] = {
    AccountEmailPurpose.VERIFY_EMAIL: EmailVerification,
    AccountEmailPurpose.PASSWORD_RESET: PasswordReset,
}


class SqlAccountEmailStore:
    """Worker side of send_account_email. Each row is found only by its own ID."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _for(self, ref_id: UUID, tenant_id: UUID | None) -> Iterator[Connection]:
        with self._engine.begin() as connection:
            set_settings(connection, account_ref_id=str(ref_id))
            if tenant_id is not None:
                set_settings(connection, tenant_id=str(tenant_id))
            yield connection

    def prepare(
        self,
        purpose: AccountEmailPurpose,
        ref_id: UUID,
        tenant_id: UUID | None,
        token_sha256: str | None,
        expires_at: datetime | None,
    ) -> AccountEmailWork | None:
        with self._for(ref_id, tenant_id) as connection:
            if purpose is AccountEmailPurpose.INVITATION:
                if tenant_id is None:
                    return None
                row = connection.execute(
                    update(InvitationRow)
                    .where(
                        InvitationRow.tenant_id == tenant_id,
                        InvitationRow.id == ref_id,
                        InvitationRow.email_status == "pending",
                        *_OPEN,
                    )
                    .values(
                        email_status="sending", token_sha256=token_sha256, expires_at=expires_at
                    )
                    .returning(InvitationRow.email_normalized, InvitationRow.roles)
                ).one_or_none()
                if row is None:
                    return None
                name = connection.execute(
                    select(Tenant.name).where(Tenant.id == tenant_id)
                ).scalar_one()
                return AccountEmailWork(
                    row.email_normalized, name, frozenset(Role(role) for role in row.roles)
                )
            if purpose in _LINK_TABLES:
                table = _LINK_TABLES[purpose]
                email = connection.execute(
                    update(table)
                    .where(
                        table.id == ref_id,
                        table.email_status == "pending",
                        table.consumed_at.is_(None),
                        table.revoked_at.is_(None),
                    )
                    .values(
                        email_status="sending", token_sha256=token_sha256, expires_at=expires_at
                    )
                    .returning(table.email_normalized)
                ).scalar_one_or_none()
            else:
                email = connection.execute(
                    update(AccountNotice)
                    .where(AccountNotice.id == ref_id, AccountNotice.email_status == "pending")
                    .values(email_status="sending")
                    .returning(AccountNotice.email_normalized)
                ).scalar_one_or_none()
        return None if email is None else AccountEmailWork(email, None, frozenset())

    def finish(
        self,
        purpose: AccountEmailPurpose,
        ref_id: UUID,
        tenant_id: UUID | None,
        status: Literal["sent", "failed", "unknown"],
    ) -> None:
        self._from_sending(purpose, ref_id, tenant_id, status)

    def mark_unknown_if_sending(
        self, purpose: AccountEmailPurpose, ref_id: UUID, tenant_id: UUID | None
    ) -> bool:
        return self._from_sending(purpose, ref_id, tenant_id, "unknown")

    def _from_sending(
        self,
        purpose: AccountEmailPurpose,
        ref_id: UUID,
        tenant_id: UUID | None,
        status: str,
    ) -> bool:
        table = _table_for(purpose)
        values: dict[str, object] = {"email_status": status}
        if status == "sent":
            values["sent_at"] = func.now()
        with self._for(ref_id, tenant_id) as connection:
            result = connection.execute(
                update(table)
                .where(table.id == ref_id, table.email_status == "sending")
                .values(**values)
            )
        return result.rowcount == 1


def _table_for(
    purpose: AccountEmailPurpose,
) -> type[EmailVerification] | type[PasswordReset] | type[InvitationRow] | type[AccountNotice]:
    if purpose is AccountEmailPurpose.INVITATION:
        return InvitationRow
    return _LINK_TABLES.get(purpose, AccountNotice)
