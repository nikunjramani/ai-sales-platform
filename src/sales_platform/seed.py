"""Idempotently insert fictional tenants, people, memberships, policies and connections.

Runs with owner credentials, development/test only. Existing rows are left unchanged. Subjects
use the reserved .example domain. Each demo business gets one `simulated` connection (ADR 0018):
the fake manager phone and its demo catalogue. Phase 2: the demo businesses are live, each demo
person is a verified user and, when SALES_DEMO_PASSWORD is set, can also sign in with that
password (user answer Q24); the dev login keeps working either way. Phase 3: a third, local
demo business keeps its products in our own catalogue (Q20), seeded with the same synthetic
industrial data, so the product screens can be tried without touching the other demos.
"""

import hashlib
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Connection, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.accounts.adapters.accounts.password import Argon2PasswordHasher
from sales_platform.accounts.persistence.tables import (
    TenantPricingPolicy,
    TenantProfile,
    User,
    UserCredential,
)
from sales_platform.catalogue.adapters.erp.simulated import SimulatedCatalogue, demo_catalogue
from sales_platform.catalogue.domain.catalogue_admin import (
    DEFAULT_LOCATION,
    LOCAL_PRICE_LIST,
    ProductFields,
    fields_as_dict,
)
from sales_platform.catalogue.domain.catalogue_search import product_terms
from sales_platform.catalogue.persistence.tables import (
    CatalogueCategory,
    CataloguePriceRow,
    CatalogueProductRow,
    CatalogueStockRow,
)
from sales_platform.composition import build_secret_store
from sales_platform.integrations.domain.connections import ConnectionStatus, Purpose
from sales_platform.integrations.persistence.connections import ensure_connection, record_event
from sales_platform.integrations.persistence.tables import ConnectionRow
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.money import RoundingMode
from sales_platform.platform.domain.tenancy import Role
from sales_platform.platform.persistence.database import build_engine
from sales_platform.platform.persistence.models import Membership, Tenant
from sales_platform.platform.ports.secrets import SecretNotFound, SecretStore
from sales_platform.providers.simulated.provider import SIGNING_SECRET
from sales_platform.providers.simulated.signing import hook_token

INDUSTRIAL_TENANT = UUID("11111111-1111-4111-8111-111111111111")
OFFICE_TENANT = UUID("22222222-2222-4222-8222-222222222222")
LOCAL_TENANT = UUID("33333333-3333-4333-8333-333333333333")

DEMO_TENANTS = (
    (INDUSTRIAL_TENANT, "Demo Industrial Supplies"),
    (OFFICE_TENANT, "Demo Office Equipment"),
    (LOCAL_TENANT, "Demo Local Catalogue"),
)

DEMO_MEMBERSHIPS = (
    (INDUSTRIAL_TENANT, "admin@industrial.example", Role.ADMINISTRATOR),
    (INDUSTRIAL_TENANT, "sales@industrial.example", Role.SALES_OPERATOR),
    (INDUSTRIAL_TENANT, "manager@industrial.example", Role.APPROVER),
    (INDUSTRIAL_TENANT, "viewer@industrial.example", Role.VIEWER),
    (OFFICE_TENANT, "sales@office.example", Role.SALES_OPERATOR),
    (LOCAL_TENANT, "admin@local.example", Role.ADMINISTRATOR),
    (LOCAL_TENANT, "sales@local.example", Role.SALES_OPERATOR),
    (LOCAL_TENANT, "manager@local.example", Role.APPROVER),
)

# Synthetic policies for learning; real tax/rounding rules come from each pilot business.
# Typed stock counts age for days, so the local demo allows 7 days (Q10); ERP demos 5 minutes.
DEMO_POLICIES = (
    (INDUSTRIAL_TENANT, "INR", 2, RoundingMode.HALF_UP, Decimal("0.18"), 300),
    (OFFICE_TENANT, "EUR", 2, RoundingMode.HALF_EVEN, Decimal("0.19"), 300),
    (LOCAL_TENANT, "INR", 2, RoundingMode.HALF_UP, Decimal("0.18"), 7 * 24 * 3600),
)


# The development provider (dev-only, refused in production) and its demo catalogue names.
SIMULATED_PROVIDER = "simulated"
SIMULATED_ACCOUNT = "simulated-local"
DEMO_CONNECTIONS = (
    (INDUSTRIAL_TENANT, "industrial"),
    (OFFICE_TENANT, "office"),
)
# The local demo: our own catalogue, plus the simulated phone for approvals only.
LOCAL_CATALOGUE_PROVIDER = "local_catalogue"
LOCAL_CATALOGUE_ACCOUNT = "local"
SEED_ACTOR = "seed"


