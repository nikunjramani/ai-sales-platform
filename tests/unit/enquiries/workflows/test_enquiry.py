"""The LangGraph extraction workflow: branches, invented facts, crash/resume and isolation."""

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sales_platform.catalogue.adapters.erp.simulated import SimulatedErp
from sales_platform.catalogue.domain.catalogue import Product
from sales_platform.catalogue.ports.erp import CatalogueSourceMissing, ErpError, ErpErrorCode
from sales_platform.enquiries.adapters.documents.text import PdfAndTextReader
from sales_platform.enquiries.adapters.interpreters.rules import RulesInterpreter
from sales_platform.enquiries.domain.attachments import StorageArea, attachment_key
from sales_platform.enquiries.domain.requirements import (
    ExtractedRequirements,
    ExtractionOutcome,
    RequestedItem,
)
from sales_platform.enquiries.ports.interpretation import (
    DocumentText,
    EnquiryTranscript,
    InterpretationRejected,
)
from sales_platform.enquiries.ports.workflow import CleanAttachment, EnquiryContent
from sales_platform.enquiries.workflows.enquiry import (
    EnquiryWorkflowDeps,
    ExtractionJobHandler,
    build_enquiry_graph,
)
from sales_platform.platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.platform.ports.jobs import ClaimedJob
from tests.support.workflow_store import InMemoryWorkflowStore


class CountingInterpreter:
    """Wraps another interpreter (or a scripted answer) and counts model calls."""

    def __init__(self, answer: ExtractedRequirements | Exception | None = None) -> None:
        self.answer = answer
        self.extract_calls = 0
        self.rules = RulesInterpreter()

    def extract(
        self, transcript: EnquiryTranscript, documents: Sequence[DocumentText]
    ) -> ExtractedRequirements:
        self.extract_calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer or self.rules.extract(transcript, documents)

    def choose_product(self, item: RequestedItem, candidates: Sequence[Product]) -> str | None:
        return self.rules.choose_product(item, candidates)


class Harness:
    def __init__(self, erp: SimulatedErp, storage: Any, interpreter: CountingInterpreter) -> None:
        self.store = InMemoryWorkflowStore()
        self.storage = storage
        self.interpreter = interpreter
        self.erp = erp
        self.checkpointer = InMemorySaver()

    def handler(self) -> ExtractionJobHandler:
        """A fresh graph over the same checkpointer, as after a worker restart."""
        deps = EnquiryWorkflowDeps(
            self.store, self.storage, PdfAndTextReader(), self.interpreter, self.erp
        )
        return ExtractionJobHandler(build_enquiry_graph(deps, self.checkpointer), self.store)

    def enquiry(self, tenant: UUID, text: str, attachments: Sequence[CleanAttachment] = ()) -> UUID:
        enquiry_id = uuid4()
        self.store.contents[(tenant, enquiry_id)] = EnquiryContent(
            EnquiryTranscript.single(text), tuple(attachments)
        )
        return enquiry_id

    def run(self, tenant: UUID, enquiry_id: UUID) -> tuple[ExtractionOutcome, dict[str, Any]]:
        job = ClaimedJob(uuid4(), tenant, JobKind.EXTRACT_REQUIREMENTS,
                         {"enquiry_id": str(enquiry_id)}, 1, 5, uuid4(), uuid4())  # fmt: skip
        self.handler()(job)
        outcome, details = self.store.outcomes[enquiry_id]
        return outcome, dict(details)


@pytest.fixture
def make_harness(simulated_erp: SimulatedErp, memory_storage: Any) -> Callable[..., Harness]:
    def build(answer: ExtractedRequirements | Exception | None = None) -> Harness:
        return Harness(simulated_erp, memory_storage, CountingInterpreter(answer))

    return build


