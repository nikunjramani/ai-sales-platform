"""Quotation approval workflow (LangGraph with human-in-the-loop interrupts).

    START -> draft --priced--> request_approval -> await_decision -> apply_decision
               \\                     ^                                  |  approve -> approved
                \\--pricing failed--> await_revision <-- request_revision -+  reject  -> rejected

    approved -> await_customer -> apply_customer -> end  (accepted, declined, expired, withdrawn)
                                        | prices changed -> request_approval (revised version)
                                        |                   or await_revision (cannot be priced)

- draft:            price the extracted lines with the M1 calculator; store version 1, or the
                    pricing issues if the ERP facts cannot price them.
- request_approval: create the approval task for the current version (and its notification job).
- await_decision:   pause (interrupt) until a manager decides. The process can stop meanwhile;
                    the pause lives in the PostgreSQL checkpoint.
- apply_decision:   read the recorded decision (the approval service already stored it).
- await_revision:   pause until a sales operator stores a new version.
- approved:         queue delivery of the approved version to the customer (M5).
- await_customer:   pause until the customer answers, staff withdraw, or validity ends (M6).
- apply_customer:   closing answers end the run. "Prices changed" continues with the revised
                    version (request_approval), or with await_revision if it cannot be priced.

Resume jobs carry what they answer (a decision, a new version or a customer answer). A job that
does not match what the run is waiting for is stale and ignored, so a duplicate never applies
twice.
"""

from dataclasses import dataclass
from typing import TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from sales_platform.application.pricing import QuotationPricingService
from sales_platform.domain.acceptance import SYSTEM_SUBJECT, ResponseOutcome
from sales_platform.domain.documents import NoDeliveryAddress
from sales_platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.domain.pricing import PricingRejected
from sales_platform.domain.quotations import (
    ApprovalAction,
    content_sha256,
    pricing_issues_content,
    quotation_content,
)
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.ports.acceptance import ResponseStore
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.ports.quotations import QuotationWorkflowStore

# Still v1: M6 only added steps after "approved", so runs paused at earlier steps carry on.
GRAPH_VERSION = "quotation-approval-v1"
WAITING_FOR_DECISION = "decision"
WAITING_FOR_REVISION = "revision"
WAITING_FOR_CUSTOMER = "customer"


class QuotationState(TypedDict, total=False):
    tenant_id: str
    enquiry_id: str
    run_id: str
    quotation_id: str
    version: int | None
    task_id: str
    decision_id: str
    action: str
    response_id: str


type QuotationGraph = CompiledStateGraph[QuotationState, None, QuotationState, QuotationState]


class WorkflowNotWaiting(Exception):
    """The run has not reached the expected pause yet (or another worker holds it). Retried."""


@dataclass(frozen=True)
class QuotationWorkflowDeps:
    store: QuotationWorkflowStore
    pricing: QuotationPricingService
    responses: ResponseStore


def _tenant(state: QuotationState) -> UUID:
    return UUID(state["tenant_id"])


