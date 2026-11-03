"""Migration 0012 with phase 3-shaped data: backfill on upgrade, a clean downgrade, and back.

It moves the shared *_test database down to 0011 and back up to head inside the test.
"""

import os
import subprocess
import sys
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

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


def _rows(owner_engine: Engine, query: str, **values: object) -> list[tuple[object, ...]]:
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        return [tuple(row) for row in connection.execute(text(query), values)]


def _connection(owner_engine: Engine, account: str) -> UUID:
    connection_id = uuid4()
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        connection.execute(
            text(
                "INSERT INTO connections (id, tenant_id, provider_key, display_name, purposes, "
                "account_ref, status, created_by) VALUES (:id, :t, 'api', :a, "
                "ARRAY['customer_messages'], :a, 'active', 'test')"
            ),
            {"id": connection_id, "t": INDUSTRIAL_TENANT, "a": account},
        )
    return connection_id


def _enquiry(
    owner_engine: Engine, connection_ref: UUID, reference: str | None, minute: int
) -> UUID:
    event, enquiry = uuid4(), uuid4()
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, connection_ref, "
                "provider_event_id, payload_sha256, envelope, received_at, correlation_id) "
                "VALUES (:id, :t, 'api', 'key', :c, :p, :h, '{}', "
                "timestamptz '2026-10-01 10:00+00' + make_interval(mins => :m), "
                "gen_random_uuid())"
            ),
            {
                "id": event,
                "t": INDUSTRIAL_TENANT,
                "c": connection_ref,
                "p": f"request-{event}",
                "h": "0" * 64,
                "m": minute,
            },
        )
        connection.execute(
            text(
                "INSERT INTO enquiries (id, tenant_id, inbox_event_id, channel, status, "
                "customer_reference, text, correlation_id, received_at) VALUES (:id, :t, :e, "
                "'api', 'needs_clarification', :r, :x, gen_random_uuid(), "
                "timestamptz '2026-10-01 10:00+00' + make_interval(mins => :m))"
            ),
            {
                "id": enquiry,
                "t": INDUSTRIAL_TENANT,
                "e": event,
                "r": reference,
                "x": f"message {minute}",
                "m": minute,
            },
        )
    return enquiry


def test_upgrade_backfills_and_downgrade_round_trips(owner_engine: Engine) -> None:
    alembic("downgrade", "0011_catalogue")
    try:
        key_a = _connection(owner_engine, f"api-key:{uuid4()}")
        key_b = _connection(owner_engine, f"api-key:{uuid4()}")
        first = _enquiry(owner_engine, key_a, "CUST-1", 1)
        second = _enquiry(owner_engine, key_a, "CUST-1", 2)
        other_key = _enquiry(owner_engine, key_b, "CUST-1", 3)
        anonymous = _enquiry(owner_engine, key_a, None, 4)
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
            connection.execute(
                text(
                    "INSERT INTO attachments (id, tenant_id, enquiry_id, filename, "
                    "declared_content_type, size_bytes, sha256, status, storage_key) VALUES "
                    "(gen_random_uuid(), :t, :e, 'a.txt', 'text/plain', 1, :h, 'clean', 'k')"
                ),
                {"t": INDUSTRIAL_TENANT, "e": second, "h": "1" * 64},
            )

        alembic("upgrade", "head")

        # One identity, contact and conversation per (connection, reference): CUST-1 on key A
        # and CUST-1 on key B are different customers until someone merges them (D13).
        identities = _rows(
            owner_engine,
            "SELECT connection_ref, external_id, verified, consent_status "
            "FROM contact_identities ORDER BY first_seen_at",
        )
        assert identities == [
            (key_a, "CUST-1", True, "implied"),
            (key_b, "CUST-1", True, "implied"),
        ]
        assert _rows(owner_engine, "SELECT count(*) FROM contacts") == [(2,)]
        links = {
            row[0]: row[1]
            for row in _rows(
                owner_engine,
                "SELECT e.id, c.identity_id IS NOT NULL FROM enquiries e "
                "LEFT JOIN conversations c ON c.tenant_id = e.tenant_id "
                "AND c.id = e.conversation_id",
            )
        }
        assert links == {first: True, second: True, other_key: True, anonymous: False}
        same = _rows(
            owner_engine,
            "SELECT count(DISTINCT conversation_id) FROM enquiries WHERE id IN (:a, :b)",
            a=first,
            b=second,
        )
        assert same == [(1,)]

        # One inbound message per enquiry, in time order, with its attachments.
        messages = _rows(
            owner_engine,
            "SELECT enquiry_id, direction, author, text, jsonb_array_length(attachment_ids), "
            "conversation_id IS NULL FROM conversation_messages ORDER BY seq",
        )
        assert messages == [
            (first, "inbound", "customer", "message 1", 0, False),
            (second, "inbound", "customer", "message 2", 1, False),
            (other_key, "inbound", "customer", "message 3", 0, False),
            (anonymous, "inbound", "customer", "message 4", 0, True),
        ]
        assert _rows(
            owner_engine,
            "SELECT acknowledge_enquiries FROM tenants WHERE id = :t",
            t=INDUSTRIAL_TENANT,
        ) == [(True,)]

        # A follow-up round with a checkpoint: only meaningful in 0012, removed on downgrade.
        round_two = uuid4()
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
            for round_number, run_id in ((1, uuid4()), (2, round_two)):
                connection.execute(
                    text(
                        "INSERT INTO workflow_runs (id, tenant_id, enquiry_id, graph_version, "
                        "round, status) VALUES (:id, :t, :e, 'enquiry-extraction-v1', :r, "
                        "'needs_clarification')"
                    ),
                    {"id": run_id, "t": INDUSTRIAL_TENANT, "e": first, "r": round_number},
                )
        with owner_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, "
                    "metadata) VALUES (:t, '', 'c1', '{}', '{}')"
                ),
                {"t": str(round_two)},
            )

        alembic("downgrade", "0011_catalogue")
        assert _rows(owner_engine, "SELECT count(*) FROM workflow_runs") == [(1,)]
        with owner_engine.begin() as connection:
            left = connection.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"), {"t": str(round_two)}
            ).scalar_one()
        assert left == 0
        assert _rows(owner_engine, "SELECT count(*) FROM enquiries") == [(4,)]
    finally:
        alembic("upgrade", "head")
    assert _rows(owner_engine, "SELECT count(*) FROM conversation_messages") == [(4,)]


def test_api_role_may_only_reopen_a_waiting_enquiry(
    owner_engine: Engine, runtime_engine: Engine
) -> None:
    """The reopen guard: needs_clarification -> received is the API role's only status change."""
    key = _connection(owner_engine, f"api-key:{uuid4()}")
    waiting = _enquiry(owner_engine, key, "CUST-1", 1)

    def set_status(enquiry_id: UUID, status: str) -> None:
        with tenant_transaction(runtime_engine, INDUSTRIAL_TENANT) as connection:
            connection.execute(
                text("UPDATE enquiries SET status = :s WHERE id = :id"),
                {"s": status, "id": enquiry_id},
            )

    with pytest.raises(DBAPIError, match="not allowed"):
        set_status(waiting, "ready_for_quotation")
    set_status(waiting, "received")
    with pytest.raises(DBAPIError, match="not allowed"):
        set_status(waiting, "needs_clarification")
