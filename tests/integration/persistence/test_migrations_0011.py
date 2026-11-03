"""Migration 0011 with data: default product source, the documented losses on downgrade, and
the backfill on upgrade. It moves the shared *_test database down to 0010 and back to head."""

import os
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from sales_platform.domain.catalogue_admin import ProductFields
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.persistence.catalogue import SqlCatalogueStore
from sales_platform.persistence.seed import INDUSTRIAL_TENANT, LOCAL_TENANT
from sales_platform.persistence.tenancy import tenant_transaction

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("fresh_accounts")]

_SOURCES = (
    "SELECT provider_key FROM connections WHERE status = 'active' "
    "AND purposes @> ARRAY['catalogue']::text[]"
)


def alembic(*arguments: str) -> None:
    environment = {
        **os.environ,
        "SALES_ENVIRONMENT": "test",
        "SALES_DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "SALES_MIGRATION_DATABASE_URL": os.environ["TEST_MIGRATION_DATABASE_URL"],
    }
    subprocess.run([sys.executable, "-m", "alembic", *arguments], check=True, env=environment)


def owner_scalar(owner_engine: Engine, tenant: UUID, query: str) -> object:
    with tenant_transaction(owner_engine, tenant) as connection:
        return connection.execute(text(query)).scalar_one()


def _business(
    owner_engine: Engine, runtime_engine: Engine, provider: str | None, *, before_0011: bool = False
) -> UUID:
    """A verified person creates a business; before 0011 the function had 7 arguments."""
    user_id = uuid4()
    subject = f"user:{user_id}"
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, subject, email, email_normalized, email_verified_at) "
                "VALUES (:id, :s, :e, :e, now())"
            ),
            {"id": user_id, "s": subject, "e": f"{user_id}@shop.example"},
        )
    with runtime_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.subject', :s, true)"), {"s": subject})
        source = "" if before_0011 else ", CAST(:p AS text), 'Our product list'"
        tenant = connection.execute(
            text(f"SELECT create_business(:u, 'Acme', 'IN', 'UTC', 'INR', NULL, 7{source})"),
            {"u": user_id, "p": provider} if not before_0011 else {"u": user_id},
        ).scalar_one()
    return UUID(str(tenant))


def test_create_business_adds_the_default_source_only_when_asked(
    owner_engine: Engine, runtime_engine: Engine
) -> None:
    with_source = _business(owner_engine, runtime_engine, "local_catalogue")
    without = _business(owner_engine, runtime_engine, None)
    assert owner_scalar(owner_engine, with_source, _SOURCES) == "local_catalogue"
    assert owner_scalar(owner_engine, without, "SELECT count(*) FROM connections") == 0
    assert (
        owner_scalar(
            owner_engine,
            with_source,
            "SELECT count(*) FROM connection_events WHERE kind = 'created'",
        )
        == 1
    )


def test_downgrade_drops_the_catalogue_and_upgrade_backfills_a_source(
    owner_engine: Engine, runtime_engine: Engine
) -> None:
    tenant = _business(owner_engine, runtime_engine, "local_catalogue")
    context = TenantContext(tenant, "admin@acme.example", frozenset({Role.ADMINISTRATOR}))
    SqlCatalogueStore(runtime_engine).create_product(
        context, "ACME-1", ProductFields("Widget", "each"), 10_000
    )
    alembic("downgrade", "0010_accounts")
    try:
        with owner_engine.begin() as connection:
            assert (
                connection.execute(text("SELECT to_regclass('catalogue_products')")).scalar_one()
                is None
            )
        # Local sources are gone; the simulated demo source stays.
        assert owner_scalar(owner_engine, tenant, "SELECT count(*) FROM connections") == 0
        assert owner_scalar(owner_engine, LOCAL_TENANT, f"SELECT count(*) FROM ({_SOURCES}) s") == 0
        assert owner_scalar(owner_engine, INDUSTRIAL_TENANT, _SOURCES) == "simulated"
        # The 0010 function (7 arguments) is back.
        _business(owner_engine, runtime_engine, None, before_0011=True)
    finally:
        alembic("upgrade", "head")
    assert owner_scalar(owner_engine, tenant, _SOURCES) == "local_catalogue"
    assert owner_scalar(owner_engine, LOCAL_TENANT, _SOURCES) == "local_catalogue"
    assert owner_scalar(owner_engine, INDUSTRIAL_TENANT, _SOURCES) == "simulated"
    assert (
        owner_scalar(
            owner_engine,
            tenant,
            "SELECT created_by FROM connections WHERE provider_key = 'local_catalogue'",
        )
        == "migration:0011_catalogue"
    )
