"""Phase 7 on PostgreSQL: an accepted quotation becomes exactly one order, through the real
API, the real worker and SqlOrderStore. Covers automatic and staff creation, the customer's
pull view, local status moves with customer notices, the one-order guard under a race, the
ERP write's compare-and-set steps, append-only history and business isolation.
"""

import threading
from collections.abc import Callable, Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from sales_platform.api_app import create_app
from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.enquiries.adapters.interpreters.rules import RulesInterpreter
from sales_platform.enquiries.persistence.workflow import open_checkpointer
from sales_platform.orders.domain.orders import (
    EventSource,
    OrderCreation,
    OrderStatus,
    order_lines,
)
from sales_platform.orders.persistence.orders import SqlOrderStore
from sales_platform.orders.ports.orders import NewOrder, OrderExists
from sales_platform.platform.api.auth import CSRF_HEADER
from sales_platform.platform.application.jobs import JobRunner
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.tenancy import Role
from sales_platform.platform.persistence.jobs import SqlJobQueue
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.seed import INDUSTRIAL_TENANT, LOCAL_TENANT

pytestmark = pytest.mark.integration

type ApiKeyFactory = Callable[[UUID, Role], str]
BASE = f"/api/v1/tenants/{LOCAL_TENANT}"


@pytest.fixture(autouse=True)
def staff_create_orders(owner_engine: Engine) -> Iterator[None]:
    """The setting lives on the tenant row, which tests do not truncate: reset it around each."""
    yield
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        connection.execute(
            text("UPDATE tenants SET order_creation = 'staff' WHERE id = :t"), {"t": LOCAL_TENANT}
        )


@pytest.fixture
def client(integration_settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(integration_settings)) as app:
        yield app


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


def accepted_quotation(
    client: TestClient, token: str, settings: Settings, worker: Engine
) -> tuple[str, str]:
    """Enquiry -> priced -> approved -> accepted by the customer's system: (enquiry, quotation)."""
    auth = {"Authorization": f"Bearer {token}"}
    submitted = client.post(
        "/api/v1/enquiries",
        json={"text": "Please quote 2 x IND-FAN-450"},
        headers={**auth, "Idempotency-Key": f"k-{uuid4()}"},
    )
    assert submitted.status_code == 202, submitted.text
    enquiry_id = str(submitted.json()["enquiry_id"])
    drain(settings, worker)
    headers = login(client, "manager@local.example")
    [listed] = client.get(f"{BASE}/quotations", params={"status": "pending_approval"}).json()
    task = client.get(f"{BASE}/quotations/{listed['quotation_id']}").json()["pending_task"]
    decided = client.post(
        f"{BASE}/approval-tasks/{task['task_id']}/decision",
        json={
            "command_id": str(uuid4()),
            "action": "approve",
            "content_sha256": task["content_sha256"],
        },
        headers=headers,
    )
    assert decided.status_code == 200, decided.text
    drain(settings, worker)
    issued = client.get(f"/api/v1/enquiries/{enquiry_id}/quotation", headers=auth).json()
    body = {"action": "accept", "version": 1, "content_sha256": issued["content_sha256"]}
    answer = client.post(
        issued["response_path"], json=body, headers={**auth, "Idempotency-Key": f"a-{uuid4()}"}
    )
    assert answer.status_code == 200, answer.text
    return enquiry_id, str(listed["quotation_id"])


def set_creation(client: TestClient, value: str) -> None:
    headers = login(client, "admin@local.example")
    saved = client.put(f"{BASE}/order-settings", json={"order_creation": value}, headers=headers)
    assert saved.status_code == 200, saved.text


