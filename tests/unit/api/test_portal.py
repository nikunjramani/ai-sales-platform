"""HTTP contract of portal sign-in and the quotation/approval routes.

Real routers and services over the in-memory fakes; tests/integration repeats the key flows on
PostgreSQL.
"""

import hashlib
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sales_platform.adapters.identity.dev import DevIdentityProvider
from sales_platform.api.auth import CSRF_HEADER, SESSION_COOKIE, PortalAuth, build_auth_router
from sales_platform.api.portal import build_portal_router
from sales_platform.application.intake import EnquiryListService
from sales_platform.application.sessions import SessionService
from sales_platform.application.tenancy import TenantContextResolver
from sales_platform.domain.quotations import ApprovalAction

MANAGER = "manager@industrial.example"
SALES = "sales@industrial.example"


class NoEnquiries:
    def list_enquiries(self, tenant_id: UUID, statuses: object, limit: int) -> list[Any]:
        return []


def build_app(harness: Any, sessions: Any, directory: Any, view: Any, dev: bool = True) -> FastAPI:
    auth = PortalAuth(SessionService(sessions), TenantContextResolver(harness.db))
    app = FastAPI()
    app.include_router(
        build_auth_router(
            auth, view, DevIdentityProvider(directory) if dev else None, [MANAGER], False
        )
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
            clock=harness.clock,
        )
    )
    return app


@pytest.fixture
def client(
    harness: Any, memory_sessions: Any, memory_directory: Any, session_view: Any
) -> Iterator[TestClient]:
    with TestClient(build_app(harness, memory_sessions, memory_directory, session_view)) as http:
        yield http


def login(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/dev-login", json={"subject": subject})
    assert response.status_code == 200, response.text
    return {CSRF_HEADER: response.json()["csrf_token"]}


@pytest.fixture
def pending(harness: Any) -> tuple[UUID, Any]:
    enquiry = harness.enquiry(("IND-FAN-450", "2", "each"))
    harness.run_jobs()
    return harness.quotation(enquiry).quotation_id, harness.pending_task(enquiry)


def base(harness: Any) -> str:
    return f"/api/v1/tenants/{harness.tenant_id}"


def test_dev_login_sets_a_protected_cookie_and_returns_tenants(
    client: TestClient, harness: Any
) -> None:
    assert client.get("/api/v1/auth/config").json() == {
        "password_login": True,
        "dev_login": True,
        "dev_subjects": [MANAGER],
    }
    response = client.post("/api/v1/auth/dev-login", json={"subject": MANAGER})
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    for flag in ("HttpOnly", "SameSite=strict", "Path=/api"):
        assert flag.lower() in cookie.lower()
    body = response.json()
    assert body["subject"] == MANAGER and len(body["csrf_token"]) == 64
    assert body["tenants"][0]["roles"] == ["approver"]
    assert "quotations:approve" in body["tenants"][0]["permissions"]
    assert client.get("/api/v1/auth/session").json()["csrf_token"] == body["csrf_token"]


def test_unknown_people_and_disabled_dev_login_are_refused(
    harness: Any, memory_sessions: Any, memory_directory: Any, session_view: Any
) -> None:
    with TestClient(build_app(harness, memory_sessions, memory_directory, session_view)) as http:
        assert (
            http.post("/api/v1/auth/dev-login", json={"subject": "nobody@x.test"}).status_code
            == 401
        )
        assert http.get("/api/v1/auth/session").status_code == 401
    with TestClient(
        build_app(harness, memory_sessions, memory_directory, session_view, dev=False)
    ) as http:
        assert http.get("/api/v1/auth/config").json()["dev_login"] is False
        assert http.post("/api/v1/auth/dev-login", json={"subject": MANAGER}).status_code == 404


def test_changes_need_the_csrf_header_and_logout_ends_the_session(
    client: TestClient, harness: Any, pending: tuple[UUID, Any]
) -> None:
    headers = login(client, MANAGER)
    _, task = pending
    body = {
        "command_id": str(uuid4()),
        "action": "approve",
        "content_sha256": task.content_sha256,
    }
    url = f"{base(harness)}/approval-tasks/{task.task_id}/decision"
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json=body, headers={CSRF_HEADER: "0" * 64}).status_code == 403
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/session").status_code == 401


