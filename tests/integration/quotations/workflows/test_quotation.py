"""M4/M5 on PostgreSQL: enquiry -> draft version -> approval (portal or channel) -> resume ->
approved PDF rendered, stored and offered to the API client that sent the enquiry.

Also proves concurrency (one decision wins), worker restart, append-only versions/decisions/
documents, column-limited delivery updates, narrow session/membership visibility and tenant
isolation through the real roles.
"""

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pypdf import PdfReader
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from sales_platform.api_app import create_app
from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.enquiries.adapters.interpreters.rules import RulesInterpreter
from sales_platform.enquiries.persistence.workflow import open_checkpointer
from sales_platform.integrations.domain.connections import ConnectionStatus
from sales_platform.integrations.persistence.connections import SqlConnectionStore, set_status
from sales_platform.platform.adapters.secrets.local import new_key
from sales_platform.platform.api.auth import CSRF_HEADER
from sales_platform.platform.application.jobs import JobRunner
from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.platform.persistence.jobs import SqlJobQueue
from sales_platform.platform.persistence.tenancy import SqlMembershipReader, tenant_transaction
from sales_platform.providers.simulated.signing import SIGNATURE_HEADER, hook_token, sign
from sales_platform.quotations.application.acceptance import (
    QuotationResponseService,
)
from sales_platform.quotations.application.approvals import ApprovalService
from sales_platform.quotations.application.pricing import QuotationPricingService
from sales_platform.quotations.domain.acceptance import (
    OfferNotOpen,
    PricesChanged,
    ResponseAction,
    ResponseOutcome,
    ResponseRequest,
)
from sales_platform.quotations.domain.quotations import (
    ApprovalAction,
    DecisionCommand,
    DecisionConflict,
)
from sales_platform.quotations.persistence.acceptance import SqlResponseStore
from sales_platform.quotations.persistence.pricing_policy import SqlPricingPolicyReader
from sales_platform.quotations.persistence.quotations import SqlApprovalStore
from sales_platform.seed import INDUSTRIAL_TENANT, OFFICE_TENANT, seed_demo_tenants
from tests.support.demo_catalogue import DemoCatalogue

type ApiKeyFactory = Callable[[UUID, Role], str]

MANAGER = "manager@industrial.example"
SALES = "sales@industrial.example"
SECRET = "integration-channel-secret"
ENQUIRY = "Please quote 2 x IND-FAN-450 and 150 pcs M8 bolts"


@pytest.fixture
def settings(integration_settings: Settings) -> Settings:
    return integration_settings.model_copy(update={"simulated_channel_secret": SecretStr(SECRET)})


@pytest.fixture
def make_client(settings: Settings, runtime_engine: Engine) -> Iterator[Callable[[], TestClient]]:
    """One TestClient per person, so each keeps its own session cookie."""
    clients: list[TestClient] = []

    def build() -> TestClient:
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@contextmanager
def running_worker(settings: Settings, engine: Engine) -> Iterator[JobRunner]:
    """Everything python -m sales_platform.worker wires, in-process (rules interpreter)."""
    erp = DemoCatalogue({INDUSTRIAL_TENANT: "industrial", OFFICE_TENANT: "office"})
    with open_checkpointer(settings.worker_url()) as checkpointer:
        handlers = build_job_handlers(
            settings, engine, build_runtime(settings, engine), checkpointer, RulesInterpreter(), erp
        )
        yield JobRunner(SqlJobQueue(engine), handlers)


def drain(settings: Settings, engine: Engine) -> None:
    with running_worker(settings, engine) as worker:
        while worker.run_once():
            pass


def submit(client: TestClient, token: str, text_: str) -> str:
    response = client.post(
        "/api/v1/enquiries",
        json={"text": text_, "customer_reference": "CUST-42"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"k-{uuid4()}"},
    )
    assert response.status_code == 202, response.text
    return str(response.json()["enquiry_id"])