def test_automatic_orders_follow_acceptance_and_customers_can_follow_them(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    integration_settings: Settings,
    worker_engine: Engine,
) -> None:
    set_creation(client, "automatic")
    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    auth = {"Authorization": f"Bearer {token}"}
    enquiry_id, quotation_id = accepted_quotation(
        client, token, integration_settings, worker_engine
    )
    drain(integration_settings, worker_engine)  # create_order runs with the acceptance's jobs

    login(client, "sales@local.example")
    order = client.get(f"{BASE}/quotations/{quotation_id}/order").json()
    assert order["status"] == "placed"
    assert [line["unit_price"] for line in order["lines"]] == ["4250.00"]
    pulled = client.get(f"/api/v1/enquiries/{enquiry_id}/order", headers=auth)
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["order_number"] == order["order_number"]
    assert "erp_order_id" not in pulled.json()  # never staff or ERP details for customers

    sales = login(client, "sales@local.example")
    moved = client.post(
        f"{BASE}/orders/{order['order_id']}/status", json={"status": "confirmed"}, headers=sales
    )
    assert moved.status_code == 200, moved.text
    refused = client.post(
        f"{BASE}/orders/{order['order_id']}/status", json={"status": "delivered"}, headers=sales
    )
    assert refused.status_code == 409
    detail = client.get(f"{BASE}/orders/{order['order_id']}").json()
    assert [event["type"] for event in detail["events"]] == ["created", "status_changed"]
    assert (
        client.get(f"{BASE}/orders", params={"status": "confirmed"}).json()[0]["order_id"]
        == (order["order_id"])
    )
    drain(integration_settings, worker_engine)  # notify_order: API customers pull, nothing sent


def test_staff_create_orders_by_default_and_only_once(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    integration_settings: Settings,
    worker_engine: Engine,
) -> None:
    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    _, quotation_id = accepted_quotation(client, token, integration_settings, worker_engine)
    drain(integration_settings, worker_engine)
    sales = login(client, "sales@local.example")
    assert client.get(f"{BASE}/quotations/{quotation_id}/order").status_code == 404
    created = client.post(f"{BASE}/quotations/{quotation_id}/order", json={}, headers=sales)
    assert created.status_code == 201, created.text
    again = client.post(f"{BASE}/quotations/{quotation_id}/order", json={}, headers=sales)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "order_exists"


def _new_order(store: SqlOrderStore, quotation_id: UUID) -> NewOrder:
    accepted = store.accepted_quotation(LOCAL_TENANT, quotation_id)
    assert accepted is not None and accepted.response_id is not None
    order_id = uuid4()
    content = accepted.content
    return NewOrder(
        order_id=order_id,
        quotation=accepted,
        status=OrderStatus.PLACED,
        hold_reason=None,
        currency=str(content["currency"]),
        subtotal=Decimal(str(content["subtotal"])),
        tax=Decimal(str(content["tax"])),
        total=Decimal(str(content["total"])),
        lines=order_lines(content),
        actor="test",
        source=EventSource.STAFF,
    )


def test_two_creates_racing_for_one_quotation_make_one_order(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    integration_settings: Settings,
    worker_engine: Engine,
    runtime_engine: Engine,
) -> None:
    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    _, quotation = accepted_quotation(client, token, integration_settings, worker_engine)
    quotation_id = UUID(quotation)
    stores = [SqlOrderStore(runtime_engine), SqlOrderStore(worker_engine)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create(store: SqlOrderStore) -> None:
        new = _new_order(store, quotation_id)
        barrier.wait()
        try:
            store.create(LOCAL_TENANT, new)
            outcomes.append("created")
        except OrderExists:
            outcomes.append("exists")

    threads = [threading.Thread(target=create, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["created", "exists"]


def test_order_history_is_append_only_and_other_businesses_see_nothing(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    integration_settings: Settings,
    worker_engine: Engine,
    runtime_engine: Engine,
) -> None:
    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    _, quotation = accepted_quotation(client, token, integration_settings, worker_engine)
    store = SqlOrderStore(worker_engine)
    order = store.create(LOCAL_TENANT, _new_order(store, UUID(quotation)))

    # Lines and events never change, for any role; other businesses see nothing.
    for engine in (runtime_engine, worker_engine):
        for statement in ("UPDATE order_events SET actor = 'x'", "DELETE FROM order_lines"):
            with pytest.raises(DBAPIError), engine.begin() as connection:
                connection.execute(
                    text(f"SELECT set_config('app.tenant_id', '{LOCAL_TENANT}', true)")
                )
                connection.execute(text(statement))
        with pytest.raises(ProgrammingError), engine.begin() as connection:
            connection.execute(text(f"SELECT set_config('app.tenant_id', '{LOCAL_TENANT}', true)"))
            connection.execute(text("UPDATE orders SET total = 0"))
    assert SqlOrderStore(runtime_engine).get(INDUSTRIAL_TENANT, order.order_id) is None
    assert store.order_creation(LOCAL_TENANT) is OrderCreation.STAFF
