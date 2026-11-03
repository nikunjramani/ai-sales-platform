"""Live run of phases 6 and 7 against the local ERPNext sandbox (skipped unless
SALES_TEST_ERPNEXT_URL, _KEY and _SECRET are set; see provider-setup.md#erpnext).

Through the real API and worker: Enable ERPNext with one form -> it replaces our product list
and the first copy runs -> an enquiry for PUMP-100 is priced live from ERPNext -> approved ->
accepted -> the order is created automatically and written to ERPNext once, with our reference
-> its status is read back. It creates a sales order in the sandbox (synthetic data).
"""

import os
from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from sales_platform.adapters.interpreters.rules import RulesInterpreter
from sales_platform.adapters.providers.erpnext.client import FrappeClient
from sales_platform.adapters.secrets.local import new_key
from sales_platform.api.app import create_app
from sales_platform.api.auth import CSRF_HEADER
from sales_platform.application.jobs import JobRunner
from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.config import Settings
from sales_platform.domain.tenancy import Role
from sales_platform.persistence.jobs import SqlJobQueue
from sales_platform.persistence.seed import LOCAL_TENANT
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.persistence.workflow import open_checkpointer

pytestmark = pytest.mark.integration
type ApiKeyFactory = Callable[[UUID, Role], str]
BASE = f"/api/v1/tenants/{LOCAL_TENANT}"


@pytest.fixture
def sandbox() -> dict[str, str]:
    values = {
        name: os.environ.get(f"SALES_TEST_ERPNEXT_{name.upper()}", "")
        for name in ("url", "key", "secret")
    }
    if not all(values.values()):
        pytest.skip("No ERPNext sandbox configured")
    return values


@pytest.fixture
def settings(integration_settings: Settings) -> Settings:
    # The API key goes to the local secret store (a fresh synthetic key for this test).
    return integration_settings.model_copy(
        update={"secret_store": "local", "secret_store_key": SecretStr(new_key())}
    )


@pytest.fixture
def client(settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as app:
        yield app


@pytest.fixture(autouse=True)
def restore_local_business(owner_engine: Engine) -> Iterator[None]:
    yield
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        connection.execute(
            text("UPDATE tenants SET order_creation = 'staff' WHERE id = :t"), {"t": LOCAL_TENANT}
        )


def drain(settings: Settings, engine: Engine) -> None:
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


def test_quote_and_order_through_erpnext(
    client: TestClient,
    settings: Settings,
    worker_engine: Engine,
    make_api_key: ApiKeyFactory,
    sandbox: dict[str, str],
) -> None:
    admin = login(client, "admin@local.example")
    enabled = client.post(
        f"{BASE}/connections/enable",
        json={"provider_key": "erpnext", "accept_costs": False},
        headers=admin,
    )
    assert enabled.status_code == 200, enabled.text
    connection_id = enabled.json()["connection"]["connection_id"]
    form = {
        "base_url": sandbox["url"],
        "api_key": sandbox["key"],
        "api_secret": sandbox["secret"],
        "price_list": "Standard Selling",
        "warehouses": os.environ.get("SALES_TEST_ERPNEXT_WAREHOUSE", "Stores - PS"),
        "write_orders": True,
        "default_customer": os.environ.get("SALES_TEST_ERPNEXT_CUSTOMER", "Sandbox Customer"),
    }
    sent = client.post(
        f"{BASE}/connections/{connection_id}/actions/settings",
        json={"submission": form},
        headers=admin,
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["connection"]["status"] == "active"
    saved = client.put(
        f"{BASE}/order-settings", json={"order_creation": "automatic"}, headers=admin
    )
    assert saved.status_code == 200
    drain(settings, worker_engine)  # the first copy

    sync = client.get(f"{BASE}/catalogue/sync").json()
    assert sync["synced"] and sync["latest"]["status"] == "succeeded", sync
    summary = client.get(f"{BASE}/catalogue/summary").json()
    assert summary["source"]["provider_key"] == "erpnext" and not summary["source"]["editable"]

    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    auth = {"Authorization": f"Bearer {token}"}
    submitted = client.post(
        "/api/v1/enquiries",
        json={"text": "Please quote 2 x PUMP-100"},
        headers={**auth, "Idempotency-Key": f"k-{uuid4()}"},
    )
    assert submitted.status_code == 202, submitted.text
    enquiry_id = submitted.json()["enquiry_id"]
    drain(settings, worker_engine)
    manager = login(client, "manager@local.example")
    [listed] = client.get(f"{BASE}/quotations", params={"status": "pending_approval"}).json()
    detail = client.get(f"{BASE}/quotations/{listed['quotation_id']}").json()
    line = detail["versions"][-1]["content"]["lines"][0]
    assert (line["sku"], line["price_source"], line["price_list_id"]) == (
        "PUMP-100",
        "erpnext",
        "Standard Selling",
    )
    task = detail["pending_task"]
    decided = client.post(
        f"{BASE}/approval-tasks/{task['task_id']}/decision",
        json={
            "command_id": str(uuid4()),
            "action": "approve",
            "content_sha256": task["content_sha256"],
        },
        headers=manager,
    )
    assert decided.status_code == 200, decided.text
    drain(settings, worker_engine)
    issued = client.get(f"/api/v1/enquiries/{enquiry_id}/quotation", headers=auth).json()
    body = {"action": "accept", "version": 1, "content_sha256": issued["content_sha256"]}
    answer = client.post(
        issued["response_path"], json=body, headers={**auth, "Idempotency-Key": f"a-{uuid4()}"}
    )
    assert answer.status_code == 200, answer.text
    drain(settings, worker_engine)  # create_order, then the ERP write

    login(client, "sales@local.example")
    order = client.get(f"{BASE}/quotations/{listed['quotation_id']}/order").json()
    assert order["fulfilment"] == "erp" and order["erp_order_id"], order
    detail = client.get(f"{BASE}/orders/{order['order_id']}").json()
    [operation] = detail["operations"]
    assert operation["status"] == "succeeded"
    erp = FrappeClient(sandbox["url"], sandbox["key"], sandbox["secret"])
    document = erp.get_doc("Sales Order", order["erp_order_id"])
    assert document["po_no"] == operation["external_reference"]
    assert str(document["items"][0]["rate"]).startswith(line["unit_price"].split(".")[0])
    pulled = client.get(f"/api/v1/enquiries/{enquiry_id}/order", headers=auth).json()
    assert pulled["order_number"] == order["order_number"]
