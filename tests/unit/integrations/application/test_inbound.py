"""Generic inbound: token and signature checks first, tenant from the connection, dedupe, routes."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.integrations.application.inbound import (
    EventTooLarge,
    HookNotFound,
    InboundService,
)
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.integrations.domain.events import ConsentChange, InboundEvent, ManagerAction
from sales_platform.integrations.ports.providers import (
    InboundRejected,
    InboundRequest,
    InvalidInbound,
)
from sales_platform.platform.ports.secrets import SecretNotFound
from sales_platform.providers.simulated import SimulatedProvider
from sales_platform.providers.simulated.signing import SIGNATURE_HEADER, sign

SECRET = "unit-signing-secret"
TOKEN = "unit-hook-token"
MANAGER = "manager@industrial.example"


class DictSecrets:
    def __init__(self) -> None:
        self.values: dict[tuple[UUID, str], str] = {}

    def put(self, tenant_id: UUID, name: str, value: str) -> str:
        reference = f"local:{uuid4()}"
        self.values[(tenant_id, reference)] = value
        return reference

    def get(self, tenant_id: UUID, reference: str) -> str:
        try:
            return self.values[(tenant_id, reference)]
        except KeyError:
            raise SecretNotFound("Secret not found") from None

    def delete(self, tenant_id: UUID, reference: str) -> None:
        self.values.pop((tenant_id, reference), None)

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        raise NotImplementedError

    def get_platform(self, provider_key: str, name: str) -> str:
        raise NotImplementedError


class SpyProvider(SimulatedProvider):
    """The simulated provider, counting parses and optionally returning fixed events."""

    def __init__(self, events: Sequence[InboundEvent] | None = None) -> None:
        super().__init__(Path("unused"))
        self.parsed = 0
        self._events = events

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        self.parsed += 1
        return self._events if self._events is not None else super().parse_inbound(conn, request)


def phone(harness: Any, secrets: DictSecrets, tenant_id: UUID | None = None) -> Connection:
    """A simulated connection with a webhook token and signing secret."""
    tenant = tenant_id or harness.tenant_id
    base = harness.connections.find_by_account(tenant, "simulated", "simulated-local")
    connection = replace(base, secret_refs={"signing_secret": secrets.put(tenant, "s", SECRET)})
    added: Connection = harness.connections.add(
        connection, hashlib.sha256(TOKEN.encode()).hexdigest()
    )
    return added


def request(body: dict[str, object], secret: str = SECRET) -> InboundRequest:
    raw = json.dumps(body).encode()
    return InboundRequest({SIGNATURE_HEADER.lower(): sign(secret, raw)}, raw, {}, datetime.now(UTC))


def service(harness: Any, secrets: DictSecrets, provider: SimulatedProvider) -> InboundService:
    return InboundService(harness.connections, ProviderRegistry([provider]), secrets, harness.inbox)


def enroll_body(harness: Any, event_id: str = "evt-1") -> dict[str, object]:
    code = harness.channels.start_enrollment(harness.context(MANAGER), "simulated").code
    return {"event_id": event_id, "external_user_id": "phone", "type": "enroll", "code": code}


def test_a_signed_event_is_stored_once_and_routed_by_a_job(harness: Any) -> None:
    secrets = DictSecrets()
    connection = phone(harness, secrets)
    inbound = service(harness, secrets, SimulatedProvider(Path("unused")))
    body = enroll_body(harness)
    assert inbound.receive(connection.connection_id, TOKEN, request(body)).received == 1
    again = inbound.receive(connection.connection_id, TOKEN, request(body))
    assert (again.received, again.duplicates) == (1, 1)
    assert harness.run_jobs() == 1  # one process_inbound job, not two
    [row] = harness.inbox.rows.values()
    assert row.outcome == "enrolled"
    [binding] = harness.channels.bindings(harness.context(MANAGER))
    assert (binding.external_user_id, binding.connection_id) == ("phone", "simulated-local")


def test_wrong_token_unknown_or_disabled_connection_all_look_the_same(harness: Any) -> None:
    secrets = DictSecrets()
    connection = phone(harness, secrets)
    inbound = service(harness, secrets, SimulatedProvider(Path("unused")))
    body = request(enroll_body(harness))
    for connection_id, token in ((connection.connection_id, "wrong"), (uuid4(), TOKEN)):
        with pytest.raises(HookNotFound, match="Not found"):
            inbound.receive(connection_id, token, body)
    harness.connections.add(
        replace(connection, status=ConnectionStatus.DISABLED),
        harness.connections.token_hashes[connection.connection_id],
    )
    with pytest.raises(HookNotFound, match="Not found"):
        inbound.receive(connection.connection_id, TOKEN, body)
    without_provider = InboundService(
        harness.connections, ProviderRegistry([]), secrets, harness.inbox
    )
    with pytest.raises(HookNotFound):
        without_provider.receive(connection.connection_id, TOKEN, body)
    assert harness.inbox.rows == {}


def test_the_signature_is_checked_before_parsing(harness: Any) -> None:
    secrets = DictSecrets()
    connection = phone(harness, secrets)
    spy = SpyProvider()
    inbound = service(harness, secrets, spy)
    forged = request(enroll_body(harness), secret="attacker")
    with pytest.raises(InboundRejected):
        inbound.receive(connection.connection_id, TOKEN, forged)
    unsigned = replace(forged, headers={})
    with pytest.raises(InboundRejected):
        inbound.receive(connection.connection_id, TOKEN, unsigned)
    assert spy.parsed == 0 and harness.inbox.rows == {}


def test_size_and_shape_limits(harness: Any) -> None:
    secrets = DictSecrets()
    connection = phone(harness, secrets)
    inbound = service(harness, secrets, SimulatedProvider(Path("unused")))
    with pytest.raises(EventTooLarge):
        inbound.receive(connection.connection_id, TOKEN, request({"pad": "x" * 20_000}))
    # A body naming a business is not accepted: the business comes only from the connection.
    named = {**enroll_body(harness), "tenant_id": str(harness.tenant_id)}
    with pytest.raises(InvalidInbound):
        inbound.receive(connection.connection_id, TOKEN, request(named))


def test_the_business_is_always_the_connections(harness: Any, office_tenant: UUID) -> None:
    secrets = DictSecrets()
    office_phone = phone(harness, secrets, office_tenant)
    inbound = service(harness, secrets, SimulatedProvider(Path("unused")))
    # An industrial manager's code sent through the office business's hook is not found there.
    inbound.receive(office_phone.connection_id, TOKEN, request(enroll_body(harness)))
    harness.run_jobs()
    [row] = harness.inbox.rows.values()
    assert row.outcome == "rejected"  # final: no retry, the job finished
    assert harness.channels.bindings(harness.context(MANAGER)) == []


def test_same_event_id_with_another_type_is_a_new_event(harness: Any) -> None:
    secrets = DictSecrets()
    connection = phone(harness, secrets)
    events: list[InboundEvent] = [
        ManagerAction("same-id", "phone", "enroll", code="X"),
        ConsentChange("same-id", "phone", opted_in=False),
    ]
    inbound = service(harness, secrets, SpyProvider(events))
    receipt = inbound.receive(connection.connection_id, TOKEN, request({}))
    assert (receipt.received, receipt.duplicates) == (2, 0)
    harness.run_jobs()
    outcomes = sorted(row.outcome for row in harness.inbox.rows.values())
    assert outcomes == ["rejected", "unsupported"]


def test_events_on_a_connection_disabled_before_routing_are_not_acted_on(harness: Any) -> None:
    secrets = DictSecrets()
    connection = phone(harness, secrets)
    inbound = service(harness, secrets, SimulatedProvider(Path("unused")))
    inbound.receive(connection.connection_id, TOKEN, request(enroll_body(harness)))
    harness.connections.add(replace(connection, status=ConnectionStatus.DISABLED))
    harness.run_jobs()
    [row] = harness.inbox.rows.values()
    assert row.outcome == "connection not active"
    assert harness.channels.bindings(harness.context(MANAGER)) == []


def test_purposes_do_not_change_routing(harness: Any) -> None:
    """Routing is by event type: a manager action is handled the same on any connection."""
    secrets = DictSecrets()
    connection = phone(harness, secrets)
    harness.connections.add(
        replace(connection, purposes=frozenset({Purpose.MANAGER_APPROVALS})),
        harness.connections.token_hashes[connection.connection_id],
    )
    inbound = service(harness, secrets, SimulatedProvider(Path("unused")))
    inbound.receive(connection.connection_id, TOKEN, request(enroll_body(harness)))
    harness.run_jobs()
    assert [row.outcome for row in harness.inbox.rows.values()] == ["enrolled"]