def build_quotation_graph(
    deps: QuotationWorkflowDeps, checkpointer: BaseCheckpointSaver[str]
) -> QuotationGraph:
    store = deps.store

    def draft(state: QuotationState) -> QuotationState:
        tenant_id = _tenant(state)
        enquiry_id = UUID(state["enquiry_id"])
        lines = store.extraction_lines(tenant_id, enquiry_id)
        if not lines:
            raise PermanentJobError("Enquiry has no extracted lines ready for quotation")
        # The workflow drafts as a system sales identity; people still approve.
        context = TenantContext(tenant_id, SYSTEM_SUBJECT, frozenset({Role.SALES_OPERATOR}))
        outcome = deps.pricing.price(context, lines)  # ERP errors propagate and are retried
        if isinstance(outcome, PricingRejected):
            quotation_id, version = store.create_quotation(
                tenant_id,
                enquiry_id,
                None,
                None,
                pricing_issues_content(outcome.issues),
                SYSTEM_SUBJECT,
            )
        else:
            content = quotation_content(outcome)
            quotation_id, version = store.create_quotation(
                tenant_id, enquiry_id, content, content_sha256(content), None, SYSTEM_SUBJECT
            )
        return {"quotation_id": str(quotation_id), "version": version}

    def request_approval(state: QuotationState) -> QuotationState:
        version = state.get("version")
        if version is None:
            raise PermanentJobError("No version to approve")
        task_id = store.request_approval(_tenant(state), UUID(state["quotation_id"]), version)
        return {"task_id": str(task_id)}

    def await_decision(state: QuotationState) -> QuotationState:
        # Nodes re-run from the top when resumed, so nothing with side effects goes here.
        decision_id = interrupt({"waiting_for": WAITING_FOR_DECISION, "task_id": state["task_id"]})
        return {"decision_id": str(decision_id)}

    def apply_decision(state: QuotationState) -> QuotationState:
        decision = store.decision(_tenant(state), UUID(state["decision_id"]))
        if decision is None or str(decision.task_id) != state["task_id"]:
            raise PermanentJobError("Decision does not belong to the awaited approval task")
        return {"action": decision.action.value}

    def await_revision(state: QuotationState) -> QuotationState:
        after = state.get("version")
        version = interrupt({"waiting_for": WAITING_FOR_REVISION, "after_version": after})
        return {"version": int(version)}

    def check_revision(state: QuotationState) -> QuotationState:
        current = store.current_version(_tenant(state), UUID(state["quotation_id"]))
        if current != state.get("version"):
            raise PermanentJobError("Resumed with a version that is not the current one")
        return {}

    def approved(state: QuotationState) -> QuotationState:
        # Hand the approved version to the delivery outbox (rendering and sending happen in
        # the deliver_quotation job, with its own retries), then end the run.
        tenant_id, version = _tenant(state), state.get("version")
        try:
            queued = version is not None and store.queue_delivery(
                tenant_id, UUID(state["quotation_id"]), version
            )
        except NoDeliveryAddress as error:
            raise PermanentJobError(str(error)) from None
        if not queued:
            raise PermanentJobError("The approved version is not the quotation's current one")
        return {}

    def await_customer(state: QuotationState) -> QuotationState:
        response_id = interrupt(
            {"waiting_for": WAITING_FOR_CUSTOMER, "version": state.get("version")}
        )
        return {"response_id": str(response_id)}

    def apply_customer(state: QuotationState) -> QuotationState:
        response = deps.responses.response(_tenant(state), UUID(state["response_id"]))
        if response is None or response.version != state.get("version"):
            raise PermanentJobError("Response does not belong to the awaited version")
        if response.outcome is ResponseOutcome.REFUSED_CHANGED:
            # The revised version (if it could be priced) was stored with the response.
            return {"action": response.outcome.value, "version": response.revised_version}
        store.finish(_tenant(state), UUID(state["run_id"]), response.outcome.value)
        return {"action": response.outcome.value}

    def rejected(state: QuotationState) -> QuotationState:
        store.finish(_tenant(state), UUID(state["run_id"]), "rejected")
        return {}

    graph = StateGraph(QuotationState)
    graph.add_node("draft", draft)
    graph.add_node("request_approval", request_approval)
    graph.add_node("await_decision", await_decision)
    graph.add_node("apply_decision", apply_decision)
    graph.add_node("await_revision", await_revision)
    graph.add_node("check_revision", check_revision)
    graph.add_node("approved", approved)
    graph.add_node("await_customer", await_customer)
    graph.add_node("apply_customer", apply_customer)
    graph.add_node("rejected", rejected)
    graph.add_edge(START, "draft")
    graph.add_conditional_edges(
        "draft",
        lambda state: "request_approval" if state.get("version") else "await_revision",
        ["request_approval", "await_revision"],
    )
    graph.add_edge("request_approval", "await_decision")
    graph.add_edge("await_decision", "apply_decision")
    graph.add_conditional_edges(
        "apply_decision",
        lambda state: {
            ApprovalAction.APPROVE.value: "approved",
            ApprovalAction.REJECT.value: "rejected",
        }.get(state["action"], "await_revision"),
        ["approved", "rejected", "await_revision"],
    )
    graph.add_edge("await_revision", "check_revision")
    graph.add_edge("check_revision", "request_approval")
    graph.add_edge("approved", "await_customer")
    graph.add_edge("await_customer", "apply_customer")

    def after_customer(state: QuotationState) -> str:
        if state["action"] != ResponseOutcome.REFUSED_CHANGED.value:
            return END
        return "request_approval" if state.get("version") else "await_revision"

    graph.add_conditional_edges(
        "apply_customer", after_customer, ["request_approval", "await_revision", END]
    )
    graph.add_edge("rejected", END)
    return graph.compile(checkpointer=checkpointer)


def _resume_value(waiting: object, payload: dict[str, str]) -> str | int | None:
    """What to resume with, or None if this job does not answer the current pause."""
    if not isinstance(waiting, dict) or waiting.get("waiting_for") != payload.get("waiting_for"):
        return None
    if waiting["waiting_for"] == WAITING_FOR_DECISION:
        if payload.get("task_id") == waiting.get("task_id") and payload.get("decision_id"):
            return payload["decision_id"]
        return None
    if waiting["waiting_for"] == WAITING_FOR_CUSTOMER:
        same_version = payload.get("version") == str(waiting.get("version"))
        return payload["response_id"] if same_version and payload.get("response_id") else None
    try:
        version = int(payload["version"])
    except (KeyError, ValueError):
        return None
    after = waiting.get("after_version")
    return version if after is None or version > int(after) else None


class QuotationJobHandler:
    """Handles draft_quotation (start) and resume_quotation (continue after a pause) jobs.

    A per-run lock makes sure only one worker advances a run at a time.
    """

    def __init__(self, graph: QuotationGraph, store: QuotationWorkflowStore) -> None:
        self._graph = graph
        self._store = store

    def __call__(self, job: ClaimedJob) -> None:
        try:
            enquiry_id = UUID(job.payload["enquiry_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid enquiry_id") from None
        run_id = self._store.start_run(job.require_tenant(), enquiry_id, GRAPH_VERSION)
        config: RunnableConfig = {"configurable": {"thread_id": str(run_id)}}
        with self._store.lock_run(run_id) as locked:
            if not locked:
                raise WorkflowNotWaiting("Another worker is advancing this run")
            snapshot = self._graph.get_state(config)
            if not snapshot.values:
                if job.kind is not JobKind.DRAFT_QUOTATION:
                    raise WorkflowNotWaiting("The run has not started yet")
                start: QuotationState = {
                    "tenant_id": str(job.require_tenant()),
                    "enquiry_id": str(enquiry_id),
                    "run_id": str(run_id),
                }
                self._graph.invoke(start, config)
                return
            if snapshot.next and not snapshot.interrupts:
                self._graph.invoke(None, config)  # an earlier attempt crashed mid-step
                snapshot = self._graph.get_state(config)
            if job.kind is JobKind.DRAFT_QUOTATION:
                return
            if not snapshot.interrupts:
                if snapshot.next:
                    raise WorkflowNotWaiting("The run is not paused yet")
                return  # finished: nothing is waiting any more
            value = _resume_value(snapshot.interrupts[0].value, job.payload)
            if value is not None:
                self._graph.invoke(Command(resume=value), config)
