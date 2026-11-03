"""Migration 0008 with M6-shaped data: backfill on upgrade, a clean downgrade, and back again.

It moves the shared *_test database down to 0007 and back up to head inside the test.
"""

import os
import subprocess
import sys
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from sales_platform.persistence.seed import INDUSTRIAL_TENANT
from sales_platform.persistence.tenancy import tenant_transaction


def alembic(*arguments: str) -> None:
    environment = {
        **os.environ,
        "SALES_ENVIRONMENT": "test",
        "SALES_DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "SALES_MIGRATION_DATABASE_URL": os.environ["TEST_MIGRATION_DATABASE_URL"],
    }
    subprocess.run([sys.executable, "-m", "alembic", *arguments], check=True, env=environment)


def scalar(owner_engine: Engine, query: str, **values: object) -> object:
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        return connection.execute(text(query), values).scalar_one()


def m6_data(owner_engine: Engine) -> tuple[UUID, UUID]:
    """An active and a revoked API key, an API enquiry receipt and a simulated binding."""
    active, revoked = uuid4(), uuid4()
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        for key_id, revoked_at in ((active, None), (revoked, "now()")):
            connection.execute(
                text(
                    "INSERT INTO api_keys (id, tenant_id, name, role, secret_sha256, revoked_at) "
                    f"VALUES (:id, :t, 'Key', 'intake_service', :h, {revoked_at or 'NULL'})"
                ),
                {"id": key_id, "t": INDUSTRIAL_TENANT, "h": "0" * 64},
            )
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, "
                "provider_event_id, payload_sha256, envelope, received_at, correlation_id) "
                "VALUES (gen_random_uuid(), :t, 'api', :c, 'request-000001', :h, "
                '\'{"sender_external_id": "CUST-1"}\', now(), gen_random_uuid())'
            ),
            {"t": INDUSTRIAL_TENANT, "c": f"api-key:{active}", "h": "0" * 64},
        )
        enrollment = uuid4()
        connection.execute(
            text(
                "INSERT INTO channel_enrollments (id, tenant_id, subject, channel, code_sha256, "
                "expires_at, consumed_at) VALUES (:id, :t, 'manager@industrial.example', "
                "'simulated', :h, now(), now())"
            ),
            {"id": enrollment, "t": INDUSTRIAL_TENANT, "h": "e" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO channel_bindings (id, tenant_id, subject, channel, connection_id, "
                "external_user_id, enrollment_id) VALUES (gen_random_uuid(), :t, "
                "'manager@industrial.example', 'simulated', 'simulated-local', 'phone', :e)"
            ),
            {"t": INDUSTRIAL_TENANT, "e": enrollment},
        )
    return active, revoked


def test_upgrade_backfills_and_downgrade_round_trips(owner_engine: Engine) -> None:
    alembic("downgrade", "0007_acceptance")
    try:
        active, revoked = m6_data(owner_engine)
        alembic("upgrade", "head")

        status = "SELECT status FROM connections WHERE provider_key = 'api' AND account_ref = :a"
        assert scalar(owner_engine, status, a=f"api-key:{active}") == "active"
        assert scalar(owner_engine, status, a=f"api-key:{revoked}") == "disabled"
        assert (
            scalar(
                owner_engine,
                "SELECT count(*) FROM inbox_events e JOIN connections c ON c.id = e.connection_ref "
                "WHERE c.account_ref = :a AND e.event_type = 'customer_message' "
                "AND e.envelope->>'reply_to' = :a",
                a=f"api-key:{active}",
            )
            == 1
        )
        assert (
            scalar(
                owner_engine,
                "SELECT count(*) FROM channel_bindings b "
                "JOIN connections c ON c.id = b.connection_ref "
                "WHERE c.provider_key = 'simulated' AND c.account_ref = 'simulated-local' "
                "AND c.purposes = ARRAY['manager_approvals']",
            )
            == 1
        )
        assert (
            scalar(
                owner_engine,
                "SELECT count(*) FROM connections c WHERE NOT EXISTS (SELECT 1 FROM "
                "connection_events e WHERE e.connection_id = c.id AND e.kind = 'migrated')",
            )
            == 0
        )

        alembic("downgrade", "0007_acceptance")
        assert scalar(owner_engine, "SELECT count(*) FROM channel_bindings") == 1
        assert (
            scalar(owner_engine, "SELECT count(*) FROM inbox_events WHERE envelope ? 'reply_to'")
            == 0
        )
    finally:
        alembic("upgrade", "head")
    assert (
        scalar(owner_engine, "SELECT count(*) FROM inbox_events WHERE connection_ref IS NULL") == 0
    )
