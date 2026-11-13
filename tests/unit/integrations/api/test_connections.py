"""Portal connection routes: who sees what (P1-Q8), per-connection linking codes, isolation."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sales_platform.accounts.api.auth import build_auth_router
from sales_platform.enquiries.application.intake import EnquiryListService
from sales_platform.integrations.api.connections import build_connection_router
from sales_platform.integrations.application.connections import ConnectionService
from sales_platform.integrations.domain.connections import Purpose
from sales_platform.platform.adapters.identity.dev import DevIdentityProvider
from sales_platform.platform.api.auth import CSRF_HEADER, PortalAuth
from sales_platform.platform.api.errors import add_error_handlers
from sales_platform.platform.application.sessions import SessionService
from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.domain.tenancy import Role
from sales_platform.quotations.api.enrollments import build_enrollment_router
from sales_platform.quotations.api.portal import build_portal_router
from sales_platform.quotations.application.channel_actions import ChannelEvent, ChannelEventKind

ADMIN = "admin@industrial.example"
MANAGER = "manager@industrial.example"
SALES = "sales@industrial.example"


class NoEnquiries:
    def list_enquiries(self, tenant_id: UUID, statuses: object, limit: int) -> list[Any]:
        return []


@pytest.fixture
def client(
    harness: Any, memory_sessions: Any, memory_directory: Any, session_view: Any
) -> Iterator[TestClient]:
    harness.db.add_member(harness.tenant_id, ADMIN, Role.ADMINISTRATOR)
    auth = PortalAuth(SessionService(memory_sessions), TenantContextResolver(harness.db))
    app = FastAPI()
    add_error_handlers(app)
    app.include_router(
        build_auth_router(auth, session_view, DevIdentityProvider(memory_directory), [], False)
    )
    app.include_router(
        build_portal_router(
            auth,
            harness.quotations,
            harness.approvals,
            harness.channels,
            EnquiryListService(NoEnquiries()),
            harness.documents(),
            harness.responses(),
            harness.connections,
        )  # fmt: skip
    )
    app.include_router(
        build_connection_router(
            auth,
            ConnectionService(harness.connections, harness.registry),
            "http://localhost:8000",
        )
    )
    app.include_router(build_enrollment_router(auth, harness.channels))
    with TestClient(app) as http:
        yield http


def login(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/dev-login", json={"subject": subject})
    assert response.status_code == 200, response.text
    return {CSRF_HEADER: response.json()["csrf_token"]}


def base(harness: Any) -> str:
    return f"/api/v1/tenants/{harness.tenant_id}"


def test_administrators_see_every_connection_approvers_only_linkable_ones(
    client: TestClient, harness: Any
) -> None:
    login(client, ADMIN)
    listed = client.get(f"{base(harness)}/connections").json()
    assert sorted(item["provider_key"] for item in listed) == ["api", "email", "simulated"]
    assert all(set(item) >= {"provider_name", "purposes", "status"} for item in listed)
    assert not any({"config", "secret_refs", "webhook_token_sha256"} & set(item) for item in listed)
    catalogue = client.get(f"{base(harness)}/connections", params={"purpose": "catalogue"}).json()
    assert [item["provider_key"] for item in catalogue] == ["simulated"]

    simulated = harness.connections.find_by_account(
        harness.tenant_id, "simulated", "simulated-local"
    )
    harness.connections.add(replace(simulated, last_error="signature key rotated"))
    login(client, MANAGER)
    [visible] = client.get(f"{base(harness)}/connections").json()
    assert (visible["provider_key"], visible["last_error"]) == ("simulated", None)

    login(client, SALES)
    assert client.get(f"{base(harness)}/connections").status_code == 403


def test_a_code_from_one_connection_only_links_there(client: TestClient, harness: Any) -> None:
    headers = login(client, MANAGER)
    phone = harness.connections.find_by_account(harness.tenant_id, "simulated", "simulated-local")
    created = client.post(
        f"{base(harness)}/connections/{phone.connection_id}/enrollments", headers=headers
    )
    assert created.status_code == 201
    assert created.json()["channel"] == "simulated"
    code = created.json()["code"]
    [enrollment] = harness.db.enrollments.values()
    assert enrollment.channel == "simulated"
    binding = harness.channels.handle(
        ChannelEvent(
            "simulated",
            "simulated-local",
            harness.tenant_id,
            "e1",
            "phone",
            ChannelEventKind.ENROLL,
            code=code,
        )  # fmt: skip
    )
    assert binding.external_user_id == "phone"
    bindings = client.get(f"{base(harness)}/channel-bindings").json()
    assert bindings[0]["connection_ref"] == str(phone.connection_id)
    assert bindings[0]["connection_name"] == phone.display_name

    api = harness.connections.find_by_account(
        harness.tenant_id, "api", "api-key:00000000-0000-4000-8000-0000000000c1"
    )
    not_for_approvals = client.post(
        f"{base(harness)}/connections/{api.connection_id}/enrollments", headers=headers
    )
    assert not_for_approvals.status_code == 422
    for unknown in (uuid4(), _office_phone(harness)):  # unknown, or another business's
        assert (
            client.post(f"{base(harness)}/connections/{unknown}/enrollments", headers=headers)
        ).status_code == 404


def test_the_old_request_checks_providers_and_connections(client: TestClient, harness: Any) -> None:
    headers = login(client, MANAGER)
    url = f"{base(harness)}/channel-enrollments"
    assert client.post(url, json={"channel": "simulated"}, headers=headers).status_code == 201
    assert client.post(url, json={"channel": "telegram"}, headers=headers).status_code == 422
    assert client.post(url, json={"channel": "api"}, headers=headers).status_code == 422


def _office_phone(harness: Any) -> UUID:
    rows = [
        row
        for row in harness.connections.rows.values()
        if row.tenant_id != harness.tenant_id and Purpose.MANAGER_APPROVALS in row.purposes
    ]
    connection_id: UUID = rows[0].connection_id
    return connection_id
