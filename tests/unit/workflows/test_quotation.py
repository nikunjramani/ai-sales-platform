"""Quotation approval graph: draft, pause for a decision, resume, revision loop, crash/restart."""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.domain.pricing import RequestedLine
from sales_platform.domain.quotations import ApprovalAction, QuotationStatus, TaskStatus
from sales_platform.ports.erp import ErpError, ErpErrorCode
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.workflows.quotation import (
    GRAPH_VERSION,
    QuotationWorkflowDeps,
    WorkflowNotWaiting,
    build_quotation_graph,
)

FAN = ("IND-FAN-450", "2", "each")
PUMP = ("IND-PUMP-2HP", "1", "each")  # no price in the simulated ERP
MANAGER = "manager@industrial.example"
SALES = "sales@industrial.example"


def fans(quantity: int) -> RequestedLine:
    return RequestedLine("IND-FAN-450", Decimal(quantity), "each")


def job(tenant: UUID, kind: JobKind, payload: dict[str, str]) -> ClaimedJob:
    return ClaimedJob(uuid4(), tenant, kind, payload, 1, 5, uuid4(), uuid4())


def run_id(harness: Any, enquiry: UUID) -> UUID:
    found: UUID = harness.db.runs[(harness.tenant_id, enquiry, GRAPH_VERSION)]
    return found


def waiting_for(harness: Any, enquiry: UUID) -> object:
    """What the paused run waits for (its interrupt value), or None if it is not paused."""
    graph = build_quotation_graph(
        QuotationWorkflowDeps(harness.db, harness.pricing, harness.db), harness.checkpointer
    )
    state = graph.get_state({"configurable": {"thread_id": str(run_id(harness, enquiry))}})
    return state.interrupts[0].value if state.interrupts else None


def test_priced_draft_waits_for_a_manager_then_for_the_customer(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    quotation = harness.quotation(enquiry)
    assert quotation.status is QuotationStatus.PENDING_APPROVAL and quotation.current_version == 1
    task = harness.pending_task(enquiry)
    version = harness.db.versions[0].view
    assert version.content["total"] == "10030.00" and version.created_by.startswith("system:")
    assert task.content_sha256 == version.content_sha256

    harness.approvals.decide(MANAGER, harness.command(task))
    assert harness.quotation(enquiry).status is QuotationStatus.APPROVED
    assert [job.kind for job in harness.db.jobs] == [JobKind.RESUME_QUOTATION]
    harness.run_jobs()
    # Approved: delivered, and the run now waits for the customer's answer (M6).
    assert run_id(harness, enquiry) not in harness.db.run_status
    assert waiting_for(harness, enquiry) == {"waiting_for": "customer", "version": 1}


def test_reject_closes_the_quotation(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    task = harness.pending_task(enquiry)
    harness.approvals.decide(MANAGER, harness.command(task, ApprovalAction.REJECT, "Too low"))
    harness.run_jobs()
    assert harness.quotation(enquiry).status is QuotationStatus.REJECTED
    assert harness.db.run_status[run_id(harness, enquiry)] == "rejected"


def test_revision_loop_creates_a_new_version_and_a_new_approval_task(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    first = harness.pending_task(enquiry)
    harness.approvals.decide(
        MANAGER, harness.command(first, ApprovalAction.REQUEST_REVISION, "Quote 3 fans")
    )
    harness.run_jobs()  # resumes and pauses again, waiting for a revision
    assert harness.quotation(enquiry).status is QuotationStatus.REVISION_REQUESTED

    quotation = harness.quotation(enquiry)
    result = harness.quotations.revise(
        harness.context(SALES), quotation.quotation_id, uuid4(), 1, [fans(3)], "Customer wants 3"
    )
    assert result.version == 2
    harness.run_jobs()
    second = harness.pending_task(enquiry)
    assert second.version == 2 and second.task_id != first.task_id
    assert harness.db.tasks[first.task_id].status is TaskStatus.REVISION_REQUESTED

    harness.approvals.decide(MANAGER, harness.command(second))
    harness.run_jobs()
    assert waiting_for(harness, enquiry) == {"waiting_for": "customer", "version": 2}


def test_unpriceable_request_waits_for_a_revision_instead_of_inventing_a_price(
    harness: Any,
) -> None:
    enquiry = harness.enquiry(PUMP)
    harness.run_jobs()
    quotation = harness.quotation(enquiry)
    assert quotation.status is QuotationStatus.PRICING_FAILED
    assert quotation.current_version is None and harness.db.versions == []
    assert quotation.pricing_issues[0]["code"] == "price_missing"
    assert harness.db.tasks == {}

    harness.quotations.revise(
        harness.context(SALES), quotation.quotation_id, uuid4(), None, [fans(1)], "Pump -> fan"
    )
    harness.run_jobs()
    assert harness.pending_task(enquiry).version == 1


def test_duplicate_and_stale_resume_jobs_change_nothing(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    task = harness.pending_task(enquiry)
    harness.approvals.decide(
        MANAGER, harness.command(task, ApprovalAction.REQUEST_REVISION, "Check qty")
    )
    resume = harness.db.jobs[0]
    harness.run_jobs()
    handler = harness.handler()
    # The same decision delivered again: the run now waits for a revision, so it is ignored.
    handler(job(harness.tenant_id, resume.kind, resume.payload))
    stale_revision = {"enquiry_id": str(enquiry), "waiting_for": "revision", "version": "1"}
    handler(job(harness.tenant_id, JobKind.RESUME_QUOTATION, stale_revision))
    assert harness.quotation(enquiry).status is QuotationStatus.REVISION_REQUESTED
    assert len(harness.db.tasks) == 1


def test_resume_before_the_run_exists_is_retried(harness: Any) -> None:
    payload = {"enquiry_id": str(uuid4()), "waiting_for": "decision", "task_id": "x"}
    with pytest.raises(WorkflowNotWaiting):
        harness.handler()(job(harness.tenant_id, JobKind.RESUME_QUOTATION, payload))


def test_a_run_held_by_another_worker_is_retried(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.db.locked_runs.add(harness.db.start_run(harness.tenant_id, enquiry, GRAPH_VERSION))
    with pytest.raises(WorkflowNotWaiting):
        harness.run_jobs()


def test_crash_after_drafting_resumes_without_repricing_or_a_second_task(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    draft = harness.db.jobs[0]
    harness.db.fail_next["request_approval"] = RuntimeError("worker killed")
    with pytest.raises(RuntimeError):
        harness.run_jobs()
    assert harness.quotation(enquiry).current_version == 1  # draft was stored

    # The ERP is now down: a resumed run must not need it, because drafting already happened.
    harness.erp.failures["get_products"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    harness.handler()(job(harness.tenant_id, draft.kind, draft.payload))
    assert harness.pending_task(enquiry).version == 1
    harness.handler()(job(harness.tenant_id, draft.kind, draft.payload))  # duplicate: no-op
    assert len(harness.db.tasks) == 1


def test_erp_outage_during_drafting_is_retried_not_guessed(harness: Any) -> None:
    harness.enquiry(FAN)
    harness.erp.failures["get_prices"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    with pytest.raises(ErpError):
        harness.run_jobs()
    assert harness.db.quotations == {}


def test_enquiry_without_extracted_lines_is_permanent(harness: Any) -> None:
    payload = {"enquiry_id": str(uuid4())}
    with pytest.raises(PermanentJobError):
        harness.handler()(job(harness.tenant_id, JobKind.DRAFT_QUOTATION, payload))