def _set_tenant(connection: Connection, tenant_id: UUID) -> None:
    # FORCE RLS also applies to the owner. Context is transaction-local.
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def _enable_hook(
    connection: Connection,
    tenant_id: UUID,
    connection_id: UUID,
    secret: str,
    secrets: SecretStore,
) -> None:
    """Give the demo phone connection its webhook (development, when the dev secret is set).

    The signing secret is the development secret, stored in the secret store (only its
    reference in the database). The URL token is derived from it, so tools can compute the
    hook URL from .env; only the token's SHA-256 is stored.
    """
    refs = connection.execute(
        select(ConnectionRow.secret_refs).where(
            ConnectionRow.tenant_id == tenant_id, ConnectionRow.id == connection_id
        )
    ).scalar_one()
    reference = refs.get(SIGNING_SECRET)
    try:
        current = None if reference is None else secrets.get(tenant_id, reference)
    except SecretNotFound:
        current = None
    if current != secret:
        reference = secrets.put(tenant_id, SIGNING_SECRET, secret)
    token = hook_token(secret, connection_id)
    connection.execute(
        update(ConnectionRow)
        .where(ConnectionRow.tenant_id == tenant_id, ConnectionRow.id == connection_id)
        .values(
            secret_refs={**refs, SIGNING_SECRET: reference},
            webhook_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )
    )


def _keep_demo_catalogue(connection: Connection, tenant_id: UUID) -> None:
    """The ERP demos quote from the simulated catalogue. Migration 0011 gives a business our
    product list when it has no active source (e.g. after a downgrade and upgrade before this
    seed ran); such a second source is disabled here, as one business has one source."""
    others = connection.execute(
        update(ConnectionRow)
        .where(
            ConnectionRow.tenant_id == tenant_id,
            ConnectionRow.status == ConnectionStatus.ACTIVE.value,
            ConnectionRow.purposes.contains([Purpose.CATALOGUE.value]),
            ConnectionRow.provider_key != SIMULATED_PROVIDER,
        )
        .values(status=ConnectionStatus.DISABLED.value, updated_at=func.now())
        .returning(ConnectionRow.id)
    ).scalars()
    for connection_id in list(others):
        record_event(connection, tenant_id, connection_id, "status_changed", SEED_ACTOR)


def _demo_people(connection: Connection, password: str | None) -> None:
    """Verified users for the demo subjects (their email), before memberships reference them."""
    hasher = Argon2PasswordHasher() if password is not None else None
    for subject in sorted({subject for _, subject, _ in DEMO_MEMBERSHIPS}):
        connection.execute(
            insert(User)
            .values(
                id=uuid5(NAMESPACE_URL, f"demo-user:{subject}"),
                subject=subject,
                email=subject,
                email_normalized=subject,
                email_verified_at=func.now(),
            )
            .on_conflict_do_nothing(index_elements=["subject"])
        )
        # People backfilled by migration 0010 exist already, unverified.
        user_id = connection.execute(
            update(User)
            .where(User.subject == subject)
            .values(email_verified_at=func.coalesce(User.email_verified_at, func.now()))
            .returning(User.id)
        ).scalar_one()
        if hasher is None or password is None:
            continue
        current = connection.execute(
            select(UserCredential.password_hash).where(UserCredential.user_id == user_id)
        ).scalar_one_or_none()
        if current is not None and hasher.verify(current, password):
            continue
        new_hash = hasher.hash(password)
        connection.execute(
            insert(UserCredential)
            .values(user_id=user_id, password_hash=new_hash)
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={"password_hash": new_hash, "password_changed_at": func.now()},
            )
        )