def test_non_members_get_not_found(client: TestClient, office_tenant: UUID) -> None:
    login(client, MANAGER)
    assert client.get(f"/api/v1/tenants/{office_tenant}/quotations").status_code == 404
    assert client.get(f"/api/v1/tenants/{uuid4()}/quotations").status_code == 404


def test_manager_reviews_and_approves_with_replay_and_conflict(
    client: TestClient, harness: Any, pending: tuple[UUID, Any]
) -> None:
    quotation_id, task = pending
    headers = login(client, MANAGER)
    listed = client.get(f"{base(harness)}/quotations", params={"status": "pending_approval"})
    assert [item["quotation_id"] for item in listed.json()] == [str(quotation_id)]
    assert listed.json()[0]["total"] == "10030.00"
    detail = client.get(f"{base(harness)}/quotations/{quotation_id}").json()
    assert detail["can_decide"] is True and detail["can_revise"] is False
    assert detail["pending_task"]["task_id"] == str(task.task_id)
    assert detail["versions"][0]["content"]["lines"][0]["sku"] == "IND-FAN-450"

    url = f"{base(harness)}/approval-tasks/{task.task_id}/decision"
    stale = {"command_id": str(uuid4()), "action": "approve", "content_sha256": "c" * 64}
    assert client.post(url, json=stale, headers=headers).status_code == 409
    body = {**stale, "content_sha256": detail["pending_task"]["content_sha256"]}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 200 and first.json()["replayed"] is False
    assert client.post(url, json=body, headers=headers).json()["replayed"] is True
    other = {**body, "command_id": str(uuid4()), "action": "reject", "reason": "late"}
    conflict = client.post(url, json=other, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["decision"]["action"] == "approve"
    missing_reason = {**other, "reason": None}
    assert client.post(url, json=missing_reason, headers=headers).status_code == 422


def test_sales_cannot_decide_but_can_revise_when_asked(
    client: TestClient, harness: Any, pending: tuple[UUID, Any]
) -> None:
    quotation_id, task = pending
    headers = login(client, SALES)
    decision = {
        "command_id": str(uuid4()),
        "action": "approve",
        "content_sha256": task.content_sha256,
    }
    url = f"{base(harness)}/approval-tasks/{task.task_id}/decision"
    assert client.post(url, json=decision, headers=headers).status_code == 403
    harness.approvals.decide(MANAGER, harness.command(task, ApprovalAction.REQUEST_REVISION, "qty"))
    harness.run_jobs()
    assert client.get(f"{base(harness)}/quotations/{quotation_id}").json()["can_revise"] is True
    revise = f"{base(harness)}/quotations/{quotation_id}/revisions"
    unpriced = {
        "command_id": str(uuid4()),
        "base_version": 1,
        "lines": [{"sku": "IND-PUMP-2HP", "quantity": "1", "unit": "each"}],
        "reason": "pump",
    }
    rejected = client.post(revise, json=unpriced, headers=headers)
    assert rejected.status_code == 422 and rejected.json()["issues"][0]["code"] == "price_missing"
    bad = {**unpriced, "lines": [{"sku": "IND-FAN-450", "quantity": "lots", "unit": "each"}]}
    assert client.post(revise, json=bad, headers=headers).status_code == 422
    good = {**unpriced, "lines": [{"sku": "IND-FAN-450", "quantity": "3", "unit": "each"}]}
    created = client.post(revise, json=good, headers=headers)
    assert created.status_code == 201 and created.json()["version"] == 2
    assert client.post(revise, json=good, headers=headers).status_code == 200  # replay
    stale = {**good, "command_id": str(uuid4())}
    assert client.post(revise, json=stale, headers=headers).status_code == 409


def assert_pdf_download(response: Any, filename_suffix: str) -> None:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;") and disposition.endswith(f'{filename_suffix}"')
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content.startswith(b"%PDF-")


def test_preview_then_approved_document_and_delivery_status(
    client: TestClient, harness: Any, pending: tuple[UUID, Any], office_tenant: UUID
) -> None:
    headers = login(client, MANAGER)
    quotation_id, task = pending
    url = f"{base(harness)}/quotations/{quotation_id}"
    assert_pdf_download(client.get(f"{url}/versions/1/preview.pdf"), "-v1-preview.pdf")
    assert client.get(f"{url}/versions/9/preview.pdf").status_code == 404
    assert client.get(f"{url}/document.pdf").status_code == 404  # not approved yet
    assert client.get(f"{url}/delivery").json() == {"document": None, "deliveries": []}

    decision = {
        "command_id": str(uuid4()),
        "action": "approve",
        "content_sha256": task.content_sha256,
    }
    decided = client.post(
        f"{base(harness)}/approval-tasks/{task.task_id}/decision", json=decision, headers=headers
    )
    assert decided.status_code == 200
    harness.run_jobs()

    document = client.get(f"{url}/document.pdf")
    assert_pdf_download(document, "-v1.pdf")
    overview = client.get(f"{url}/delivery").json()
    assert overview["document"]["sha256"] == hashlib.sha256(document.content).hexdigest()
    [delivery] = overview["deliveries"]
    assert (delivery["channel"], delivery["status"]) == ("api", "available")
    assert delivery["first_downloaded_at"] is None  # staff downloads are not customer downloads
    other = f"/api/v1/tenants/{office_tenant}/quotations/{quotation_id}/document.pdf"
    assert client.get(other).status_code == 404


def test_pdf_problems_are_reported_honestly(
    client: TestClient, harness: Any, pending: tuple[UUID, Any]
) -> None:
    login(client, MANAGER)
    quotation_id, task = pending
    url = f"{base(harness)}/quotations/{quotation_id}"
    harness.db.tenant_names[harness.tenant_id] = "Demo 工業"
    unrenderable = client.get(f"{url}/versions/1/preview.pdf")
    assert unrenderable.status_code == 422
    assert "cannot show" in unrenderable.json()["detail"]

    harness.db.tenant_names[harness.tenant_id] = "Demo Industrial"
    harness.approvals.decide(MANAGER, harness.command(task))
    harness.run_jobs()
    [(_, stored)] = harness.db.documents.values()
    harness.storage.put(stored.storage_key, b"%PDF-1.4 swapped")
    tampered = client.get(f"{url}/document.pdf")
    assert tampered.status_code == 500
    assert "integrity" in tampered.json()["detail"]


def test_staff_see_the_customer_view_and_can_withdraw(
    client: TestClient, harness: Any, pending: tuple[UUID, Any]
) -> None:
    headers = login(client, MANAGER)
    quotation_id, task = pending
    url = f"{base(harness)}/quotations/{quotation_id}"
    before = client.get(f"{url}/customer").json()
    assert before["customer_status"] is None and before["can_withdraw"] is False
    body = {"command_id": str(uuid4()), "reason": "Mistake in quantity"}
    not_issued = client.post(f"{url}/withdrawal", json=body, headers=headers)
    assert not_issued.status_code == 409

    harness.approvals.decide(MANAGER, harness.command(task))
    harness.run_jobs()
    view = client.get(f"{url}/customer").json()
    assert view["customer_status"] == "open" and view["can_withdraw"] is True
    assert view["offered_version"] == 1 and view["valid_until"]
    assert client.post(f"{url}/withdrawal", json=body).status_code == 403  # no CSRF header
    withdrawn = client.post(f"{url}/withdrawal", json=body, headers=headers)
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["response"]["outcome"] == "withdrawn"
    again = client.post(f"{url}/withdrawal", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["replayed"] is True
    view = client.get(f"{url}/customer").json()
    assert view["customer_status"] == "withdrawn" and view["can_withdraw"] is False
    assert [item["outcome"] for item in view["responses"]] == ["withdrawn"]
    assert client.get(f"{base(harness)}/quotations", params={"status": "withdrawn"}).json()
