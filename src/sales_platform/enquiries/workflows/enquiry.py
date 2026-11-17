"""Enquiry extraction workflow (LangGraph).

    START -> extract -> match -> assess -> ready | clarify | review -> END
                 \\________(model answer invalid)______/

- extract: read every customer message of the enquiry and its clean attachments, ask the
  interpreter for items, then check every item against the customer's own words, each message
  and document on its own (invented facts are flagged).
- match:   search the tenant's ERP per item; the interpreter may only pick among ERP results.
- assess:  deterministic rules decide ready / needs clarification / needs review.
- finish:  record the outcome on the enquiry.

Each reading is one round (phase 4): a follow-up message starts the next round on a fresh
thread, and the round records the newest message it read so a message that arrives meanwhile
causes another round instead of being missed. Each step is checkpointed, so after a crash the
job resumes at the step that failed instead of asking the model again. State holds IDs and
small summaries only; document text is never checkpointed. Prices are not involved here:
pricing stays with the M1 calculator.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sales_platform.catalogue.domain.catalogue import Product
from sales_platform.catalogue.ports.sources import CatalogueSourceMissing, ProductSource
from sales_platform.conversations.ports.conversations import CustomerNotifier
from sales_platform.enquiries.domain.requirements import (
    ExtractedRequirements,
    ExtractionOutcome,
    MatchStatus,
    ProductMatch,
    RequestedItem,
    assess,
    decide_match,
    needs_categories,
    unverified_items,
)
from sales_platform.enquiries.ports.interpretation import (
    DocumentReader,
    EnquiryInterpreter,
    InterpretationRejected,
)
from sales_platform.enquiries.ports.workflow import ReadingCap, WorkflowStore
from sales_platform.integrations.domain.messages import TemplateKey
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.platform.ports.storage import ObjectStorage

GRAPH_VERSION = "enquiry-extraction-v1"
SYSTEM_SUBJECT = "system:enquiry-workflow"
SEARCH_LIMIT = 5
INVALID_MODEL_ANSWER = "The model's answer was not in the required format"
NO_PRODUCT_SOURCE = "No product source is connected"
READING_LIMIT = "Daily reading limit reached; a person should read this enquiry"


class EnquiryState(TypedDict, total=False):
    tenant_id: str
    enquiry_id: str
    run_id: str
    rejected: bool
    items: list[dict[str, str | None]]
    unverified: list[int]
    unsupported_documents: list[str]
    matches: list[dict[str, object]]
    products: list[dict[str, str]]
    outcome: str
    lines: list[dict[str, str]]
    questions: list[str]
    review_reasons: list[str]
    no_source: bool  # the business has no product source connected (P1-Q3)
    seq_upto: int  # the newest customer message this round read (phase 4)


type EnquiryGraph = CompiledStateGraph[EnquiryState, None, EnquiryState, EnquiryState]


@dataclass(frozen=True)
class EnquiryWorkflowDeps:
    store: WorkflowStore
    storage: ObjectStorage
    reader: DocumentReader
    interpreter: EnquiryInterpreter
    erp: ProductSource
    # Writes the questions to the customer's tool (phase 4); None in offline evaluations.
    notifier: CustomerNotifier | None = None


# Checkpointed state uses plain strings so it serialises safely and stays readable.
def _item_to_state(item: RequestedItem) -> dict[str, str | None]:
    return {
        "source_quote": item.source_quote,
        "product_text": item.product_text,
        "quantity": None if item.quantity is None else str(item.quantity),
        "unit": item.unit,
        "quantity_quote": item.quantity_quote,
    }


def _item_from_state(data: dict[str, str | None]) -> RequestedItem:
    quantity = data["quantity"]
    return RequestedItem(
        source_quote=str(data["source_quote"]),
        product_text=str(data["product_text"]),
        quantity=None if quantity is None else Decimal(quantity),
        unit=data["unit"],
        quantity_quote=data.get("quantity_quote"),  # absent before phase 4
    )


def _product_to_state(product: Product) -> dict[str, str]:
    return {
        "sku": product.sku,
        "description": product.description,
        "unit": product.unit,
        "minimum_quantity": str(product.minimum_quantity),
        "quantity_step": str(product.quantity_step),
        "category": product.category or "",
    }


def _product_from_state(data: dict[str, str]) -> Product:
    return Product(
        data["sku"],
        data["description"],
        data["unit"],
        Decimal(data["minimum_quantity"]),
        Decimal(data["quantity_step"]),
        data.get("category") or None,  # absent in checkpoints from before categories
    )


def _ids(state: EnquiryState) -> tuple[UUID, UUID]:
    return UUID(state["tenant_id"]), UUID(state["enquiry_id"])


def build_enquiry_graph(
    deps: EnquiryWorkflowDeps, checkpointer: BaseCheckpointSaver[str]
) -> EnquiryGraph:
    def extract(state: EnquiryState) -> EnquiryState:
        tenant_id, enquiry_id = _ids(state)
        content = deps.store.enquiry_content(tenant_id, enquiry_id)
        if content is None:
            raise PermanentJobError("Enquiry not found for this tenant")
        documents = [
            deps.reader.read(item.filename, item.content_type, deps.storage.get(item.storage_key))
            for item in content.attachments
        ]
        unsupported = [
            f"{doc.name} ({doc.unsupported_reason})" for doc in documents if not doc.text
        ]
        transcript = content.transcript
        try:
            requirements = deps.interpreter.extract(transcript, documents)
        except InterpretationRejected:
            return {
                "rejected": True,
                "items": [],
                "unverified": [],
                "unsupported_documents": unsupported,
                "seq_upto": content.last_message_seq,
            }
        # Each message and document is a separate source; our own questions never are.
        sources = transcript.texts + [doc.text for doc in documents if doc.text]
        return {
            "rejected": False,
            "items": [_item_to_state(item) for item in requirements.items],
            "unverified": unverified_items(requirements, sources, transcript.texts),
            "unsupported_documents": unsupported,
            "seq_upto": content.last_message_seq,
        }

    def match(state: EnquiryState) -> EnquiryState:
        tenant_id, _ = _ids(state)
        # A read-only system identity: the workflow may look up products, nothing more.
        context = TenantContext(tenant_id, SYSTEM_SUBJECT, frozenset({Role.VIEWER}))
        matches: list[dict[str, object]] = []
        products: dict[str, Product] = {}
        for index, data in enumerate(state["items"]):
            if index in state["unverified"]:
                continue
            item = _item_from_state(data)
            try:
                candidates = deps.erp.search_products(context, item.product_text, SEARCH_LIMIT)
            except CatalogueSourceMissing:
                # Nothing can be matched until the business connects a product source.
                return {"matches": [], "products": [], "no_source": True}
            chosen = (
                deps.interpreter.choose_product(item, candidates) if len(candidates) > 1 else None
            )
            decided = decide_match(index, candidates, chosen)
            products.update((product.sku, product) for product in candidates)
            matches.append(
                {
                    "item_index": index,
                    "status": decided.status.value,
                    "sku": decided.sku,
                    "candidate_skus": list(decided.candidate_skus),
                }
            )
        return {"matches": matches, "products": [_product_to_state(p) for p in products.values()]}

    def assess_step(state: EnquiryState) -> EnquiryState:
        no_source: EnquiryState = {
            "outcome": ExtractionOutcome.NEEDS_REVIEW.value,
            "lines": [],
            "questions": [],
            "review_reasons": [NO_PRODUCT_SOURCE],
        }
        if state.get("no_source"):
            return no_source
        if state.get("rejected"):
            reasons = [INVALID_MODEL_ANSWER] + [
                f"Attachment {name!r} could not be read; a person should check it"
                for name in state["unsupported_documents"]
            ]
            return {
                "outcome": ExtractionOutcome.NEEDS_REVIEW.value,
                "lines": [],
                "questions": [],
                "review_reasons": reasons,
            }
        requirements = ExtractedRequirements(
            tuple(_item_from_state(data) for data in state["items"])
        )
        matches = [
            ProductMatch(
                int(str(data["item_index"])),
                MatchStatus(str(data["status"])),
                None if data["sku"] is None else str(data["sku"]),
                tuple(str(sku) for sku in data["candidate_skus"]),  # type: ignore[attr-defined]
            )
            for data in state.get("matches", [])
        ]
        categories: Sequence[str] = ()
        if needs_categories(requirements, matches, state["unverified"]):
            tenant_id, _ = _ids(state)
            context = TenantContext(tenant_id, SYSTEM_SUBJECT, frozenset({Role.VIEWER}))
            try:
                categories = deps.erp.list_categories(context)
            except CatalogueSourceMissing:
                return no_source
        result = assess(
            requirements,
            matches,
            {data["sku"]: _product_from_state(data) for data in state.get("products", [])},
            state["unverified"],
            state["unsupported_documents"],
            categories,
        )
        return {
            "outcome": result.outcome.value,
            "lines": [
                {"sku": line.sku, "quantity": str(line.quantity), "unit": line.unit}
                for line in result.lines
            ],
            "questions": list(result.questions),
            "review_reasons": list(result.review_reasons),
        }

    def record(state: EnquiryState, outcome: ExtractionOutcome) -> bool:
        tenant_id, enquiry_id = _ids(state)
        details: dict[str, object] = {
            "graph_version": GRAPH_VERSION,
            "items": state.get("items", []),
            "matches": state.get("matches", []),
            "lines": state.get("lines", []),
            "questions": state.get("questions", []),
            "review_reasons": state.get("review_reasons", []),
            "unsupported_documents": state.get("unsupported_documents", []),
            "message_seq_upto": state.get("seq_upto"),
        }
        return deps.store.record_outcome(
            tenant_id, enquiry_id, UUID(state["run_id"]), outcome, details
        )

    def ready(state: EnquiryState) -> EnquiryState:
        record(state, ExtractionOutcome.READY_FOR_QUOTATION)
        return {}

    def clarify(state: EnquiryState) -> EnquiryState:
        applied = record(state, ExtractionOutcome.NEEDS_CLARIFICATION)
        questions = state.get("questions", [])
        if applied and questions and deps.notifier is not None:
            # Our fixed questions go to the customer's tool. A repeat of this step (after a
            # crash) reuses the key, so the outbox never sends them twice.
            tenant_id, enquiry_id = _ids(state)
            deps.notifier.send(
                tenant_id,
                enquiry_id,
                TemplateKey.CLARIFICATION_QUESTIONS,
                {"questions": questions},
                f"questions:{state['run_id']}",
                UUID(state["run_id"]),
            )
        return {}

    def review(state: EnquiryState) -> EnquiryState:
        record(state, ExtractionOutcome.NEEDS_REVIEW)
        return {}

    graph = StateGraph(EnquiryState)
    graph.add_node("extract", extract)
    graph.add_node("match", match)
    graph.add_node("assess", assess_step)
    graph.add_node("ready", ready)
    graph.add_node("clarify", clarify)
    graph.add_node("review", review)
    graph.add_edge(START, "extract")
    graph.add_conditional_edges(
        "extract", lambda state: "assess" if state.get("rejected") else "match", ["match", "assess"]
    )
    graph.add_edge("match", "assess")
    graph.add_conditional_edges(
        "assess",
        lambda state: {
            ExtractionOutcome.READY_FOR_QUOTATION.value: "ready",
            ExtractionOutcome.NEEDS_CLARIFICATION.value: "clarify",
        }.get(state["outcome"], "review"),
        ["ready", "clarify", "review"],
    )
    for node in ("ready", "clarify", "review"):
        graph.add_edge(node, END)
    return graph.compile(checkpointer=checkpointer)


class ExtractionJobHandler:
    """Job handler: start the graph for an enquiry, or resume it from its last checkpoint.

    With a ReadingCap, a new reading of an enquiry from a capped tool (hosted chat) is not
    started once the business used its daily budget: the enquiry goes to review with
    READING_LIMIT instead, and the model is not called (user answer, 2026-10-28).
    """

    def __init__(
        self, graph: EnquiryGraph, store: WorkflowStore, cap: ReadingCap | None = None
    ) -> None:
        self._graph = graph
        self._store = store
        self._cap = cap

    def __call__(self, job: ClaimedJob) -> None:
        try:
            enquiry_id = UUID(job.payload["enquiry_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid enquiry_id") from None
        try:
            round_number = int(job.payload.get("round", "1"))  # jobs before phase 4: round 1
        except ValueError:
            raise PermanentJobError("Job payload has no valid round") from None
        run_id = self._store.start_run(
            job.require_tenant(), enquiry_id, GRAPH_VERSION, round_number
        )
        config: RunnableConfig = {"configurable": {"thread_id": str(run_id)}}
        snapshot = self._graph.get_state(config)
        if snapshot.values and not snapshot.next:
            return  # finished on an earlier attempt
        if snapshot.next:
            self._graph.invoke(None, config)  # resume after a crash
        elif self._over_cap(job.require_tenant(), enquiry_id, run_id):
            self._store.record_outcome(
                job.require_tenant(),
                enquiry_id,
                run_id,
                ExtractionOutcome.NEEDS_REVIEW,
                {
                    "graph_version": GRAPH_VERSION,
                    "items": [],
                    "matches": [],
                    "lines": [],
                    "questions": [],
                    "review_reasons": [READING_LIMIT],
                    "unsupported_documents": [],
                    "reading_limit": True,
                },
            )
        else:
            start: EnquiryState = {
                "tenant_id": str(job.require_tenant()),
                "enquiry_id": str(enquiry_id),
                "run_id": str(run_id),
            }
            self._graph.invoke(start, config)

    def _over_cap(self, tenant_id: UUID, enquiry_id: UUID, run_id: UUID) -> bool:
        if self._cap is None:
            return False
        used = self._store.readings_today(tenant_id, enquiry_id, run_id, self._cap.provider_keys)
        return used is not None and used >= self._cap.per_business_day