def seed_demo_tenants(settings: Settings) -> None:
    if settings.environment not in {"development", "test"}:
        raise ValueError("Demo data seeding is disabled outside development/test")
    engine = build_engine(settings.migration_url())
    try:
        with engine.begin() as connection:
            demo_password = settings.demo_password
            _demo_people(connection, demo_password.get_secret_value() if demo_password else None)
            for tenant_id, name in DEMO_TENANTS:
                _set_tenant(connection, tenant_id)
                connection.execute(
                    insert(Tenant)
                    .values(id=tenant_id, name=name, status="active", went_live_at=func.now())
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                # Empty PDF details: demo PDFs keep printing only the business name.
                connection.execute(
                    insert(TenantProfile)
                    .values(tenant_id=tenant_id)
                    .on_conflict_do_nothing(index_elements=["tenant_id"])
                )
            for tenant_id, subject, role in DEMO_MEMBERSHIPS:
                _set_tenant(connection, tenant_id)
                connection.execute(
                    insert(Membership)
                    .values(id=uuid4(), tenant_id=tenant_id, subject=subject, role=role.value)
                    .on_conflict_do_nothing(index_elements=["tenant_id", "subject", "role"])
                )
            for tenant_id, currency, minor_units, rounding, tax_rate, stock_age in DEMO_POLICIES:
                _set_tenant(connection, tenant_id)
                connection.execute(
                    insert(TenantPricingPolicy)
                    .values(
                        tenant_id=tenant_id,
                        version=1,
                        currency=currency,
                        minor_units=minor_units,
                        rounding_mode=rounding.value,
                        tax_rate=tax_rate,
                        max_price_age_seconds=900,
                        max_stock_age_seconds=stock_age,
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id"])
                )
            for tenant_id, catalogue in DEMO_CONNECTIONS:
                _set_tenant(connection, tenant_id)
                _keep_demo_catalogue(connection, tenant_id)
                connection_id = ensure_connection(
                    connection,
                    tenant_id=tenant_id,
                    provider_key=SIMULATED_PROVIDER,
                    account_ref=SIMULATED_ACCOUNT,
                    display_name="Simulated phone and demo catalogue",
                    purposes=[Purpose.MANAGER_APPROVALS, Purpose.CATALOGUE],
                    created_by="seed",
                    config={"catalogue": catalogue},
                )
                if settings.simulated_channel_secret is not None:
                    _enable_hook(
                        connection,
                        tenant_id,
                        connection_id,
                        settings.simulated_channel_secret.get_secret_value(),
                        build_secret_store(settings),
                    )
            _set_tenant(connection, LOCAL_TENANT)
            # Migration 0011 may already have given it a product source (one per business).
            has_source = connection.execute(
                select(ConnectionRow.id).where(
                    ConnectionRow.tenant_id == LOCAL_TENANT,
                    ConnectionRow.status == "active",
                    ConnectionRow.purposes.contains([Purpose.CATALOGUE.value]),
                )
            ).first()
            if has_source is None:
                ensure_connection(
                    connection,
                    tenant_id=LOCAL_TENANT,
                    provider_key=LOCAL_CATALOGUE_PROVIDER,
                    account_ref=LOCAL_CATALOGUE_ACCOUNT,
                    display_name="Our product list",
                    purposes=[Purpose.CATALOGUE],
                    created_by=SEED_ACTOR,
                )
            ensure_connection(
                connection,
                tenant_id=LOCAL_TENANT,
                provider_key=SIMULATED_PROVIDER,
                account_ref=SIMULATED_ACCOUNT,
                display_name="Simulated phone",
                purposes=[Purpose.MANAGER_APPROVALS],
                created_by=SEED_ACTOR,
            )
            seed_local_catalogue(connection, LOCAL_TENANT, demo_catalogue("industrial"))
    finally:
        engine.dispose()


def seed_local_catalogue(
    connection: Connection, tenant_id: UUID, catalogue: SimulatedCatalogue
) -> None:
    """Copy a synthetic catalogue into our own catalogue tables, once (owner role, dev/test).

    Prices keep their original start dates (fixtures may be back-dated; staff entries may not)
    and become revisions of the one "standard" list. Stock counts are as of now. Nothing is
    written if the business already has products.
    """
    _set_tenant(connection, tenant_id)
    if connection.execute(
        select(CatalogueProductRow.id).where(CatalogueProductRow.tenant_id == tenant_id).limit(1)
    ).first():
        return
    categories: dict[str, UUID] = {}
    for name in sorted({p.category for p in catalogue.products.values() if p.category}):
        categories[name] = uuid4()
        connection.execute(
            insert(CatalogueCategory).values(
                id=categories[name],
                tenant_id=tenant_id,
                name=name,
                created_by=SEED_ACTOR,
                updated_by=SEED_ACTOR,
            )
        )
    for sku, product in sorted(catalogue.products.items()):
        product_id = uuid4()
        fields = ProductFields(
            name=product.description,
            unit=product.unit,
            category_id=categories.get(product.category or ""),
            minimum_quantity=product.minimum_quantity,
            quantity_step=product.quantity_step,
        )
        connection.execute(
            insert(CatalogueProductRow).values(
                id=product_id,
                tenant_id=tenant_id,
                sku=sku,
                search_terms=sorted(product_terms(sku, product.description, product.category)),
                created_by=SEED_ACTOR,
                updated_by=SEED_ACTOR,
                **fields_as_dict(fields),
            )
        )
        ordered = sorted(catalogue.prices.get(sku, ()), key=lambda price: price.effective_from)
        for revision, price in enumerate(ordered, start=1):
            connection.execute(
                insert(CataloguePriceRow).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    product_id=product_id,
                    price_list=LOCAL_PRICE_LIST,
                    revision=revision,
                    currency=price.currency,
                    unit=product.unit,
                    unit_price=price.unit_price,
                    effective_from=price.effective_from,
                    command_id=uuid4(),
                    created_by=SEED_ACTOR,
                )
            )
        if sku in catalogue.stock:
            connection.execute(
                insert(CatalogueStockRow).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    product_id=product_id,
                    location_code=DEFAULT_LOCATION,  # one location per business (Q21)
                    quantity=catalogue.stock[sku],
                    unit=product.unit,
                    as_of=func.now(),
                    command_id=uuid4(),
                    recorded_by=SEED_ACTOR,
                )
            )


def main() -> None:
    seed_demo_tenants(Settings())
    print(
        "Synthetic tenants, memberships, policies and connections are present. "
        "Existing rows unchanged."
    )


if __name__ == "__main__":
    main()
