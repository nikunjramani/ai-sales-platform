"""Extraction workflow on PostgreSQL: API -> worker -> LangGraph (Postgres checkpoints) -> M1."""

import base64
from collections.abc import Callable, Iterator, Mapping, Sequence
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from sales_platform.adapters.documents.text import PdfAndTextReader
from sales_platform.adapters.erp.simulated import SimulatedErp, demo_catalogues
from sales_platform.adapters.interpreters.rules import RulesInterpreter
from sales_platform.adapters.storage.local import LocalObjectStorage
from sales_platform.api.app import create_app
from sales_platform.application.jobs import JobRunner
from sales_platform.application.preparation import EnquiryPreparationHandler
from sales_platform.application.pricing import QuotationPricingService
from sales_platform.config import Settings
from sales_platform.domain.catalogue import Product
from sales_platform.domain.jobs import JobKind
from sales_platform.domain.money import Money
from sales_platform.domain.pricing import PricedQuotation, RequestedLine
from sales_platform.domain.requirements import ExtractedRequirements, RequestedItem
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.persistence.intake import SqlPreparationStore
from sales_platform.persistence.jobs import SqlJobQueue
from sales_platform.persistence.seed import INDUSTRIAL_TENANT, OFFICE_TENANT
from sales_platform.persistence.tenancy import SqlPricingPolicyReader, tenant_transaction
from sales_platform.persistence.workflow import SqlWorkflowStore, open_checkpointer
from sales_platform.ports.erp import ErpError, ErpErrorCode
from sales_platform.ports.interpretation import DocumentText, EnquiryTranscript
from sales_platform.workflows.enquiry import (
    EnquiryWorkflowDeps,
    ExtractionJobHandler,
    build_enquiry_graph,
)

type ApiKeyFactory = Callable[[UUID, Role], str]


class CountingInterpreter(RulesInterpreter):
    def __init__(self) -> None:
        self.extract_calls = 0
        self.while_reading: list[Callable[[], object]] = []  # run once, mid-read (race tests)

    def extract(
        self, transcript: EnquiryTranscript, documents: Sequence[DocumentText]
    ) -> ExtractedRequirements:
        self.extract_calls += 1
        result = super().extract(transcript, documents)
        while self.while_reading:
            self.while_reading.pop()()
        return result

    def choose_product(self, item: RequestedItem, candidates: Sequence[Product]) -> str | None:
        return super().choose_product(item, candidates)


@pytest.fixture
def client(integration_settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(integration_settings)) as http:
        yield http


@pytest.fixture
def erp() -> SimulatedErp:
    return SimulatedErp(demo_catalogues(INDUSTRIAL_TENANT, OFFICE_TENANT))


@pytest.fixture
def interpreter() -> CountingInterpreter:
    return CountingInterpreter()


@pytest.fixture
def worker(
    worker_engine: Engine,
    storage: LocalObjectStorage,
    integration_settings: Settings,
    erp: SimulatedErp,
    interpreter: CountingInterpreter,
) -> Iterator[JobRunner]:
    store = SqlWorkflowStore(worker_engine)
    with open_checkpointer(integration_settings.worker_url()) as checkpointer:
        deps = EnquiryWorkflowDeps(store, storage, PdfAndTextReader(), interpreter, erp)
        yield JobRunner(
            SqlJobQueue(worker_engine),
            {
                JobKind.PREPARE_ENQUIRY: EnquiryPreparationHandler(
                    SqlPreparationStore(worker_engine), storage
                ),
                JobKind.EXTRACT_REQUIREMENTS: ExtractionJobHandler(
                    build_enquiry_graph(deps, checkpointer), store
                ),
            },
        )


