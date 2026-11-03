"""Schema rules that live in the database itself (functions, triggers, policies, constraints).

Kept from the former per-migration test files (0011, 0012, 0013) when the migrations were
replaced by one baseline (ADR 0026 step 4); their upgrade/downgrade round trips went with the
old migrations.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from sales_platform.persistence.seed import INDUSTRIAL_TENANT, OFFICE_TENANT
from sales_platform.persistence.tenancy import tenant_transaction

pytestmark = [pytest.mark.integration]

_SOURCES = (
    "SELECT provider_key FROM connections WHERE status = 'active' "
    "AND purposes @> ARRAY['catalogue']::text[]"
)


def _owner_scalar(owner_engine: Engine, tenant: UUID, query: str) -> object:
    with tenant_transaction(owner_engine, tenant) as connection:
        return connection.execute(text(query)).scalar_one()


# --- create_business: the default product source (former 0011) ---------------------------


def _business(owner_engine: Engine, runtime_engine: Engine, provider: str | None) -> UUID:
    """A verified person creates a business through the SECURITY DEFINER function."""
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
        tenant = connection.execute(
            text(
                "SELECT create_business(:u, 'Acme', 'IN', 'UTC', 'INR', NULL, 7, "
                "CAST(:p AS text), 'Our product list')"
            ),
            {"u": user_id, "p": provider},
        ).scalar_one()
    return UUID(str(tenant))


@pytest.mark.usefixtures("fresh_accounts")
def test_create_business_adds_the_default_source_only_when_asked(
    owner_engine: Engine, runtime_engine: Engine
) -> None:
    with_source = _business(owner_engine, runtime_engine, "local_catalogue")
    without = _business(owner_engine, runtime_engine, None)
    assert _owner_scalar(owner_engine, with_source, _SOURCES) == "local_catalogue"
    assert _owner_scalar(owner_engine, without, "SELECT count(*) FROM connections") == 0
    assert (
        _owner_scalar(
            owner_engine,
            with_source,
            "SELECT count(*) FROM connection_events WHERE kind = 'created'",
        )
        == 1
    )


# --- the enquiry reopen guard (former 0012) ------------------------------------------------


def _api_connection(owner_engine: Engine, account: str) -> UUID:
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


def _waiting_enquiry(owner_engine: Engine, connection_ref: UUID) -> UUID:
    event, enquiry = uuid4(), uuid4()
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, connection_ref, "
                "provider_event_id, payload_sha256, envelope, received_at, correlation_id) "
                "VALUES (:id, :t, 'api', 'key', :c, :p, :h, '{}', now(), gen_random_uuid())"
            ),
            {
                "id": event,
                "t": INDUSTRIAL_TENANT,
                "c": connection_ref,
                "p": f"request-{event}",
                "h": "0" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO enquiries (id, tenant_id, inbox_event_id, channel, status, "
                "customer_reference, text, correlation_id, received_at) VALUES (:id, :t, :e, "
                "'api', 'needs_clarification', 'CUST-1', 'message', gen_random_uuid(), now())"
            ),
            {"id": enquiry, "t": INDUSTRIAL_TENANT, "e": event},
        )
    return enquiry


def test_api_role_may_only_reopen_a_waiting_enquiry(
    owner_engine: Engine, runtime_engine: Engine
) -> None:
    """The reopen guard: needs_clarification -> received is the API role's only status change."""
    waiting = _waiting_enquiry(owner_engine, _api_connection(owner_engine, f"api-key:{uuid4()}"))

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


# --- platform routes and reply classifications (former 0013) ------------------------------


def _telegram_connection(owner_engine: Engine, tenant_id: UUID) -> UUID:
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
    first = _telegram_connection(owner_engine, INDUSTRIAL_TENANT)
    second = _telegram_connection(owner_engine, OFFICE_TENANT)
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
    first = _telegram_connection(owner_engine, INDUSTRIAL_TENANT)
    second = _telegram_connection(owner_engine, OFFICE_TENANT)
    _route(owner_engine, INDUSTRIAL_TENANT, first, "owner:42")
    with pytest.raises(IntegrityError):
        _route(owner_engine, OFFICE_TENANT, second, "owner:42")


def test_reply_classifications_are_append_only(owner_engine: Engine) -> None:
    connection_id = _telegram_connection(owner_engine, INDUSTRIAL_TENANT)
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
