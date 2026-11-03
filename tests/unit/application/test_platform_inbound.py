"""The platform webhook (one URL for the whole platform): verify, route by key, store (phase 5)."""

import json
from datetime import datetime
from uuid import UUID

import pytest

from sales_platform.adapters.secrets.memory import InMemorySecretStore
from sales_platform.application.inbound import HookNotFound, InboundService
from sales_platform.application.lifecycle import ConnectionLifecycle
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.connections import Connection
from sales_platform.domain.intake import ConnectionUpdate, CustomerMessage
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.ports.providers import InboundRejected, InboundRequest
from tests.unit.conftest import Job, MemoryConnections, MemoryInbox
from tests.unit.one_click import PLATFORM_SECRET, TOOL, MemoryLifecycle, OneClickTool


class Customers:
    def __init__(self) -> None:
        self.received: list[tuple[UUID, CustomerMessage]] = []

    def receive(self, connection: Connection, message: CustomerMessage) -> bool:
        self.received.append((connection.tenant_id, message))
        return True


class Platform:
    def __init__(self, now: datetime, *tenants: UUID) -> None:
        self.now = now
        self.tool = OneClickTool(now)
        self.connections = MemoryConnections(now, *tenants)
        self.store = MemoryLifecycle(self.connections, now)
        self.jobs: list[Job] = []
        self.inbox = MemoryInbox(self.connections, self.jobs)
        self.secrets = InMemorySecretStore()
        self.secrets.put_platform(TOOL, "webhook_secret", PLATFORM_SECRET)
        registry = ProviderRegistry([self.tool])
        self.customers = Customers()
        self.service = InboundService(
            self.connections,
            registry,
            self.secrets,
            self.inbox,
            customers=self.customers,
            routes=self.store,
            connections=self.connections,
        )
        self.lifecycle = ConnectionLifecycle(
            self.connections, self.store, registry, self.secrets, clock=lambda: now
        )

    def enable(self, tenant_id: UUID) -> Connection:
        context = TenantContext(tenant_id, "user:admin", frozenset({Role.ADMINISTRATOR}))
        return self.lifecycle.enable(context, TOOL, accept_costs=False).connection

    def request(self, body: object, secret: str = PLATFORM_SECRET) -> InboundRequest:
        return InboundRequest(
            headers={"x-secret": secret},
            body=json.dumps(body).encode(),
            query={},
            received_at=self.now,
        )


@pytest.fixture
def platform(now: datetime, industrial_tenant: UUID, office_tenant: UUID) -> Platform:
    return Platform(now, industrial_tenant, office_tenant)


def stored_events(platform: Platform) -> list[object]:
    return [row.event for row in platform.inbox.rows.values()]


def test_a_wrong_company_secret_is_refused_before_anything_is_read(platform: Platform) -> None:
    with pytest.raises(InboundRejected):
        platform.service.receive_platform(TOOL, platform.request({"routes": []}, "forged"))
    assert platform.tool.unrouted_calls == 0


def test_an_unknown_tool_or_one_without_platform_webhooks_is_not_found(
    platform: Platform,
) -> None:
    with pytest.raises(HookNotFound):
        platform.service.receive_platform("nothing", platform.request({}))


def test_a_request_no_business_claims_is_answered_generically_and_not_stored(
    platform: Platform,
) -> None:
    receipt = platform.service.receive_platform(TOOL, platform.request({"routes": ["bc:none"]}))
    assert (receipt.received, platform.tool.unrouted_calls) == (0, 1)
    assert stored_events(platform) == []


def test_a_link_code_routes_the_setup_event_to_its_business(
    platform: Platform, industrial_tenant: UUID
) -> None:
    connection = platform.enable(industrial_tenant)
    body = {
        "routes": ["link:CODE1"],
        "events": [
            {"type": "update", "id": "u-1", "action": "link", "data": {"owner": "acme"}},
            # Not active yet: anything but setup events is dropped.
            {"type": "message", "id": "m-1", "from": "c-1", "text": "hello"},
        ],
    }

    receipt = platform.service.receive_platform(TOOL, platform.request(body))

    assert receipt.received == 1
    assert stored_events(platform) == [ConnectionUpdate("u-1", "link", {"owner": "acme"})]
    assert platform.customers.received == []
    assert platform.jobs[-1].tenant_id == connection.tenant_id


def test_customer_messages_of_an_active_tool_are_taken_at_once(
    platform: Platform, industrial_tenant: UUID
) -> None:
    connection = platform.enable(industrial_tenant)
    platform.lifecycle.on_update(connection, ConnectionUpdate("u-1", "link", {"owner": "acme"}))
    body = {
        "routes": ["owner:acme"],
        "events": [{"type": "message", "id": "m-1", "from": "c-1", "text": "50 fans"}],
    }

    platform.service.receive_platform(TOOL, platform.request(body))

    assert [(tenant, message.text) for tenant, message in platform.customers.received] == [
        (industrial_tenant, "50 fans")
    ]


def test_a_key_only_reaches_its_own_business(
    platform: Platform, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    industrial = platform.enable(industrial_tenant)
    office = platform.enable(office_tenant)
    platform.lifecycle.on_update(industrial, ConnectionUpdate("u-1", "link", {"owner": "a"}))
    platform.lifecycle.on_update(office, ConnectionUpdate("u-2", "link", {"owner": "b"}))
    body = {
        "routes": ["owner:b"],
        "events": [{"type": "message", "id": "m-1", "from": "c-1", "text": "hi"}],
    }

    platform.service.receive_platform(TOOL, platform.request(body))

    assert [tenant for tenant, _ in platform.customers.received] == [office_tenant]


def test_an_unreadable_body_is_acknowledged_without_storing(
    platform: Platform, industrial_tenant: UUID
) -> None:
    platform.enable(industrial_tenant)
    body = {"routes": ["link:CODE1"], "events": [{"type": "strange"}]}
    receipt = platform.service.receive_platform(TOOL, platform.request(body))
    assert receipt.received == 0 and stored_events(platform) == []


def test_a_replayed_setup_event_is_stored_once(platform: Platform, industrial_tenant: UUID) -> None:
    platform.enable(industrial_tenant)
    body = {
        "routes": ["link:CODE1"],
        "events": [{"type": "update", "id": "u-1", "action": "link", "data": {"owner": "a"}}],
    }
    platform.service.receive_platform(TOOL, platform.request(body))
    again = platform.service.receive_platform(TOOL, platform.request(body))
    assert (again.received, again.duplicates) == (1, 1)
    assert len(stored_events(platform)) == 1