def test_clear_request_is_ready_for_quotation(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    harness = make_harness()
    enquiry = harness.enquiry(
        industrial_tenant, "Please quote 2 x IND-FAN-450 and 150 pcs M8 bolts"
    )
    outcome, details = harness.run(industrial_tenant, enquiry)
    assert outcome is ExtractionOutcome.READY_FOR_QUOTATION
    assert details["lines"] == [
        {"sku": "IND-FAN-450", "quantity": "2", "unit": "each"},
        {"sku": "IND-BOLT-M8", "quantity": "150", "unit": "each"},
    ]


def test_ambiguous_request_asks_a_question(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    harness = make_harness()
    outcome, details = harness.run(
        industrial_tenant, harness.enquiry(industrial_tenant, "Please quote 50 exhaust fans")
    )
    assert outcome is ExtractionOutcome.NEEDS_CLARIFICATION
    assert "IND-FAN-450" in details["questions"][0] and "IND-FAN-600" in details["questions"][0]


def test_pdf_attachment_is_read_and_image_goes_to_review(
    make_harness: Callable[..., Harness],
    industrial_tenant: UUID,
    memory_storage: Any,
    make_pdf: Callable[[list[str]], bytes],
) -> None:
    harness = make_harness()
    pdf_id, image_id = uuid4(), uuid4()
    pdf_key = attachment_key(industrial_tenant, pdf_id, StorageArea.CLEAN)
    image_key = attachment_key(industrial_tenant, image_id, StorageArea.CLEAN)
    memory_storage.put(pdf_key, make_pdf(["Order list", "Please supply 150 pcs M8 bolts"]))
    memory_storage.put(image_key, b"\x89PNG\r\n\x1a\n")
    attachments = [
        CleanAttachment(pdf_id, "order.pdf", "application/pdf", pdf_key),
        CleanAttachment(image_id, "sketch.png", "image/png", image_key),
    ]
    outcome, details = harness.run(
        industrial_tenant, harness.enquiry(industrial_tenant, "See attached.", attachments)
    )
    assert details["items"][0]["product_text"] == "M8 bolts"  # read from the PDF
    assert outcome is ExtractionOutcome.NEEDS_REVIEW
    assert "sketch.png (image_requires_ocr)" in details["review_reasons"][0]


def test_invented_item_is_never_quoted(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    invented = ExtractedRequirements(
        (RequestedItem("100 x IND-PUMP-2HP", "IND-PUMP-2HP", Decimal(100), None),)
    )
    harness = make_harness(invented)
    outcome, details = harness.run(
        industrial_tenant, harness.enquiry(industrial_tenant, "Please quote 2 x IND-FAN-450")
    )
    assert outcome is ExtractionOutcome.NEEDS_REVIEW
    assert details["lines"] == []


def test_invalid_model_answer_goes_to_review(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    harness = make_harness(InterpretationRejected("bad json"))
    outcome, details = harness.run(
        industrial_tenant, harness.enquiry(industrial_tenant, "Please quote 2 x IND-FAN-450")
    )
    assert outcome is ExtractionOutcome.NEEDS_REVIEW
    assert "required format" in details["review_reasons"][0]


def test_unknown_product_reply_names_the_categories(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    harness = make_harness()
    outcome, details = harness.run(
        industrial_tenant, harness.enquiry(industrial_tenant, "Please quote 2 tata sierra cars")
    )
    assert outcome is ExtractionOutcome.NEEDS_CLARIFICATION
    assert details["questions"] == [
        "We could not find 'tata sierra cars' in our catalogue. We supply: Cables, Exhaust fans, "
        "Fasteners, Pumps. Ask about a category to see its products, or share a product code."
    ]


def test_nothing_understood_reply_names_the_categories(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    harness = make_harness()
    _, details = harness.run(
        industrial_tenant, harness.enquiry(industrial_tenant, "can you get price for tata cars")
    )
    assert details["questions"][0].startswith("Which products and quantities")
    assert "We supply: Cables, Exhaust fans, Fasteners, Pumps." in details["questions"][0]


def test_category_lookup_failure_is_retried_not_guessed(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    harness = make_harness()
    enquiry = harness.enquiry(industrial_tenant, "Please quote 2 tata sierra cars")
    harness.erp.failures["list_categories"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    with pytest.raises(ErpError):
        harness.run(industrial_tenant, enquiry)
    del harness.erp.failures["list_categories"]
    outcome, details = harness.run(industrial_tenant, enquiry)
    assert outcome is ExtractionOutcome.NEEDS_CLARIFICATION
    assert "We supply:" in details["questions"][0]


def test_crash_resumes_without_asking_the_model_again(
    make_harness: Callable[..., Harness], industrial_tenant: UUID
) -> None:
    harness = make_harness()
    enquiry = harness.enquiry(industrial_tenant, "Please quote 2 x IND-FAN-450")
    harness.erp.failures["search_products"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    with pytest.raises(ErpError):
        harness.run(industrial_tenant, enquiry)  # extract succeeded, match failed
    assert harness.interpreter.extract_calls == 1

    del harness.erp.failures["search_products"]
    outcome, _ = harness.run(industrial_tenant, enquiry)  # new graph instance, same checkpoints
    assert outcome is ExtractionOutcome.READY_FOR_QUOTATION
    assert harness.interpreter.extract_calls == 1  # resumed at "match"

    harness.run(industrial_tenant, enquiry)  # finished runs are not repeated
    assert harness.interpreter.extract_calls == 1


def test_other_tenants_products_are_not_matched(
    make_harness: Callable[..., Harness], office_tenant: UUID
) -> None:
    harness = make_harness()
    outcome, details = harness.run(
        office_tenant, harness.enquiry(office_tenant, "Please quote 4 x IND-FAN-450")
    )
    assert outcome is ExtractionOutcome.NEEDS_CLARIFICATION
    question = details["questions"][0]
    assert "could not find 'IND-FAN-450' in our catalogue" in question
    # Only this business's own categories are named.
    assert "We supply: Desks, Office chairs." in question
    assert "Exhaust fans" not in question


def test_missing_enquiry_is_permanent(
    make_harness: Callable[..., Harness], industrial_tenant: UUID, office_tenant: UUID
) -> None:
    harness = make_harness()
    enquiry = harness.enquiry(industrial_tenant, "Please quote 2 x IND-FAN-450")
    with pytest.raises(PermanentJobError):
        harness.run(office_tenant, enquiry)  # right ID, wrong tenant


@pytest.mark.parametrize(
    "text", ["Please quote 2 x IND-FAN-450", "can you get price for tata cars"]
)
def test_a_business_without_a_product_source_goes_to_review(
    make_harness: Callable[..., Harness], industrial_tenant: UUID, text: str
) -> None:
    """P1-Q3: no retries, review with a clear reason (whether matching or listing categories)."""
    harness = make_harness()
    for operation in ("search_products", "list_categories"):
        harness.erp.failures[operation] = CatalogueSourceMissing()
    outcome, details = harness.run(industrial_tenant, harness.enquiry(industrial_tenant, text))
    assert outcome is ExtractionOutcome.NEEDS_REVIEW
    assert details["review_reasons"] == ["No product source is connected"]
