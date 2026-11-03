"""HTTP contract of the enquiry API, using the real router and services over in-memory fakes."""

import base64
import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from sales_platform.accounts.application.api_keys import ApiKeyAuthenticator, generate_api_key
from sales_platform.accounts.ports.api_keys import ApiKeyRecord
from sales_platform.catalogue.ports.erp import ErpError, ErpErrorCode
from sales_platform.customer_channels.api.enquiries import MAX_REQUEST_BYTES, build_enquiry_router
from sales_platform.enquiries.application.intake import EnquiryIntakeService
from sales_platform.platform.domain.tenancy import Role

BODY = {"text": "Please quote 50 industrial fans for Pune", "customer_reference": "CUST-1"}


CLIENT_KEY = UUID("00000000-0000-4000-8000-0000000000c1")  # the harness API client


@pytest.fixture
def client(
    api_key_reader: Any,
    intake_store: Any,
    memory_storage: Any,
    connection_store: Any,
    provider_registry: Any,
    now: datetime,
    harness: Any,
) -> Iterator[TestClient]:
    app = FastAPI()
    intake = EnquiryIntakeService(
        intake_store, memory_storage, connection_store, provider_registry, clock=lambda: now
    )
    app.include_router(
        build_enquiry_router(
            ApiKeyAuthenticator(api_key_reader), intake, harness.documents(), harness.responses()
        )
    )
    with TestClient(app) as http:
        yield http


@pytest.fixture
def make_headers(api_key_reader: Any) -> Any:
    def build(
        tenant: UUID,
        role: Role = Role.INTAKE_SERVICE,
        key: str = "req-000001",
        key_id: UUID | None = None,
    ) -> dict[str, str]:
        """A new key; key_id=CLIENT_KEY is the harness's API client (its quotations' recipient)."""
        new = generate_api_key()
        if key_id is not None:
            token = f"qk_{key_id.hex}_{new.token.split('_', 2)[2]}"
            new = replace(new, key_id=key_id, token=token)
        api_key_reader.records[new.key_id] = ApiKeyRecord(
            new.key_id, tenant, role, new.secret_sha256, False
        )
        return {"Authorization": f"Bearer {new.token}", "Idempotency-Key": key}

    return build


def test_accept_then_replay_then_conflict(
    client: TestClient, make_headers: Any, industrial_tenant: UUID
) -> None:
    headers = make_headers(industrial_tenant)
    first = client.post("/api/v1/enquiries", json=BODY, headers=headers)
    assert first.status_code == 202
    accepted = first.json()
    assert accepted["status"] == "received" and accepted["duplicate"] is False
    assert first.headers["X-Correlation-ID"] == accepted["correlation_id"]

    replay = client.post("/api/v1/enquiries", json=BODY, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == {**accepted, "duplicate": True}

    changed = client.post("/api/v1/enquiries", json={**BODY, "text": "other"}, headers=headers)
    assert changed.status_code == 409


def test_status_lookup_is_tenant_scoped(
    client: TestClient, make_headers: Any, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    attachment = {
        "filename": "spec.txt",
        "content_type": "text/plain",
        "content_base64": base64.b64encode(b"50 fans, 450 mm").decode(),
    }
    submitter = make_headers(industrial_tenant)
    created = client.post(
        "/api/v1/enquiries", json={**BODY, "attachments": [attachment]}, headers=submitter
    ).json()
    url = f"/api/v1/enquiries/{created['enquiry_id']}"
    own = client.get(url, headers=submitter)
    assert own.status_code == 200
    assert own.json()["channel"] == "api"  # pinned: phase 1 keeps the provider key here
    assert own.json()["attachments"][0]["status"] == "quarantined"
    assert client.get(url, headers=make_headers(office_tenant, Role.VIEWER)).status_code == 404
    # Another key of the same business sees only its own enquiries (P1-Q9).
    assert client.get(url, headers=make_headers(industrial_tenant)).status_code == 404


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer qk_bad", "Basic dXNlcjpwYXNz", "Bearer " + "qk_" + "0" * 32 + "_x"],
)
def test_bad_credentials_get_401(client: TestClient, authorization: str | None) -> None:
    headers = {"Idempotency-Key": "req-000001"}
    if authorization:
        headers["Authorization"] = authorization
    response = client.post("/api/v1/enquiries", json=BODY, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_tenant_field_in_body_is_rejected_not_trusted(
    client: TestClient, make_headers: Any, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    body = {**BODY, "tenant_id": str(office_tenant)}
    response = client.post("/api/v1/enquiries", json=body, headers=make_headers(industrial_tenant))
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"text": ""}, 422),
        (
            {
                "attachments": [
                    {
                        "filename": "a.pdf",
                        "content_type": "application/pdf",
                        "content_base64": "not base64!",
                    }
                ]
            },
            422,
        ),
        ({}, 403),
    ],
)
def test_validation_and_permission_errors(
    client: TestClient,
    make_headers: Any,
    industrial_tenant: UUID,
    change: dict[str, object],
    expected: int,
) -> None:
    role = Role.VIEWER if expected == 403 else Role.INTAKE_SERVICE
    response = client.post(
        "/api/v1/enquiries", json={**BODY, **change}, headers=make_headers(industrial_tenant, role)
    )
    assert response.status_code == expected


