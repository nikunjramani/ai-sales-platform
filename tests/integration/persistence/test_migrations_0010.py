"""Migration 0010 with data: backfill on upgrade, the documented losses on downgrade, and back.

It moves the shared *_test database down to 0009 and back up to head inside the test.
"""

import os
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from sales_platform.persistence.seed import INDUSTRIAL_TENANT, OFFICE_TENANT
from sales_platform.persistence.tenancy import tenant_transaction

pytestmark = pytest.mark.usefixtures("fresh_accounts")


def alembic(*arguments: str) -> None:
    environment = {
        **os.environ,
        "SALES_ENVIRONMENT": "test",
        "SALES_DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "SALES_MIGRATION_DATABASE_URL": os.environ["TEST_MIGRATION_DATABASE_URL"],
    }
    subprocess.run([sys.executable, "-m", "alembic", *arguments], check=True, env=environment)


def owner_scalar(owner_engine: Engine, tenant: UUID, query: str, **values: object) -> object:
    with tenant_transaction(owner_engine, tenant) as connection:
        return connection.execute(text(query), values).scalar_one()


def self_created_business(owner_engine: Engine, runtime_engine: Engine) -> tuple[UUID, str]:
    """A person who signed up and created a business with pricing "set later"."""
    user_id = uuid4()
    subject = f"user:{user_id}"
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, subject, email, email_normalized, email_verified_at) "
                "VALUES (:id, :s, 'ana@shop.example', 'ana@shop.example', now())"
            ),
            {"id": user_id, "s": subject},
        )
    with runtime_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.subject', :s, true)"), {"s": subject})
        tenant = connection.execute(
            text("SELECT create_business(:u, 'Acme', 'IN', 'UTC', 'INR', NULL, 7)"),
            {"u": user_id},
        ).scalar_one()
    return UUID(str(tenant)), subject


def test_downgrade_documents_its_losses_and_upgrade_backfills(
    owner_engine: Engine, runtime_engine: Engine
) -> None:
    tenant, subject = self_created_business(owner_engine, runtime_engine)
    alembic("downgrade", "0009_outbound_messages")
    try:
        # The business and its memberships stay; its incomplete pricing is gone.
        assert owner_scalar(owner_engine, tenant, "SELECT count(*) FROM tenants") == 1
        assert owner_scalar(owner_engine, tenant, "SELECT count(*) FROM memberships") == 2
        assert (
            owner_scalar(owner_engine, tenant, "SELECT count(*) FROM tenant_pricing_policies") == 0
        )
        assert (
            owner_scalar(
                owner_engine,
                INDUSTRIAL_TENANT,
                "SELECT count(*) FROM tenant_pricing_policies",
            )
            == 1
        )
        with owner_engine.begin() as connection:
            exists = connection.execute(text("SELECT to_regclass('users')")).scalar_one()
        assert exists is None
        alembic("upgrade", "head")

        # Back up: one person per membership subject, never verified by the backfill.
        with owner_engine.begin() as connection:
            rows = connection.execute(
                text("SELECT subject, email FROM users WHERE email_verified_at IS NULL")
            ).all()
        people = {row.subject: row.email for row in rows}
        assert people[subject] is None  # "user:<uuid>" is not an address
        assert people["manager@industrial.example"] == "manager@industrial.example"
        for business in (INDUSTRIAL_TENANT, OFFICE_TENANT, tenant):
            assert owner_scalar(owner_engine, business, "SELECT status FROM tenants") == "active"
            assert (
                owner_scalar(owner_engine, business, "SELECT went_live_at IS NOT NULL FROM tenants")
                is True
            )
            assert owner_scalar(owner_engine, business, "SELECT count(*) FROM tenant_profiles") == 1
    finally:
        alembic("upgrade", "head")
