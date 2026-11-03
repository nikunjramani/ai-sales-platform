"""HTTP answers of the platform webhook route (phase 5): forged, unclaimed, replayed, handshake."""

import json
from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sales_platform.adapters.secrets.memory import InMemorySecretStore
from sales_platform.api.hooks import build_hook_router
from sales_platform.application.inbound import InboundService
from sales_platform.application.lifecycle import ConnectionLifecycle
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.tenancy import Role, TenantContext
from tests.unit.conftest import Job, MemoryConnections, MemoryInbox
from tests.unit.one_click import PLATFORM_SECRET, TOOL, MemoryLifecycle, OneClickTool

URL = f"/api/v1/hooks/p/{TOOL}"


@pytest.fixture
def setup(now: datetime, industrial_tenant: UUID) -> Iterator[tuple[TestClient, OneClickTool]]:
    tool = OneClickTool(now)
    connections = MemoryConnections(now, industrial_tenant)
    store = MemoryLifecycle(connections, now)
    secrets = InMemorySecretStore()
    secrets.put_platform(TOOL, "webhook_secret", PLATFORM_SECRET)
    registry = ProviderRegistry([tool])
    lifecycle = ConnectionLifecycle(connections, store, registry, secrets, clock=lambda: now)
    admin = TenantContext(industrial_tenant, "user:admin", frozenset({Role.ADMINISTRATOR}))
    lifecycle.enable(admin, TOOL, accept_costs=False)  # route link:CODE1
    jobs: list[Job] = []
    service = InboundService(
        connections,
        registry,
        secrets,
        MemoryInbox(connections, jobs),
        routes=store,
        connections=connections,
    )
    app = FastAPI()
    app.include_router(build_hook_router(service))
    with TestClient(app) as client:
        yield client, tool


def body(routes: list[str]) -> bytes:
    events = [{"type": "update", "id": "u-1", "action": "link", "data": {"owner": "a"}}]
    return json.dumps({"routes": routes, "events": events}).encode()


def test_a_forged_or_missing_secret_is_refused_before_anything_is_read(
    setup: tuple[TestClient, OneClickTool],
) -> None:
    client, tool = setup
    for headers in ({}, {"x-secret": "forged"}):
        answer = client.post(URL, content=body(["link:CODE1"]), headers=headers)
        assert (answer.status_code, answer.json()) == (401, {"detail": "Invalid signature"})
    assert tool.unrouted_calls == 0


def test_unknown_tools_and_per_connection_tools_have_no_platform_route(
    setup: tuple[TestClient, OneClickTool],
) -> None:
    client, _ = setup
    answer = client.post("/api/v1/hooks/p/nothing", content=b"{}")
    assert answer.status_code == 404


def test_genuine_requests_are_answered_200_even_when_no_business_claims_them(
    setup: tuple[TestClient, OneClickTool],
) -> None:
    client, tool = setup
    answer = client.post(URL, content=body(["link:WRONG"]), headers={"x-secret": PLATFORM_SECRET})
    assert (answer.status_code, answer.json()) == (200, {"received": 0, "duplicates": 0})
    assert tool.unrouted_calls == 1
    unreadable = client.post(URL, content=b"not json", headers={"x-secret": PLATFORM_SECRET})
    assert unreadable.status_code == 200  # acknowledged, so the tool does not retry it


def test_a_replay_is_stored_once(setup: tuple[TestClient, OneClickTool]) -> None:
    client, _ = setup
    headers = {"x-secret": PLATFORM_SECRET}
    first = client.post(URL, content=body(["link:CODE1"]), headers=headers)
    again = client.post(URL, content=body(["link:CODE1"]), headers=headers)
    assert first.json() == {"received": 1, "duplicates": 0}
    assert again.json() == {"received": 1, "duplicates": 1}


def test_a_get_without_a_handshake_is_not_found(setup: tuple[TestClient, OneClickTool]) -> None:
    client, _ = setup
    assert client.get(URL).status_code == 404
