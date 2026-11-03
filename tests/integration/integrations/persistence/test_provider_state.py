"""Phase 5 state on PostgreSQL with the real roles: lifecycle steps, routes, staff messages and
customer messages from webhooks (CustomerInbox through the real intake store)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from sales_platform.accounts.persistence.accounts import SqlBusinessStore
from sales_platform.customer_channels.application.customer_inbox import CustomerInbox
from sales_platform.customer_channels.domain.chat import ChatLimits
from sales_platform.enquiries.application.follow_ups import default_router
from sales_platform.enquiries.persistence.intake import SqlIntakeStore
from sales_platform.enquiries.persistence.staff_messages import SqlStaffMessages
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import ConnectionStatus
from sales_platform.integrations.domain.events import CustomerMessage
from sales_platform.integrations.persistence.connections import SqlConnectionStore
from sales_platform.integrations.persistence.inbound import SqlInboundStore
from sales_platform.integrations.persistence.provider_state import (
    SqlConnectionLifecycle,
    SqlRouteLookup,
    SqlRouteWriter,
)
from sales_platform.integrations.ports.connections import ConnectionChange, RouteTaken
from sales_platform.platform.adapters.secrets.memory import InMemorySecretStore
from sales_platform.platform.adapters.storage.local import LocalObjectStorage
from sales_platform.platform.domain.rate_limits import RateRule
from sales_platform.platform.persistence.rate_limits import SqlRateLimiter
from sales_platform.platform.persistence.tenancy import tenant_transaction
from tests.unit.one_click import TOOL, OneClickTool

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
PAST = datetime(2026, 1, 1, tzinfo=UTC)


def rows(owner_engine: Engine, tenant_id: UUID, query: str, **values: object) -> list[tuple]:  # type: ignore[type-arg]
    with tenant_transaction(owner_engine, tenant_id) as connection:
        return [tuple(row) for row in connection.execute(text(query), values)]


def change(**values: object) -> ConnectionChange:
    base: dict[str, object] = {
        "status": ConnectionStatus.SETTING_UP,
        "config": {},
        "secret_refs": {},
        "last_error": None,
        "pending_action": None,
        "event_kind": "provisioned",
    }
    return ConnectionChange(**{**base, **values})  # type: ignore[arg-type]


@pytest.mark.parametrize("role", ["runtime", "worker"])
def test_both_roles_apply_a_step_in_one_transaction(
    role: str,
    owner_engine: Engine,
    runtime_engine: Engine,
    worker_engine: Engine,
    industrial_tenant: UUID,
) -> None:
    engine = runtime_engine if role == "runtime" else worker_engine
    store = SqlConnectionLifecycle(runtime_engine)
    connection_id = store.create(
        industrial_tenant, TOOL, "Tool", ["customer_messages"], "user:admin"
    )
    SqlConnectionLifecycle(engine).apply(
        industrial_tenant,
        connection_id,
        change(
            status=ConnectionStatus.ACTIVE,
            config={"public_detail": "ready"},
            secret_refs={"token": "local:ref"},
            pending_action=None,
            add_routes={f"owner:{connection_id}": None, f"link:{connection_id}": PAST},
            poll_at=NOW + timedelta(minutes=5),
            event_kind="resumed",
        ),
        "provider:test",
    )

    row = SqlConnectionStore(engine).get(industrial_tenant, connection_id)
    assert row is not None and row.active and row.secret_refs == {"token": "local:ref"}
    assert SqlRouteLookup(engine).resolve(TOOL, f"owner:{connection_id}") == (
        industrial_tenant,
        connection_id,
    )
    assert SqlRouteLookup(engine).resolve(TOOL, f"link:{connection_id}") is None  # expired
    assert rows(
        owner_engine,
        industrial_tenant,
        "SELECT kind FROM jobs WHERE payload->>'connection_id' = :c",
        c=str(connection_id),
    ) == [("poll_connection",)]
    assert [kind for (kind,) in rows(
        owner_engine,
        industrial_tenant,
        "SELECT kind FROM connection_events WHERE connection_id = :c ORDER BY created_at",
        c=connection_id,
    )] == ["created", "resumed"]  # fmt: skip


def test_a_key_of_another_business_rolls_the_whole_step_back(
    runtime_engine: Engine, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    store = SqlConnectionLifecycle(runtime_engine)
    first = store.create(industrial_tenant, TOOL, "Tool", ["customer_messages"], "user:a")
    second = store.create(office_tenant, TOOL, "Tool", ["customer_messages"], "user:b")
    key = f"owner:{uuid4()}"
    store.apply(industrial_tenant, first, change(add_routes={key: None}), "user:a")

    with pytest.raises(RouteTaken):
        store.apply(
            office_tenant,
            second,
            change(status=ConnectionStatus.ACTIVE, add_routes={key: None}),
            "user:b",
        )

    row = SqlConnectionStore(runtime_engine).get(office_tenant, second)
    assert row is not None and row.status is ConnectionStatus.SETTING_UP
    assert SqlRouteLookup(runtime_engine).resolve(TOOL, key) == (industrial_tenant, first)


def test_message_routes_are_added_by_the_worker_and_never_stolen(
    runtime_engine: Engine, worker_engine: Engine, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    store = SqlConnectionLifecycle(runtime_engine)
    first = store.create(industrial_tenant, TOOL, "Tool", ["manager_approvals"], "user:a")
    second = store.create(office_tenant, TOOL, "Tool", ["manager_approvals"], "user:b")
    key = f"msg:{uuid4()}"
    writer = SqlRouteWriter(worker_engine)
    writer.add_routes(industrial_tenant, first, TOOL, {key: None})
    writer.add_routes(office_tenant, second, TOOL, {key: None})  # skipped, not an error
    assert SqlRouteLookup(worker_engine).resolve(TOOL, key) == (industrial_tenant, first)


def test_staff_messages_join_the_conversation_once(
    owner_engine: Engine, runtime_engine: Engine, worker_engine: Engine, industrial_tenant: UUID
) -> None:
    connection_id = SqlConnectionLifecycle(runtime_engine).create(
        industrial_tenant, TOOL, "Tool", ["customer_messages"], "user:a"
    )
    recorded = SqlInboundStore(runtime_engine).record(
        SqlConnectionStore(runtime_engine).get(industrial_tenant, connection_id),  # type: ignore[arg-type]
        [CustomerMessage("own-1", "customer-7", "We can deliver Friday", from_business=True)],
        NOW,
        queue_jobs=False,
    )
    staff = SqlStaffMessages(worker_engine)
    args = (industrial_tenant, connection_id, "customer-7", recorded[0].inbox_event_id)
    assert staff.record(*args, "We can deliver Friday", NOW)
    assert not staff.record(*args, "We can deliver Friday", NOW)
    assert rows(
        owner_engine,
        industrial_tenant,
        "SELECT m.author, m.direction, i.external_id FROM conversation_messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "JOIN contact_identities i ON i.id = c.identity_id WHERE m.inbox_event_id = :e",
        e=recorded[0].inbox_event_id,
    ) == [("staff", "inbound", "customer-7")]


class NoMessagesAllowed(ChatLimits):
    """Every message is over the per-minute limit."""

    def rules(self):  # type: ignore[no-untyped-def]
        rules = super().rules()
        return type(rules)(
            RateRule("test_none", 0, timedelta(minutes=1)),
            rules.per_day,
            rules.start_per_ip,
            rules.files_per_day,
        )


def test_webhook_customer_messages_take_the_intake_road_and_limits_hold_them(
    owner_engine: Engine,
    runtime_engine: Engine,
    storage: LocalObjectStorage,
    industrial_tenant: UUID,
) -> None:
    lifecycle = SqlConnectionLifecycle(runtime_engine)
    connection_id = lifecycle.create(
        industrial_tenant, TOOL, "Tool", ["customer_messages"], "user:a"
    )
    lifecycle.apply(
        industrial_tenant, connection_id, change(status=ConnectionStatus.ACTIVE), "user:a"
    )
    connection = SqlConnectionStore(runtime_engine).get(industrial_tenant, connection_id)
    assert connection is not None

    def inbox(limits: ChatLimits) -> CustomerInbox:
        return CustomerInbox(
            SqlIntakeStore(runtime_engine),
            SqlInboundStore(runtime_engine),
            storage,
            SqlRateLimiter(runtime_engine),
            ProviderRegistry([OneClickTool(NOW)]),
            InMemorySecretStore(),
            SqlBusinessStore(runtime_engine),
            default_router(),
            limits,
            clock=lambda: NOW,
        )

    normal = inbox(ChatLimits())
    assert normal.receive(connection, CustomerMessage("m-1", "c-1", "50 fans please"))
    assert not normal.receive(connection, CustomerMessage("m-1", "c-1", "50 fans please"))
    assert normal.receive(connection, CustomerMessage("m-2", "c-2", "STOP"))
    assert inbox(NoMessagesAllowed()).receive(connection, CustomerMessage("m-3", "c-3", "spam"))
    # Phase 7: the whole message asks about orders: an order question, never an enquiry.
    assert normal.receive(connection, CustomerMessage("m-4", "c-4", "Where is my order?"))
    events = rows(owner_engine, industrial_tenant, "SELECT event_type FROM inbox_events")
    assert ("order_status_request",) in events

    jobs = rows(
        owner_engine,
        industrial_tenant,
        "SELECT kind, payload FROM jobs ORDER BY created_at",
    )
    preparations = [payload for kind, payload in jobs if kind == "prepare_enquiry"]
    assert [payload.get("acknowledge") for payload in preparations] == ["true", None]
    assert [payload.get("hold") for payload in preparations] == [None, "true"]
    assert "process_inbound" in [kind for kind, _ in jobs]  # STOP became a consent event