def test_missing_or_short_idempotency_key(
    client: TestClient, make_headers: Any, industrial_tenant: UUID
) -> None:
    headers = make_headers(industrial_tenant)
    del headers["Idempotency-Key"]
    assert client.post("/api/v1/enquiries", json=BODY, headers=headers).status_code == 422
    headers["Idempotency-Key"] = "short"
    assert client.post("/api/v1/enquiries", json=BODY, headers=headers).status_code == 422


def test_oversized_request_is_refused_before_parsing(
    client: TestClient, make_headers: Any, industrial_tenant: UUID
) -> None:
    headers = {
        **make_headers(industrial_tenant),
        "Content-Length": str(MAX_REQUEST_BYTES + 1),
        "Content-Type": "application/json",
    }
    response = client.post("/api/v1/enquiries", content=b"{}", headers=headers)
    assert response.status_code == 413


def test_storage_failure_is_503_and_nothing_is_accepted(
    client: TestClient, make_headers: Any, industrial_tenant: UUID, intake_store: Any
) -> None:
    intake_store.fail_record = OperationalError("insert", {}, Exception("connection lost"))
    response = client.post("/api/v1/enquiries", json=BODY, headers=make_headers(industrial_tenant))
    assert response.status_code == 503
    assert "retry with the same Idempotency-Key" in response.json()["detail"]
    assert intake_store.receipts == {}


def test_customers_see_only_an_issued_quotation(
    client: TestClient,
    make_headers: Any,
    harness: Any,
    industrial_tenant: UUID,
    office_tenant: UUID,
) -> None:
    enquiry = harness.enquiry(("IND-FAN-450", "2", "each"))
    harness.run_jobs()
    headers = make_headers(industrial_tenant, key_id=CLIENT_KEY)
    url = f"/api/v1/enquiries/{enquiry}/quotation"
    waiting = client.get(url, headers=headers)
    assert waiting.status_code == 404  # drafted but not approved: invisible to the customer
    assert waiting.json()["detail"] == "No quotation has been issued for this enquiry yet"
    assert client.get(f"{url}.pdf", headers=headers).status_code == 404

    task = harness.pending_task(enquiry)
    harness.approvals.decide("manager@industrial.example", harness.command(task))
    harness.run_jobs()

    issued = client.get(url, headers=headers)
    assert issued.status_code == 200, issued.text
    body = issued.json()
    version = harness.db.versions[-1].view.content
    assert body["version"] == 1 and body["total"] == version["total"]
    assert body["quotation_number"].startswith("Q-") and body["currency"] == version["currency"]
    assert set(body["lines"][0]) == {
        "sku", "description", "quantity", "unit", "unit_price", "line_total"
    }  # fmt: skip
    assert body["document"]["download_path"] == f"{url}.pdf"

    pdf = client.get(f"{url}.pdf", headers=headers)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["cache-control"] == "no-store"
    assert hashlib.sha256(pdf.content).hexdigest() == body["document"]["sha256"]
    [(_, delivery)] = harness.db.delivery_rows.values()
    assert delivery.first_downloaded_at is not None

    assert client.get(url, headers=make_headers(office_tenant)).status_code == 404
    other_key = make_headers(industrial_tenant)  # same business, another key (P1-Q9)
    assert client.get(url, headers=other_key).status_code == 404
    assert client.get(f"{url}.pdf", headers=other_key).status_code == 404
    assert client.get(url).status_code == 401