def login(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/dev-login", json={"subject": subject})
    assert response.status_code == 200, response.text
    return {CSRF_HEADER: response.json()["csrf_token"]}


def base(tenant: UUID = INDUSTRIAL_TENANT) -> str:
    return f"/api/v1/tenants/{tenant}"


@pytest.fixture
def quoted(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
) -> dict[str, Any]:
    """An industrial enquiry, drafted and waiting for approval; returns the quotation detail."""
    intake = make_client()
    submit(intake, make_api_key(INDUSTRIAL_TENANT, Role.INTAKE_SERVICE), ENQUIRY)
    drain(settings, worker_engine)
    manager = make_client()
    login(manager, MANAGER)
    listed = manager.get(f"{base()}/quotations", params={"status": "pending_approval"}).json()
    assert len(listed) == 1
    detail: dict[str, Any] = manager.get(f"{base()}/quotations/{listed[0]['quotation_id']}").json()
    return detail


def decide(
    client: TestClient,
    headers: dict[str, str],
    detail: dict[str, Any],
    action: str,
    reason: str | None = None,
) -> Any:
    task = detail["pending_task"]
    return client.post(
        f"{base()}/approval-tasks/{task['task_id']}/decision",
        json={
            "command_id": str(uuid4()),
            "action": action,
            "content_sha256": task["content_sha256"],
            "reason": reason,
        },
        headers=headers,
    )


# Approved and delivered; since M6 the run keeps waiting for the customer's answer.
APPROVED_WAITING = ("approved", "running")


def run_status(owner_engine: Engine, quotation_id: str) -> tuple[str, str]:
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        row = connection.execute(
            text(
                "SELECT q.status, r.status AS run FROM quotations q JOIN workflow_runs r "
                "ON r.enquiry_id = q.enquiry_id AND r.graph_version = 'quotation-approval-v1' "
                "WHERE q.id = :id"
            ),
            {"id": quotation_id},
        ).one()
    return row.status, row.run


def test_enquiry_is_drafted_approved_in_the_portal_and_the_run_resumes(
    quoted: dict[str, Any],
    make_client: Callable[[], TestClient],
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    summary = quoted["summary"]
    # 2 x 4250.00 + 150 x 2.375 = 8856.25; 18% tax 1594.125 -> 1594.13 (half up).
    assert (summary["total"], summary["currency"]) == ("10450.38", "INR")
    assert summary["customer_reference"] == "CUST-42" and summary["current_version"] == 1
    assert quoted["versions"][0]["created_by"] == "system:quotation-workflow"
    assert quoted["can_decide"] is True

    manager = make_client()
    headers = login(manager, MANAGER)
    [listed] = manager.get(f"{base()}/enquiries").json()  # pinned before phase 1
    assert (listed["channel"], listed["customer_reference"]) == ("api", "CUST-42")
    response = decide(manager, headers, quoted, "approve")
    assert response.status_code == 200, response.text
    assert run_status(owner_engine, summary["quotation_id"]) == ("approved", "running")
    drain(settings, worker_engine)
    assert run_status(owner_engine, summary["quotation_id"]) == APPROVED_WAITING


def test_revision_loop_between_manager_and_sales(
    quoted: dict[str, Any],
    make_client: Callable[[], TestClient],
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    quotation_id = quoted["summary"]["quotation_id"]
    manager, sales = make_client(), make_client()
    manager_headers, sales_headers = login(manager, MANAGER), login(sales, SALES)
    assert decide(sales, sales_headers, quoted, "approve").status_code == 403
    assert decide(manager, manager_headers, quoted, "request_revision", "3 fans").status_code == 200
    drain(settings, worker_engine)

    revision = sales.post(
        f"{base()}/quotations/{quotation_id}/revisions",
        json={
            "command_id": str(uuid4()),
            "base_version": 1,
            "lines": [{"sku": "IND-FAN-450", "quantity": "3", "unit": "each"}],
            "reason": "Customer confirmed 3 fans",
        },
        headers=sales_headers,
    )
    assert revision.status_code == 201, revision.text
    drain(settings, worker_engine)
    detail = manager.get(f"{base()}/quotations/{quotation_id}").json()
    assert [version["version"] for version in detail["versions"]] == [1, 2]
    assert detail["versions"][1]["created_by"] == SALES
    assert detail["pending_task"]["version"] == 2
    assert [task["status"] for task in detail["tasks"]] == ["revision_requested", "pending"]
    assert decide(manager, manager_headers, detail, "approve").status_code == 200
    drain(settings, worker_engine)
    assert run_status(owner_engine, quotation_id) == APPROVED_WAITING


def test_manager_approves_through_the_generic_hook(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    """Phase 1 (ADR 0018): the same approval, through /api/v1/hooks and the process_inbound job."""
    keyed = settings.model_copy(update={"secret_store_key": SecretStr(new_key())})
    seed_demo_tenants(keyed)
    connection = SqlConnectionStore(owner_engine).find_by_account(
        INDUSTRIAL_TENANT, "simulated", "simulated-local"
    )
    assert connection is not None
    url = f"/api/v1/hooks/{connection.connection_id}/{hook_token(SECRET, connection.connection_id)}"
    client = TestClient(create_app(keyed))
    client.__enter__()
    manager = make_client()
    session = login(manager, MANAGER)
    code = manager.post(
        f"{base()}/channel-enrollments", json={"channel": "simulated"}, headers=session
    ).json()["code"]

    def post(event: dict[str, Any], secret: str = SECRET, to: str = url) -> Any:
        raw = json.dumps(event).encode()
        return client.post(to, content=raw, headers={SIGNATURE_HEADER: sign(secret, raw)})

    try:
        enroll = {"event_id": "h1", "external_user_id": "phone-9", "type": "enroll", "code": code}
        assert post(enroll).json() == {"received": 1, "duplicates": 0}
        assert post(enroll).json() == {"received": 1, "duplicates": 1}
        assert post(enroll, secret="forged").status_code == 401
        assert post(enroll, to=url.rsplit("/", 1)[0] + "/wrong-token").status_code == 404
        drain(keyed, worker_engine)
        bindings = manager.get(f"{base()}/channel-bindings").json()
        assert [b["external_user_id"] for b in bindings] == ["phone-9"]

        submit(make_client(), make_api_key(INDUSTRIAL_TENANT, Role.INTAKE_SERVICE), ENQUIRY)
        drain(keyed, worker_engine)
        [message_file] = list(Path(keyed.storage_dir / "simulated-channel").rglob("*.json"))
        reference = json.loads(message_file.read_text(encoding="utf-8"))["action_reference"]
        decision = {"event_id": "h2", "external_user_id": "phone-9", "type": "decision",
                    "action_reference": reference, "action": "approve"}  # fmt: skip
        assert post(decision).status_code == 200
        drain(keyed, worker_engine)
        listed = manager.get(f"{base()}/quotations").json()
        detail = manager.get(f"{base()}/quotations/{listed[0]['quotation_id']}").json()
        assert detail["decisions"][0]["transport"] == "simulated"
        assert run_status(owner_engine, listed[0]["quotation_id"]) == APPROVED_WAITING
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as db:
            outcomes = db.execute(
                text("SELECT outcome FROM inbox_events WHERE event_type = 'manager_action' "
                     "ORDER BY received_at")
            ).scalars().all()  # fmt: skip
        assert outcomes == ["enrolled", "recorded"]
        # The manager can unlink the phone in the portal (moved from the removed alias test).
        revoke = manager.delete(
            f"{base()}/channel-bindings/{bindings[0]['binding_id']}", headers=session
        )
        assert revoke.status_code == 204
        assert manager.get(f"{base()}/channel-bindings").json()[0]["revoked_at"] is not None

        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as db:
            set_status(db, INDUSTRIAL_TENANT, connection.connection_id,
                       ConnectionStatus.DISABLED, "test")  # fmt: skip
        assert post({**enroll, "event_id": "h3"}).status_code == 404
    finally:
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as db:
            set_status(db, INDUSTRIAL_TENANT, connection.connection_id,
                       ConnectionStatus.ACTIVE, "test")  # fmt: skip
        client.__exit__(None, None, None)


def test_concurrent_decisions_record_exactly_one(
    quoted: dict[str, Any], runtime_engine: Engine, owner_engine: Engine
) -> None:
    service = ApprovalService(
        SqlApprovalStore(runtime_engine), TenantContextResolver(SqlMembershipReader(runtime_engine))
    )
    task = quoted["pending_task"]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(action: ApprovalAction) -> None:
        command = DecisionCommand(
            command_id=uuid4(),
            tenant_id=INDUSTRIAL_TENANT,
            task_id=UUID(task["task_id"]),
            content_sha256=task["content_sha256"],
            action=action,
            reason=None if action is ApprovalAction.APPROVE else "no",
            transport="portal",
            evidence_ref=None,
            received_at=datetime.now(UTC),
        )
        barrier.wait()
        try:
            service.decide(MANAGER, command)
            outcomes.append("recorded")
        except DecisionConflict:
            outcomes.append("conflict")

    threads = [
        threading.Thread(target=attempt, args=(action,))
        for action in (ApprovalAction.APPROVE, ApprovalAction.REJECT)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "recorded"]
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        assert connection.execute(text("SELECT count(*) FROM approval_decisions")).scalar() == 1
        resumes = "SELECT count(*) FROM jobs WHERE kind = 'resume_quotation'"
        assert connection.execute(text(resumes)).scalar() == 1


def test_waiting_run_survives_a_worker_restart(
    quoted: dict[str, Any],
    make_client: Callable[[], TestClient],
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    # `quoted` drained with one worker instance (its checkpointer connection is closed now).
    manager = make_client()
    assert decide(manager, login(manager, MANAGER), quoted, "approve").status_code == 200
    drain(settings, worker_engine)  # a brand-new worker, graph and checkpointer connection
    assert run_status(owner_engine, quoted["summary"]["quotation_id"]) == APPROVED_WAITING


def test_unpriceable_enquiry_waits_for_a_sales_revision(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
) -> None:
    submit(make_client(), make_api_key(INDUSTRIAL_TENANT, Role.INTAKE_SERVICE),
           "Please quote 1 x IND-PUMP-2HP")  # fmt: skip
    drain(settings, worker_engine)
    sales = make_client()
    headers = login(sales, SALES)
    [item] = sales.get(f"{base()}/quotations").json()
    assert item["status"] == "pricing_failed" and item["total"] is None
    detail = sales.get(f"{base()}/quotations/{item['quotation_id']}").json()
    assert detail["pricing_issues"][0]["code"] == "price_missing" and detail["can_revise"]
    revision = sales.post(
        f"{base()}/quotations/{item['quotation_id']}/revisions",
        json={
            "command_id": str(uuid4()),
            "base_version": None,
            "lines": [{"sku": "IND-FAN-450", "quantity": "1", "unit": "each"}],
            "reason": "Pump unavailable; offering a fan",
        },
        headers=headers,
    )
    assert revision.status_code == 201, revision.text
    drain(settings, worker_engine)
    detail = sales.get(f"{base()}/quotations/{item['quotation_id']}").json()
    assert detail["summary"]["status"] == "pending_approval"
    assert detail["pending_task"]["version"] == 1


def test_other_tenants_cannot_see_the_quotation(
    quoted: dict[str, Any], make_client: Callable[[], TestClient]
) -> None:
    office = make_client()
    login(office, "sales@office.example")
    quotation_id = quoted["summary"]["quotation_id"]
    assert office.get(f"{base()}/quotations").status_code == 404
    assert office.get(f"{base(OFFICE_TENANT)}/quotations").json() == []
    assert office.get(f"{base(OFFICE_TENANT)}/quotations/{quotation_id}").status_code == 404


def test_versions_and_decisions_are_append_only(
    quoted: dict[str, Any],
    make_client: Callable[[], TestClient],
    runtime_engine: Engine,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    manager = make_client()
    assert decide(manager, login(manager, MANAGER), quoted, "approve").status_code == 200
    for engine in (runtime_engine, worker_engine):
        for statement in (
            "UPDATE quotation_versions SET total = 0",
            "DELETE FROM quotation_versions",
            "UPDATE approval_decisions SET action = 'reject'",
        ):
            with pytest.raises(ProgrammingError):  # no privilege at all
                with tenant_transaction(engine, INDUSTRIAL_TENANT) as connection:
                    connection.execute(text(statement))
    for statement in (
        "UPDATE quotation_versions SET total = 0",
        "DELETE FROM approval_decisions",
    ):
        with pytest.raises(DBAPIError, match="immutable"):  # even the owner, via the trigger
            with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
                connection.execute(text(statement))


def test_sessions_and_memberships_are_only_visible_to_their_owner(
    make_client: Callable[[], TestClient], runtime_engine: Engine, worker_engine: Engine
) -> None:
    login(make_client(), MANAGER)
    login(make_client(), SALES)
    with runtime_engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM user_sessions")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM memberships")).scalar() == 0
        connection.execute(text("SELECT set_config('app.subject', :s, true)"), {"s": MANAGER})
        subjects = connection.execute(text("SELECT DISTINCT subject FROM memberships")).scalars()
        assert list(subjects) == [MANAGER]
        tenants = connection.execute(text("SELECT name FROM tenants")).scalars()
        assert list(tenants) == ["Demo Industrial Supplies"]
    with pytest.raises(ProgrammingError), worker_engine.begin() as connection:
        connection.execute(text("SELECT * FROM user_sessions"))


# --- M5: documents and delivery -------------------------------------------------------------


def delivery_rows(owner_engine: Engine, tenant: UUID = INDUSTRIAL_TENANT) -> list[Any]:
    with tenant_transaction(owner_engine, tenant) as connection:
        return list(
            connection.execute(
                text(
                    "SELECT d.status, d.channel, d.recipient, d.first_downloaded_at, "
                    "doc.storage_key, doc.sha256 FROM quotation_deliveries d "
                    "LEFT JOIN quotation_documents doc ON doc.id = d.document_id"
                )
            ).all()
        )


def test_approved_quotation_is_rendered_stored_and_offered_to_the_api_client(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    intake = make_client()
    token = make_api_key(INDUSTRIAL_TENANT, Role.INTAKE_SERVICE)
    customer = {"Authorization": f"Bearer {token}"}
    enquiry_id = submit(intake, token, ENQUIRY)
    drain(settings, worker_engine)
    url = f"/api/v1/enquiries/{enquiry_id}/quotation"
    assert intake.get(url, headers=customer).status_code == 404  # pending approval: invisible

    manager = make_client()
    headers = login(manager, MANAGER)
    [listed] = manager.get(f"{base()}/quotations").json()
    detail = manager.get(f"{base()}/quotations/{listed['quotation_id']}").json()
    quotation_url = f"{base()}/quotations/{listed['quotation_id']}"
    preview = manager.get(f"{quotation_url}/versions/1/preview.pdf")
    assert preview.status_code == 200 and b"%PDF-" in preview.content[:8]
    assert decide(manager, headers, detail, "approve").status_code == 200
    drain(settings, worker_engine)

    [row] = delivery_rows(owner_engine)
    assert (row.status, row.channel) == ("available", "api")
    assert row.recipient.startswith("api-key:")
    stored = (settings.storage_dir / row.storage_key).read_bytes()
    assert hashlib.sha256(stored).hexdigest() == row.sha256

    issued = intake.get(url, headers=customer)
    assert issued.status_code == 200, issued.text
    assert issued.json()["total"] == "10450.38" and issued.json()["version"] == 1
    pdf = intake.get(f"{url}.pdf", headers=customer)
    assert pdf.status_code == 200 and pdf.content == stored
    text_ = "".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert "10450.38" in text_ and "CUST-42" in text_ and "PREVIEW" not in text_
    [row] = delivery_rows(owner_engine)
    assert row.first_downloaded_at is not None

    overview = manager.get(f"{quotation_url}/delivery").json()
    assert overview["document"]["sha256"] == row.sha256
    assert overview["deliveries"][0]["first_downloaded_at"] is not None
    assert manager.get(f"{quotation_url}/document.pdf").content == stored

    drain(settings, worker_engine)  # nothing left to do; no second delivery or document
    assert len(delivery_rows(owner_engine)) == 1
    office_key = make_api_key(OFFICE_TENANT, Role.INTAKE_SERVICE)
    office = {"Authorization": f"Bearer {office_key}"}
    assert intake.get(url, headers=office).status_code == 404
    assert delivery_rows(owner_engine, OFFICE_TENANT) == []


def test_rejected_quotation_is_never_rendered_or_offered(
    quoted: dict[str, Any],
    make_client: Callable[[], TestClient],
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    manager = make_client()
    response = decide(manager, login(manager, MANAGER), quoted, "reject", "Price too high")
    assert response.status_code == 200
    drain(settings, worker_engine)
    assert delivery_rows(owner_engine) == []
    quotation_url = f"{base()}/quotations/{quoted['summary']['quotation_id']}"
    assert manager.get(f"{quotation_url}/document.pdf").status_code == 404


def test_documents_are_append_only_and_delivery_updates_are_column_limited(
    quoted: dict[str, Any],
    make_client: Callable[[], TestClient],
    settings: Settings,
    runtime_engine: Engine,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    manager = make_client()
    assert decide(manager, login(manager, MANAGER), quoted, "approve").status_code == 200
    drain(settings, worker_engine)
    assert len(delivery_rows(owner_engine)) == 1
    for engine, statement in (
        (runtime_engine, "UPDATE quotation_documents SET sha256 = 'x'"),
        (worker_engine, "UPDATE quotation_documents SET sha256 = 'x'"),
        (worker_engine, "DELETE FROM quotation_documents"),
        (runtime_engine, "UPDATE quotation_deliveries SET status = 'sent'"),
        (runtime_engine, "INSERT INTO quotation_deliveries (id) VALUES (gen_random_uuid())"),
        (worker_engine, "UPDATE quotation_deliveries SET first_downloaded_at = now()"),
        (worker_engine, "UPDATE quotation_deliveries SET recipient = 'attacker@x.test'"),
    ):
        with pytest.raises(ProgrammingError):  # no privilege for this table or column
            with tenant_transaction(engine, INDUSTRIAL_TENANT) as connection:
                connection.execute(text(statement))
    with pytest.raises(DBAPIError, match="immutable"):  # even the owner, via the trigger
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
            connection.execute(text("DELETE FROM quotation_documents"))


# --- M6: customer acceptance ----------------------------------------------------------------


def issued_quotation(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
) -> tuple[TestClient, dict[str, str], str, dict[str, Any]]:
    """Submit, approve and deliver; return (client, customer auth, enquiry ID, issued JSON)."""
    intake = make_client()
    token = make_api_key(INDUSTRIAL_TENANT, Role.INTAKE_SERVICE)
    auth = {"Authorization": f"Bearer {token}"}
    enquiry_id = submit(intake, token, ENQUIRY)
    drain(settings, worker_engine)
    manager = make_client()
    headers = login(manager, MANAGER)
    [listed] = manager.get(f"{base()}/quotations").json()
    detail = manager.get(f"{base()}/quotations/{listed['quotation_id']}").json()
    assert decide(manager, headers, detail, "approve").status_code == 200
    drain(settings, worker_engine)
    issued = intake.get(f"/api/v1/enquiries/{enquiry_id}/quotation", headers=auth).json()
    return intake, auth, enquiry_id, issued


def customer_context() -> TenantContext:
    return TenantContext(INDUSTRIAL_TENANT, "api-key:direct", frozenset({Role.INTAKE_SERVICE}))


def responses_service(
    engine: Engine, erp: DemoCatalogue | None = None, at: datetime | None = None
) -> QuotationResponseService:
    pricing = QuotationPricingService(
        erp or DemoCatalogue({INDUSTRIAL_TENANT: "industrial", OFFICE_TENANT: "office"}),
        SqlPricingPolicyReader(engine),
    )
    if at is None:
        return QuotationResponseService(SqlResponseStore(engine), pricing)
    return QuotationResponseService(SqlResponseStore(engine), pricing, clock=lambda: at)


def test_customer_accepts_over_the_api_and_the_run_ends_accepted(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    intake, auth, enquiry_id, issued = issued_quotation(
        make_client, make_api_key, settings, worker_engine
    )
    assert issued["status"] == "open"
    valid_until = datetime.fromisoformat(issued["valid_until"])
    assert timedelta(days=6, hours=23) < valid_until - datetime.now(UTC) <= timedelta(days=7)
    body = {"action": "accept", "version": 1, "content_sha256": issued["content_sha256"]}
    headers = {**auth, "Idempotency-Key": f"accept-{uuid4()}"}
    accepted = intake.post(issued["response_path"], json=body, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["outcome"] == "accepted"
    assert intake.post(issued["response_path"], json=body, headers=headers).json()["replayed"]
    drain(settings, worker_engine)
    assert run_status(owner_engine, issued_quotation_id(owner_engine)) == ("accepted", "accepted")
    after = intake.get(f"/api/v1/enquiries/{enquiry_id}/quotation", headers=auth).json()
    assert after["status"] == "accepted"
    manager = make_client()
    login(manager, MANAGER)
    view = manager.get(f"{base()}/quotations/{issued_quotation_id(owner_engine)}/customer").json()
    assert [item["outcome"] for item in view["responses"]] == ["accepted"]


def issued_quotation_id(owner_engine: Engine) -> str:
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        return str(connection.execute(text("SELECT id FROM quotations")).scalar_one())


def test_changed_price_refuses_and_the_revision_goes_to_approval(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    runtime_engine: Engine,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    _, _, enquiry_id, issued = issued_quotation(make_client, make_api_key, settings, worker_engine)
    erp = DemoCatalogue({INDUSTRIAL_TENANT: "industrial", OFFICE_TENANT: "office"})
    erp.set_price(INDUSTRIAL_TENANT, "IND-FAN-450", "4500.00")
    request = ResponseRequest(ResponseAction.ACCEPT, 1, issued["content_sha256"], None)
    with pytest.raises(PricesChanged) as refused:
        responses_service(runtime_engine, erp).respond(
            customer_context(), UUID(enquiry_id), "api", f"accept-{uuid4()}", request
        )
    assert refused.value.revised_version == 2
    quotation_id = issued_quotation_id(owner_engine)
    assert run_status(owner_engine, quotation_id) == ("revision_submitted", "running")
    drain(settings, worker_engine)  # the run asks a manager to approve version 2
    manager = make_client()
    login(manager, MANAGER)
    detail = manager.get(f"{base()}/quotations/{quotation_id}").json()
    assert detail["pending_task"]["version"] == 2
    assert detail["versions"][-1]["created_by"] == "system:quotation-workflow"
    assert detail["versions"][-1]["content"]["lines"][0]["unit_price"] == "4500.00"


def test_accept_and_withdraw_at_once_record_exactly_one_answer(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    runtime_engine: Engine,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    _, _, enquiry_id, issued = issued_quotation(make_client, make_api_key, settings, worker_engine)
    quotation_id = UUID(issued_quotation_id(owner_engine))
    staff = TenantContext(INDUSTRIAL_TENANT, SALES, frozenset({Role.SALES_OPERATOR}))
    request = ResponseRequest(ResponseAction.ACCEPT, 1, issued["content_sha256"], None)
    results: list[str] = []
    start = threading.Barrier(2)

    def accept() -> None:
        start.wait()
        try:
            responses_service(runtime_engine).respond(
                customer_context(), UUID(enquiry_id), "api", f"accept-{uuid4()}", request
            )
            results.append("accepted")
        except OfferNotOpen:
            results.append("refused")

    def withdraw() -> None:
        start.wait()
        try:
            responses_service(runtime_engine).withdraw(staff, quotation_id, uuid4(), "Mistake")
            results.append("withdrawn")
        except OfferNotOpen:
            results.append("refused")

    threads = [threading.Thread(target=accept), threading.Thread(target=withdraw)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) in (["accepted", "refused"], ["refused", "withdrawn"])
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        assert connection.execute(text("SELECT count(*) FROM quotation_responses")).scalar() == 1


def test_expiry_closes_an_unanswered_quotation(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    intake, auth, enquiry_id, issued = issued_quotation(
        make_client, make_api_key, settings, worker_engine
    )
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        run_at = connection.execute(
            text("SELECT run_at FROM jobs WHERE kind = 'expire_quotation'")
        ).scalar_one()
    assert run_at == datetime.fromisoformat(issued["valid_until"])
    quotation_id = UUID(issued_quotation_id(owner_engine))
    later = datetime.fromisoformat(issued["valid_until"]) + timedelta(seconds=1)
    # The worker role runs the expiry (as the job would at valid_until).
    expired = responses_service(worker_engine, at=later).expire(INDUSTRIAL_TENANT, quotation_id, 1)
    assert expired is not None and expired.outcome is ResponseOutcome.EXPIRED
    drain(settings, worker_engine)
    assert run_status(owner_engine, str(quotation_id)) == ("expired", "expired")
    status_now = intake.get(f"/api/v1/enquiries/{enquiry_id}/quotation", headers=auth).json()
    assert status_now["status"] == "expired"


def test_answers_are_append_only_and_tenant_isolated(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    runtime_engine: Engine,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    _, _, enquiry_id, issued = issued_quotation(make_client, make_api_key, settings, worker_engine)
    request = ResponseRequest(ResponseAction.DECLINE, 1, issued["content_sha256"], "No budget")
    responses_service(runtime_engine).respond(
        customer_context(), UUID(enquiry_id), "api", f"decline-{uuid4()}", request
    )
    for engine in (runtime_engine, worker_engine):
        for statement in (
            "UPDATE quotation_responses SET outcome = 'accepted'",
            "DELETE FROM quotation_responses",
        ):
            with pytest.raises(ProgrammingError):
                with tenant_transaction(engine, INDUSTRIAL_TENANT) as connection:
                    connection.execute(text(statement))
    with pytest.raises(DBAPIError, match="immutable"):
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
            connection.execute(text("UPDATE quotation_responses SET reason = 'changed'"))
    for engine in (runtime_engine, worker_engine):
        with tenant_transaction(engine, OFFICE_TENANT) as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM quotation_responses")).scalar() == 0
            )


def test_every_linked_phone_gets_a_working_request(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    """Regression (phase 1 live run): two accounts get the same task file name; both messages
    must be recorded as sent and both tokens must work (through the generic hook route)."""
    keyed = settings.model_copy(update={"secret_store_key": SecretStr(new_key())})
    seed_demo_tenants(keyed)
    connection = SqlConnectionStore(owner_engine).find_by_account(
        INDUSTRIAL_TENANT, "simulated", "simulated-local"
    )
    assert connection is not None
    url = f"/api/v1/hooks/{connection.connection_id}/{hook_token(SECRET, connection.connection_id)}"
    client = TestClient(create_app(keyed))
    client.__enter__()
    manager = make_client()
    headers = login(manager, MANAGER)

    def post(event: dict[str, Any]) -> Any:
        raw = json.dumps(event).encode()
        return client.post(url, content=raw, headers={SIGNATURE_HEADER: sign(SECRET, raw)})

    try:
        for phone in ("phone-a", "phone-b"):
            code = manager.post(f"{base()}/channel-enrollments", json={"channel": "simulated"},
                                headers=headers).json()["code"]  # fmt: skip
            enroll = {"event_id": f"e-{phone}", "external_user_id": phone, "type": "enroll",
                      "code": code}  # fmt: skip
            assert post(enroll).status_code == 200
            drain(keyed, worker_engine)
        submit(make_client(), make_api_key(INDUSTRIAL_TENANT, Role.INTAKE_SERVICE), ENQUIRY)
        drain(keyed, worker_engine)
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as db:
            statuses = db.execute(
                text("SELECT status FROM outbound_messages WHERE template_key = 'approval_request'")
            ).scalars().all()  # fmt: skip
        assert statuses == ["sent", "sent"]
        files = sorted(Path(keyed.storage_dir / "simulated-channel").rglob("*.json"))
        assert [path.parent.name for path in files] == ["phone-a", "phone-b"]
        reference = json.loads(files[1].read_text(encoding="utf-8"))["action_reference"]
        decision = {"event_id": "d-b", "external_user_id": "phone-b", "type": "decision",
                    "action_reference": reference, "action": "approve"}  # fmt: skip
        assert post(decision).status_code == 200
        drain(keyed, worker_engine)
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as db:
            outcomes = db.execute(
                text("SELECT outcome FROM inbox_events WHERE event_type = 'manager_action' "
                     "ORDER BY received_at")
            ).scalars().all()  # fmt: skip
        assert outcomes == ["enrolled", "enrolled", "recorded"]
    finally:
        client.__exit__(None, None, None)


def test_a_business_without_a_product_source_gets_a_clear_outcome_at_once(
    make_client: Callable[[], TestClient],
    make_api_key: ApiKeyFactory,
    settings: Settings,
    owner_engine: Engine,
    worker_engine: Engine,
) -> None:
    """P1-Q3/Q4: no retries; the enquiry goes to review with the reason shown in the portal."""
    tenant = uuid4()
    with tenant_transaction(owner_engine, tenant) as db:
        db.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'No Source Ltd')"),
                   {"id": tenant})  # fmt: skip
    submit(make_client(), make_api_key(tenant, Role.INTAKE_SERVICE), "Please quote 2 x IND-FAN-450")
    with open_checkpointer(settings.worker_url()) as checkpointer:
        handlers = build_job_handlers(  # the real per-business catalogue, no fixed reader
            settings,
            worker_engine,
            build_runtime(settings, worker_engine),
            checkpointer,
            RulesInterpreter(),
        )
        runner = JobRunner(SqlJobQueue(worker_engine), handlers)
        while runner.run_once():
            pass
    with tenant_transaction(owner_engine, tenant) as db:
        status_, outcome = db.execute(
            text("SELECT e.status, r.outcome FROM enquiries e JOIN workflow_runs r "
                 "ON r.enquiry_id = e.id")
        ).one()  # fmt: skip
        attempts = db.execute(text("SELECT max(attempts) FROM jobs")).scalar_one()
    assert status_ == "needs_review"
    assert outcome["review_reasons"] == ["No product source is connected"]
    assert attempts == 1
