"""Phase 3 end to end on PostgreSQL: the local demo business quotes from its own product list
through the real API, the real worker and each business's own product source (no fixed
reader). Covers the plan's live-run checks: evidence, a price change at acceptance, an old
stock count and an archived product."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from sales_platform.api_app import create_app
from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.enquiries.adapters.interpreters.rules import RulesInterpreter
from sales_platform.enquiries.persistence.workflow import open_checkpointer
from sales_platform.platform.api.auth import CSRF_HEADER
from sales_platform.platform.application.jobs import JobRunner
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.tenancy import Role
from sales_platform.platform.persistence.jobs import SqlJobQueue
from sales_platform.seed import LOCAL_TENANT

pytestmark = pytest.mark.integration

type ApiKeyFactory = Callable[[UUID, Role], str]
BASE = f"/api/v1/tenants/{LOCAL_TENANT}"


@pytest.fixture
def client(integration_settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(integration_settings)) as app:
        yield app


def drain(settings: Settings, engine: Engine) -> None:
    """The worker exactly as it runs: every business through its own product source."""
    with open_checkpointer(settings.worker_url()) as checkpointer:
        handlers = build_job_handlers(
            settings, engine, build_runtime(settings, engine), checkpointer, RulesInterpreter()
        )
        runner = JobRunner(SqlJobQueue(engine), handlers)
        while runner.run_once():
            pass


def login(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/dev-login", json={"subject": subject})
    assert response.status_code == 200, response.text
    return {CSRF_HEADER: response.json()["csrf_token"]}


def submit(client: TestClient, token: str, text: str) -> str:
    response = client.post(
        "/api/v1/enquiries",
        json={"text": text},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"k-{uuid4()}"},
    )
    assert response.status_code == 202, response.text
    return str(response.json()["enquiry_id"])


def product_id(client: TestClient, sku: str) -> str:
    items = client.get(f"{BASE}/catalogue/products", params={"q": sku}).json()["items"]
    return str(next(item["product_id"] for item in items if item["sku"] == sku))


def pending(client: TestClient) -> dict[str, Any]:
    [listed] = client.get(f"{BASE}/quotations", params={"status": "pending_approval"}).json()
    detail: dict[str, Any] = client.get(f"{BASE}/quotations/{listed['quotation_id']}").json()
    return detail


def test_quote_from_our_own_list_then_a_price_change_revises_it(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    integration_settings: Settings,
    worker_engine: Engine,
) -> None:
    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    auth = {"Authorization": f"Bearer {token}"}
    enquiry_id = submit(client, token, "Please quote 2 x IND-FAN-450")
    drain(integration_settings, worker_engine)

    headers = login(client, "manager@local.example")
    detail = pending(client)
    line = detail["versions"][-1]["content"]["lines"][0]
    assert (line["sku"], line["unit_price"], line["line_total"]) == (
        "IND-FAN-450",
        "4250.00",
        "8500.00",
    )
    assert (line["price_list_id"], line["price_list_version"], line["price_source"]) == (
        "standard",
        "2",
        "local-catalogue",
    )
    assert line["stock_status"] == "available"
    task = detail["pending_task"]
    decision = client.post(
        f"{BASE}/approval-tasks/{task['task_id']}/decision",
        json={
            "command_id": str(uuid4()),
            "action": "approve",
            "content_sha256": task["content_sha256"],
        },
        headers=headers,
    )
    assert decision.status_code == 200, decision.text
    drain(integration_settings, worker_engine)
    issued = client.get(f"/api/v1/enquiries/{enquiry_id}/quotation", headers=auth).json()
    assert issued["status"] == "open"

    # Staff raise the price in the portal; the customer then accepts the old version.
    sales = login(client, "sales@local.example")
    fan = product_id(client, "IND-FAN-450")
    raised = client.post(
        f"{BASE}/catalogue/products/{fan}/prices",
        json={"unit_price": "4400", "command_id": str(uuid4())},
        headers=sales,
    )
    assert raised.status_code == 201, raised.text
    body = {"action": "accept", "version": 1, "content_sha256": issued["content_sha256"]}
    answer = client.post(
        issued["response_path"], json=body, headers={**auth, "Idempotency-Key": f"a-{uuid4()}"}
    )
    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["revised_version"] == 2
    drain(integration_settings, worker_engine)
    login(client, "manager@local.example")
    revised = pending(client)
    assert revised["pending_task"]["version"] == 2
    assert revised["versions"][-1]["content"]["lines"][0]["unit_price"] == "4400.00"


def test_old_counts_are_flagged_and_archived_products_are_not_quoted(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    integration_settings: Settings,
    worker_engine: Engine,
) -> None:
    sales = login(client, "sales@local.example")
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    # A product whose only stock count is 8 days old (the local demo allows 7 days, Q10).
    created = client.post(
        f"{BASE}/catalogue/products",
        json={"sku": "IND-VALVE-1", "name": "Industrial ball valve", "unit": "each"},
        headers=sales,
    ).json()
    path = f"{BASE}/catalogue/products/{created['product_id']}"
    assert (
        client.post(
            f"{path}/prices", json={"unit_price": "900", "command_id": str(uuid4())}, headers=sales
        ).status_code
        == 201
    )
    client.post(
        f"{path}/stock",
        json={"quantity": "3", "as_of": old, "command_id": str(uuid4())},
        headers=sales,
    )
    bolt = product_id(client, "IND-BOLT-M8")
    bolt_detail = client.get(f"{BASE}/catalogue/products/{bolt}").json()
    archived = client.post(
        f"{BASE}/catalogue/products/{bolt}/archive",
        json={"expected_version": bolt_detail["row_version"]},
        headers=sales,
    )
    assert archived.status_code == 200

    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    auth = {"Authorization": f"Bearer {token}"}
    valve_enquiry = submit(client, token, "Please quote 1 x IND-VALVE-1")
    bolt_enquiry = submit(client, token, "Please quote 100 x IND-BOLT-M8")
    drain(integration_settings, worker_engine)

    login(client, "manager@local.example")
    detail = pending(client)
    line = detail["versions"][-1]["content"]["lines"][0]
    assert (line["sku"], line["stock_status"]) == ("IND-VALVE-1", "stale")
    assert detail["versions"][-1]["content"]["stock_review_required"] is True

    status = client.get(f"/api/v1/enquiries/{bolt_enquiry}", headers=auth).json()
    questions = " ".join(status["extraction"]["questions"])
    # Not quoted, and the archived bolt is never offered. Like the simulated ERP, the SKU part
    # "ind" still matches the other IND- products, so the customer is asked which one they
    # meant (an open point recorded in the handoff).
    assert status["extraction"]["lines"] == []
    assert "M8 hex bolt" not in questions and "IND-FAN-450" in questions
    assert valve_enquiry != bolt_enquiry
