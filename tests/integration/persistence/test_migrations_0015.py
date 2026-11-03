"""Migrations 0014 and 0015: down to 0013 with phase 6/7 data present and back up to head.

Data present on the way down: a synced product with a synced price, a sync run, a business
that creates orders automatically, an order question in the inbox and a queued order job.
"""

import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from sales_platform.persistence.seed import LOCAL_TENANT
from sales_platform.persistence.tenancy import tenant_transaction

pytestmark = pytest.mark.integration


def alembic(*arguments: str) -> None:
    environment = {
        **os.environ,
        "SALES_ENVIRONMENT": "test",
        "SALES_DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "SALES_MIGRATION_DATABASE_URL": os.environ["TEST_MIGRATION_DATABASE_URL"],
    }
    subprocess.run([sys.executable, "-m", "alembic", *arguments], check=True, env=environment)


def _phase_6_7_data(owner: Engine) -> None:
    with tenant_transaction(owner, LOCAL_TENANT) as connection:
        source = connection.execute(
            text(
                "SELECT id FROM connections WHERE tenant_id = :t AND status = 'active' "
                "AND purposes @> ARRAY['catalogue']"
            ),
            {"t": LOCAL_TENANT},
        ).scalar_one()
        product = uuid4()
        connection.execute(
            text(
                "INSERT INTO catalogue_products (id, tenant_id, sku, name, unit, "
                "minimum_quantity, quantity_step, search_terms, source, external_id, synced_at, "
                "created_by, updated_by) VALUES (:id, :t, 'SYNC-1', 'Synced', 'each', 1, 1, "
                "ARRAY['sync'], 'sync', 'SYNC-1', now(), 'sync:test', 'sync:test')"
            ),
            {"id": product, "t": LOCAL_TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO catalogue_prices (id, tenant_id, product_id, revision, currency, "
                "unit, unit_price, effective_from, source, command_id, created_by) VALUES "
                "(gen_random_uuid(), :t, :p, 1, 'INR', 'each', 10, now(), 'sync', "
                "gen_random_uuid(), 'sync:test')"
            ),
            {"t": LOCAL_TENANT, "p": product},
        )
        connection.execute(
            text(
                "INSERT INTO catalogue_sync_runs (id, tenant_id, connection_id, trigger, status, "
                "finished_at) VALUES (gen_random_uuid(), :t, :c, 'manual', 'succeeded', now())"
            ),
            {"t": LOCAL_TENANT, "c": source},
        )
        connection.execute(
            text("UPDATE tenants SET order_creation = 'automatic' WHERE id = :t"),
            {"t": LOCAL_TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, connection_ref, "
                "provider_event_id, event_type, envelope, payload_sha256, received_at, "
                "correlation_id) VALUES (gen_random_uuid(), :t, 'web_chat', 'x', :c, 'e-1', "
                "'order_status_request', '{}'::jsonb, :sha, now(), gen_random_uuid())"
            ),
            {"t": LOCAL_TENANT, "c": source, "sha": "0" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO jobs (id, tenant_id, kind, payload, status, max_attempts, run_at, "
                "correlation_id) VALUES (gen_random_uuid(), :t, 'create_order', '{}'::jsonb, "
                "'pending', 5, now(), gen_random_uuid())"
            ),
            {"t": LOCAL_TENANT},
        )


def test_round_trip_to_0013_with_phase_6_and_7_data(owner_engine: Engine) -> None:
    _phase_6_7_data(owner_engine)
    alembic("downgrade", "0013_provider_state")
    try:
        with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
            tables = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                        "('orders', 'order_events', 'catalogue_sync_runs')"
                    )
                ).scalars()
            )
            sources = connection.execute(
                text("SELECT DISTINCT source FROM catalogue_prices WHERE tenant_id = :t"),
                {"t": LOCAL_TENANT},
            ).scalars()
            jobs = connection.execute(
                text("SELECT count(*) FROM jobs WHERE kind = 'create_order'")
            ).scalar_one()
        assert tables == set()
        assert set(sources) == {"manual"}  # the copy stays, relabelled
        assert jobs == 0
    finally:
        alembic("upgrade", "head")
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        creation = connection.execute(
            text("SELECT order_creation FROM tenants WHERE id = :t"), {"t": LOCAL_TENANT}
        ).scalar_one()
    assert creation == "staff"  # the column came back with its default