def issue_through_harness(harness: Any) -> UUID:
    enquiry: UUID = harness.enquiry(("IND-FAN-450", "2", "each"))
    harness.run_jobs()
    harness.approvals.decide(
        "manager@industrial.example", harness.command(harness.pending_task(enquiry))
    )
    harness.run_jobs()
    return enquiry


def test_customer_accepts_or_is_told_why_not(
    client: TestClient, make_headers: Any, harness: Any, industrial_tenant: UUID
) -> None:
    enquiry = issue_through_harness(harness)
    headers = make_headers(industrial_tenant, key="accept-0001", key_id=CLIENT_KEY)
    issued = client.get(f"/api/v1/enquiries/{enquiry}/quotation", headers=headers).json()
    assert issued["status"] == "open" and issued["valid_until"]
    url = issued["response_path"]
    body = {"action": "accept", "version": issued["version"], "content_sha256": "f" * 64}
    stale = client.post(url, json=body, headers=headers)
    assert stale.status_code == 409  # not the offered content

    body["content_sha256"] = issued["content_sha256"]
    accepted = client.post(url, json=body, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["outcome"] == "accepted" and accepted.json()["replayed"] is False
    replay = client.post(url, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    other_request = client.post(
        url,
        json=body,
        headers={**headers, "Idempotency-Key": "accept-0002"},  # same key, new request
    )
    assert other_request.status_code == 409
    assert other_request.json()["detail"]["status"] == "accepted"
    other_key = client.post(url, json=body, headers=make_headers(industrial_tenant, key="x-0002"))
    assert other_key.status_code == 404  # not this key's quotation
    after = client.get(f"/api/v1/enquiries/{enquiry}/quotation", headers=headers).json()
    assert after["status"] == "accepted"
    no_key = {k: v for k, v in headers.items() if k != "Idempotency-Key"}
    assert client.post(url, json=body, headers=no_key).status_code == 422


def test_changed_prices_and_erp_failures_are_explained(
    client: TestClient, make_headers: Any, harness: Any, industrial_tenant: UUID
) -> None:
    enquiry = issue_through_harness(harness)
    headers = make_headers(industrial_tenant, key="accept-0003", key_id=CLIENT_KEY)
    issued = client.get(f"/api/v1/enquiries/{enquiry}/quotation", headers=headers).json()
    body = {"action": "accept", "version": 1, "content_sha256": issued["content_sha256"]}
    harness.erp.failures["get_prices"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    unavailable = client.post(issued["response_path"], json=body, headers=headers)
    assert unavailable.status_code == 503 and "nothing was recorded" in unavailable.json()["detail"]
    del harness.erp.failures["get_prices"]

    catalogue = harness.erp.catalogue(industrial_tenant)
    [price] = catalogue.prices["IND-FAN-450"][-1:]
    catalogue.prices["IND-FAN-450"] = (replace(price, unit_price=Decimal("4500.00")),)
    refused = client.post(issued["response_path"], json=body, headers=headers)
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["outcome"] == "refused_changed" and detail["revised_version"] == 2
    assert detail["differences"]
    status_now = client.get(f"/api/v1/enquiries/{enquiry}/quotation", headers=headers).json()
    assert status_now["status"] == "being_revised"


def test_decline_needs_the_respond_permission(
    client: TestClient, make_headers: Any, harness: Any, industrial_tenant: UUID
) -> None:
    enquiry = issue_through_harness(harness)
    # The same client key, first with a role that may only look.
    viewer = make_headers(industrial_tenant, Role.VIEWER, key="decline-001", key_id=CLIENT_KEY)
    issued = client.get(f"/api/v1/enquiries/{enquiry}/quotation", headers=viewer).json()
    body = {
        "action": "decline",
        "version": 1,
        "content_sha256": issued["content_sha256"],
        "reason": "Too expensive",
    }
    assert client.post(issued["response_path"], json=body, headers=viewer).status_code == 403
    customer = make_headers(industrial_tenant, key="decline-002", key_id=CLIENT_KEY)
    declined = client.post(issued["response_path"], json=body, headers=customer)
    assert declined.status_code == 200 and declined.json()["outcome"] == "declined"