def submit(client: TestClient, token: str, body: Mapping[str, object], key: str) -> str:
    response = client.post(
        "/api/v1/enquiries",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    return str(response.json()["enquiry_id"])


def drain(worker: JobRunner) -> None:
    while worker.run_once():
        pass


def test_enquiry_is_read_matched_and_priced_by_the_calculator(
    client: TestClient,
    worker: JobRunner,
    make_api_key: ApiKeyFactory,
    make_pdf: Callable[[list[str]], bytes],
    industrial_tenant: UUID,
    runtime_engine: Engine,
) -> None:
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    pdf = base64.b64encode(make_pdf(["Also add 12.5 metres of 2-core cable"])).decode()
    body = {
        "text": "Please quote 2 x IND-FAN-450 and 150 pcs M8 bolts for Pune.",
        "attachments": [
            {"filename": "extra.pdf", "content_type": "application/pdf", "content_base64": pdf}
        ],
    }
    enquiry_id = submit(client, token, body, "m3-ready-0001")
    drain(worker)  # prepare (inspect files) -> extract (LangGraph) -> done

    view = client.get(
        f"/api/v1/enquiries/{enquiry_id}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert view["status"] == "ready_for_quotation"
    lines = view["extraction"]["lines"]
    assert lines == [
        {"sku": "IND-FAN-450", "quantity": "2", "unit": "each"},
        {"sku": "IND-BOLT-M8", "quantity": "150", "unit": "each"},
        {"sku": "IND-CABLE-2C", "quantity": "12.5", "unit": "metre"},
    ]

    # The workflow never prices. The M1 calculator turns the extracted lines into money.
    pricing = QuotationPricingService(
        SimulatedErp(demo_catalogues(INDUSTRIAL_TENANT, OFFICE_TENANT)),
        SqlPricingPolicyReader(runtime_engine),
    )
    sales = TenantContext(
        industrial_tenant, "sales@industrial.example", frozenset({Role.SALES_OPERATOR})
    )
    quote = pricing.price(
        sales, [RequestedLine(row["sku"], Decimal(row["quantity"]), row["unit"]) for row in lines]
    )
    assert isinstance(quote, PricedQuotation)
    # 2 x 4250.00 + 150 x 2.375 + 12.5 x 38.40 = 9336.25; 18% tax = 1680.53
    assert quote.total == Money(Decimal("11016.78"), "INR")


def test_unclear_request_waits_for_clarification(
    client: TestClient, worker: JobRunner, make_api_key: ApiKeyFactory, industrial_tenant: UUID
) -> None:
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    enquiry_id = submit(client, token, {"text": "Please quote 50 exhaust fans"}, "m3-unclear-1")
    drain(worker)
    view = client.get(
        f"/api/v1/enquiries/{enquiry_id}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert view["status"] == "needs_clarification"
    assert view["extraction"]["lines"] == []
    assert "IND-FAN-600" in view["extraction"]["questions"][0]


def test_progress_survives_a_worker_restart(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    worker_engine: Engine,
    storage: LocalObjectStorage,
    integration_settings: Settings,
    erp: SimulatedErp,
    interpreter: CountingInterpreter,
    owner_engine: Engine,
) -> None:
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    enquiry_id = UUID(submit(client, token, {"text": "Please quote 2 x IND-FAN-450"}, "m3-crash-1"))
    store = SqlWorkflowStore(worker_engine)
    prepare = EnquiryPreparationHandler(SqlPreparationStore(worker_engine), storage)
    queue = SqlJobQueue(worker_engine)
    JobRunner(queue, {JobKind.PREPARE_ENQUIRY: prepare}).run_once()

    def extraction_job_runner(checkpointer: object) -> JobRunner:
        deps = EnquiryWorkflowDeps(store, storage, PdfAndTextReader(), interpreter, erp)
        handler = ExtractionJobHandler(build_enquiry_graph(deps, checkpointer), store)  # type: ignore[arg-type]
        return JobRunner(queue, {JobKind.EXTRACT_REQUIREMENTS: handler})

    erp.failures["search_products"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    with open_checkpointer(integration_settings.worker_url()) as first_process:
        extraction_job_runner(first_process).run_once()  # extract saved, match fails -> retry
    assert interpreter.extract_calls == 1

    del erp.failures["search_products"]
    with worker_engine.begin() as connection:  # skip the backoff wait
        connection.execute(text("UPDATE jobs SET run_at = now() WHERE status = 'pending'"))
    with open_checkpointer(integration_settings.worker_url()) as second_process:
        extraction_job_runner(second_process).run_once()
    assert interpreter.extract_calls == 1  # resumed from the PostgreSQL checkpoint

    with tenant_transaction(owner_engine, industrial_tenant) as connection:
        status = connection.execute(
            text("SELECT status FROM enquiries WHERE id = :id"), {"id": enquiry_id}
        ).scalar_one()
    assert status == "ready_for_quotation"


def test_checkpoints_and_runs_are_isolated(
    client: TestClient,
    worker: JobRunner,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    office_tenant: UUID,
    runtime_engine: Engine,
    worker_engine: Engine,
) -> None:
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    submit(client, token, {"text": "Please quote 2 x IND-FAN-450"}, "m3-isolation-1")
    drain(worker)
    with worker_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM checkpoints")).scalar_one() > 0
    with pytest.raises(ProgrammingError), runtime_engine.connect() as connection:
        connection.execute(text("SELECT count(*) FROM checkpoints"))
    for tenant, expected in ((industrial_tenant, 1), (office_tenant, 0)):
        with tenant_transaction(runtime_engine, tenant) as connection:
            runs = connection.execute(text("SELECT count(*) FROM workflow_runs")).scalar_one()
            assert runs == expected


# --- Phase 4: follow-ups are read in rounds -------------------------------------------------


def rounds(owner_engine: Engine, tenant_id: UUID, enquiry_id: str) -> list[tuple[int, str]]:
    with tenant_transaction(owner_engine, tenant_id) as connection:
        return [
            (row.round, row.status)
            for row in connection.execute(
                text(
                    "SELECT round, status FROM workflow_runs WHERE enquiry_id = :e "
                    "AND graph_version LIKE 'enquiry-extraction-%' ORDER BY round"
                ),
                {"e": enquiry_id},
            )
        ]


def test_an_answer_completes_the_enquiry_in_round_two(
    client: TestClient,
    worker: JobRunner,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    owner_engine: Engine,
) -> None:
    """Q6 variant for the rules reader: unit question, then a bare answer."""
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    first = {"text": "We need 5 boxes of 2-core cable", "customer_reference": "CUST-1"}
    enquiry_id = submit(client, token, first, "p4-round-0001")
    drain(worker)
    auth = {"Authorization": f"Bearer {token}"}
    view = client.get(f"/api/v1/enquiries/{enquiry_id}", headers=auth).json()
    assert view["status"] == "needs_clarification"

    answer = client.post(
        "/api/v1/enquiries",
        json={"text": "10 metres", "customer_reference": "CUST-1"},
        headers={**auth, "Idempotency-Key": "p4-round-0002"},
    ).json()
    assert answer["enquiry_id"] == enquiry_id and answer["continued"] is True
    drain(worker)
    view = client.get(f"/api/v1/enquiries/{enquiry_id}", headers=auth).json()
    assert view["status"] == "ready_for_quotation"
    assert view["extraction"]["lines"] == [
        {"sku": "IND-CABLE-2C", "quantity": "10", "unit": "metre"}
    ]
    assert rounds(owner_engine, industrial_tenant, enquiry_id) == [
        (1, "needs_clarification"),
        (2, "ready_for_quotation"),
    ]


def test_a_message_arriving_while_reading_causes_another_round(
    client: TestClient,
    worker: JobRunner,
    interpreter: CountingInterpreter,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    owner_engine: Engine,
) -> None:
    """Q3: the round that missed the new message is history; the next one reads both."""
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    body = {"text": "Please quote 2 x IND-FAN-450", "customer_reference": "CUST-9"}
    enquiry_id = submit(client, token, body, "p4-rapid-0001")
    second = {"text": "and 150 pcs M8 bolts", "customer_reference": "CUST-9"}
    sent: list[str] = []
    interpreter.while_reading.append(
        lambda: sent.append(submit(client, token, second, "p4-rapid-0002"))
    )
    drain(worker)
    assert sent == [enquiry_id]  # it joined the enquiry being read
    view = client.get(
        f"/api/v1/enquiries/{enquiry_id}", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert view["status"] == "ready_for_quotation"
    assert view["extraction"]["lines"] == [
        {"sku": "IND-FAN-450", "quantity": "2", "unit": "each"},
        {"sku": "IND-BOLT-M8", "quantity": "150", "unit": "each"},
    ]
    assert rounds(owner_engine, industrial_tenant, enquiry_id) == [
        (1, "ready_for_quotation"),  # recorded as history only; it had not seen the bolts
        (2, "ready_for_quotation"),
    ]
    with tenant_transaction(owner_engine, industrial_tenant) as connection:
        drafts = connection.execute(
            text("SELECT count(*) FROM jobs WHERE kind = 'draft_quotation'")
        ).scalar_one()
    assert drafts == 1
