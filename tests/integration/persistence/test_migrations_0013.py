"""Migration 0013: platform routes are found only by their exact key, and the downgrade is clean.

The round-trip test moves the shared *_test database down to 0012 and back up to head.
"""

import os
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from sales_platform.persistence.seed import INDUSTRIAL_TENANT, OFFICE_TENANT
from sales_platform.persistence.tenancy import tenant_transaction


def alembic(*arguments: str) -> None:
    environment = {
        **os.environ,
        "SALES_ENVIRONMENT": "test",
        "SALES_DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "SALES_MIGRATION_DATABASE_URL": os.environ["TEST_MIGRATION_DATABASE_URL"],
    }
    subprocess.run([sys.executable, "-m", "alembic", *arguments], check=True, env=environment)


def _connection(owner_engine: Engine, tenant_id: UUID) -> UUID:
    connection_id = uuid4()
    with tenant_transaction(owner_engine, tenant_id) as connection:
        connection.execute(
            text(
                "INSERT INTO connections (id, tenant_id, provider_key, display_name, purposes, "
                "account_ref, status, created_by) VALUES (:id, :t, 'telegram', 'Telegram', "
                "ARRAY['customer_messages'], :a, 'setting_up', 'test')"
            ),
            {"id": connection_id, "t": tenant_id, "a": f"test:{connection_id}"},
        )
    return connection_id


def _route(owner_engine: Engine, tenant_id: UUID, connection_id: UUID, key: str) -> None:
    with tenant_transaction(owner_engine, tenant_id) as connection:
        connection.execute(
            text(
                "INSERT INTO connection_routes (id, tenant_id, connection_id, provider_key, "
                "routing_key) VALUES (gen_random_uuid(), :t, :c, 'telegram', :k)"
            ),
            {"t": tenant_id, "c": connection_id, "k": key},
        )


def _lookup(engine: Engine, provider_key: str, routing_key: str) -> list[tuple[object, ...]]:
    """What the runtime role sees before the tenant is known (no app.tenant_id)."""
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.route_provider', :p, true)"), {"p": provider_key}
        )
        connection.execute(text("SELECT set_config('app.route_key', :k, true)"), {"k": routing_key})
        return [
            tuple(row)
            for row in connection.execute(
                text("SELECT tenant_id, connection_id FROM connection_routes")
            )
        ]


def test_a_route_is_revealed_only_by_its_exact_key(
    owner_engine: Engine, runtime_engine: Engine, worker_engine: Engine
) -> None:
    first = _connection(owner_engine, INDUSTRIAL_TENANT)
    second = _connection(owner_engine, OFFICE_TENANT)
    _route(owner_engine, INDUSTRIAL_TENANT, first, "bc:111")
    _route(owner_engine, OFFICE_TENANT, second, "bc:222")

    for engine in (runtime_engine, worker_engine):
        assert _lookup(engine, "telegram", "bc:111") == [(INDUSTRIAL_TENANT, first)]
        assert _lookup(engine, "telegram", "bc:222") == [(OFFICE_TENANT, second)]
        assert _lookup(engine, "telegram", "bc:%") == []
        assert _lookup(engine, "other", "bc:111") == []
    with runtime_engine.begin() as connection:  # no key presented: nothing at all
        assert connection.execute(text("SELECT count(*) FROM connection_routes")).scalar() == 0


def test_a_routing_key_belongs_to_one_connection_across_businesses(owner_engine: Engine) -> None:
    first = _connection(owner_engine, INDUSTRIAL_TENANT)
    second = _connection(owner_engine, OFFICE_TENANT)
    _route(owner_engine, INDUSTRIAL_TENANT, first, "owner:42")
    with pytest.raises(IntegrityError):
        _route(owner_engine, OFFICE_TENANT, second, "owner:42")


def test_reply_classifications_are_append_only(owner_engine: Engine) -> None:
    connection_id = _connection(owner_engine, INDUSTRIAL_TENANT)
    event = uuid4()
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, connection_ref, "
                "event_type, provider_event_id, payload_sha256, envelope, received_at, "
                "correlation_id) VALUES (:id, :t, 'telegram', 'x', :c, 'manager_reply', 'm-1', "
                ":h, '{}', now(), gen_random_uuid())"
            ),
            {"id": event, "t": INDUSTRIAL_TENANT, "c": connection_id, "h": "0" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO manager_reply_classifications (id, tenant_id, inbox_event_id, text, "
                "text_sha256, intent, model, outcome) VALUES (gen_random_uuid(), :t, :e, 'ok', "
                ":h, 'approve', 'rules', 'confirm_asked')"
            ),
            {"t": INDUSTRIAL_TENANT, "e": event, "h": "1" * 64},
        )
    with (
        pytest.raises(DBAPIError, match="immutable"),
        tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection,
    ):
        connection.execute(text("UPDATE manager_reply_classifications SET intent = 'reject'"))


def test_downgrade_removes_phase_5_state_and_upgrade_restores_it(owner_engine: Engine) -> None:
    connection_id = _connection(owner_engine, INDUSTRIAL_TENANT)
    _route(owner_engine, INDUSTRIAL_TENANT, connection_id, "link:abc")
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, connection_ref, "
                "event_type, provider_event_id, payload_sha256, envelope, received_at, "
                "correlation_id) VALUES (gen_random_uuid(), :t, 'telegram', 'x', :c, "
                "'connection_update', 'u-1', :h, '{}', now(), gen_random_uuid())"
            ),
            {"t": INDUSTRIAL_TENANT, "c": connection_id, "h": "0" * 64},
        )
        connection.execute(
            text(
                'UPDATE connections SET pending_action = \'{"kind": "show_steps"}\' WHERE id = :c'
            ),
            {"c": connection_id},
        )
        connection.execute(
            text(
                "INSERT INTO connection_events (id, tenant_id, connection_id, kind, actor) "
                "VALUES (gen_random_uuid(), :t, :c, 'resumed', 'test')"
            ),
            {"t": INDUSTRIAL_TENANT, "c": connection_id},
        )

    alembic("downgrade", "0012_conversations")
    try:
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
            tables = connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
                    "('connection_routes', 'approval_reply_prompts', "
                    "'manager_reply_classifications', 'media_links')"
                )
            ).scalar()
            events = connection.execute(
                text("SELECT count(*) FROM inbox_events WHERE connection_ref = :c"),
                {"c": connection_id},
            ).scalar()
            kinds = connection.execute(
                text("SELECT kind FROM connection_events WHERE connection_id = :c"),
                {"c": connection_id},
            ).all()
        assert tables == 0
        assert events == 0
        assert kinds == []
    finally:
        alembic("upgrade", "head")
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        pending = connection.execute(
            text("SELECT pending_action FROM connections WHERE id = :c"), {"c": connection_id}
        ).scalar()
    assert pending is None
