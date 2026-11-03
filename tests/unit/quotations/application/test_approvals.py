"""The shared approval decision service: authorization, replay, conflicts and staleness."""

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.domain.tenancy import AccessDenied, PermissionDenied, Role
from sales_platform.quotations.domain.pricing import PricingRejected
from sales_platform.quotations.domain.quotations import (
    ApprovalAction,
    DecisionConflict,
    QuotationNotFound,
    QuotationStatus,
    RevisionConflict,
    RevisionNotAllowed,
    SelfApprovalNotAllowed,
    StaleVersion,
    TaskNotFound,
)

FAN = ("IND-FAN-450", "2", "each")
MANAGER = "manager@industrial.example"
SALES = "sales@industrial.example"


@pytest.fixture
def pending(harness: Any) -> tuple[UUID, Any]:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    return enquiry, harness.pending_task(enquiry)


def test_approval_is_recorded_once_and_queues_one_resume(
    harness: Any, pending: tuple[UUID, Any]
) -> None:
    enquiry, task = pending
    command = harness.command(task)
    first = harness.approvals.decide(MANAGER, command)
    assert not first.replayed and first.decision.actor_subject == MANAGER
    assert first.decision.version == 1 and first.decision.transport == "portal"
    again = harness.approvals.decide(MANAGER, command)  # e.g. a network retry
    assert again.replayed and again.decision == first.decision
    assert len(harness.db.take_jobs(JobKind.RESUME_QUOTATION)) == 1
    assert harness.quotation(enquiry).status is QuotationStatus.APPROVED


def test_second_different_decision_is_a_conflict_showing_the_first(
    harness: Any, pending: tuple[UUID, Any]
) -> None:
    _, task = pending
    first = harness.approvals.decide(MANAGER, harness.command(task))
    with pytest.raises(DecisionConflict) as raised:
        harness.approvals.decide(
            MANAGER, harness.command(task, ApprovalAction.REJECT, "changed my mind")
        )
    assert raised.value.recorded == first.decision


def test_reused_command_id_with_a_different_action_is_a_conflict(
    harness: Any, pending: tuple[UUID, Any]
) -> None:
    _, task = pending
    command_id = uuid4()
    harness.approvals.decide(MANAGER, harness.command(task, command_id=command_id))
    with pytest.raises(DecisionConflict):
        harness.approvals.decide(
            MANAGER, harness.command(task, ApprovalAction.REJECT, "no", command_id=command_id)
        )


@pytest.mark.parametrize("subject", [SALES, "viewer@industrial.example"])
def test_only_approvers_decide(harness: Any, pending: tuple[UUID, Any], subject: str) -> None:
    _, task = pending
    with pytest.raises(PermissionDenied):
        harness.approvals.decide(subject, harness.command(task))


def test_other_tenants_and_strangers_cannot_decide(
    harness: Any, pending: tuple[UUID, Any], office_tenant: UUID
) -> None:
    _, task = pending
    with pytest.raises(AccessDenied):
        harness.approvals.decide("stranger@example.test", harness.command(task))
    # An office approver aiming at an industrial task under their own tenant finds nothing.
    harness.db.add_member(office_tenant, "boss@office.example", Role.APPROVER)
    command = harness.command(task)
    other = command.__class__(**{**command.__dict__, "tenant_id": office_tenant})
    with pytest.raises(TaskNotFound):
        harness.approvals.decide("boss@office.example", other)


def test_removed_membership_cannot_act_on_an_open_page(
    harness: Any, pending: tuple[UUID, Any]
) -> None:
    _, task = pending
    del harness.db.roles[(harness.tenant_id, MANAGER)]
    with pytest.raises(AccessDenied):
        harness.approvals.decide(MANAGER, harness.command(task))


def test_decision_on_content_the_manager_did_not_see_is_stale(
    harness: Any, pending: tuple[UUID, Any]
) -> None:
    _, task = pending
    with pytest.raises(StaleVersion):
        harness.approvals.decide(MANAGER, harness.command(task, content_sha256="b" * 64))


def test_creator_of_a_version_cannot_approve_it(harness: Any) -> None:
    harness.db.add_member(harness.tenant_id, SALES, Role.APPROVER)  # holds both roles
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    harness.approvals.decide(
        MANAGER,
        harness.command(harness.pending_task(enquiry), ApprovalAction.REQUEST_REVISION, "qty"),
    )
    harness.run_jobs()
    quotation = harness.quotation(enquiry)
    harness.quotations.revise(
        harness.context(SALES), quotation.quotation_id, uuid4(), 1,
        [RequestedLine("IND-FAN-450", Decimal(3), "each")], "3 fans",
    )  # fmt: skip
    harness.run_jobs()
    task = harness.pending_task(enquiry)
    with pytest.raises(SelfApprovalNotAllowed):
        harness.approvals.decide(SALES, harness.command(task))
    harness.approvals.decide(MANAGER, harness.command(task))  # someone else may


def test_unknown_task_is_not_found(harness: Any, pending: tuple[UUID, Any]) -> None:
    _, task = pending
    command = harness.command(task)
    unknown = command.__class__(**{**command.__dict__, "task_id": uuid4()})
    with pytest.raises(TaskNotFound):
        harness.approvals.decide(MANAGER, unknown)


def test_revision_rules(harness: Any, pending: tuple[UUID, Any]) -> None:
    enquiry, task = pending
    quotation_id = harness.quotation(enquiry).quotation_id
    sales = harness.context(SALES)
    lines = [RequestedLine("IND-FAN-450", Decimal(3), "each")]
    with pytest.raises(RevisionNotAllowed):  # still pending approval
        harness.quotations.revise(sales, quotation_id, uuid4(), 1, lines, "early")
    harness.approvals.decide(MANAGER, harness.command(task, ApprovalAction.REQUEST_REVISION, "qty"))
    with pytest.raises(PermissionDenied):
        harness.quotations.revise(harness.context(MANAGER), quotation_id, uuid4(), 1, lines, "x")
    with pytest.raises(RevisionConflict):  # based on a version that is not current
        harness.quotations.revise(sales, quotation_id, uuid4(), 0, lines, "old page")
    with pytest.raises(ValueError, match="changed"):
        harness.quotations.revise(sales, quotation_id, uuid4(), 1, lines, "  ")
    unpriceable = [RequestedLine("IND-PUMP-2HP", Decimal(1), "each")]
    rejected = harness.quotations.revise(sales, quotation_id, uuid4(), 1, unpriceable, "pump")
    assert isinstance(rejected, PricingRejected)
    assert harness.quotation(enquiry).current_version == 1  # nothing stored

    command_id = uuid4()
    first = harness.quotations.revise(sales, quotation_id, command_id, 1, lines, "3 fans")
    again = harness.quotations.revise(sales, quotation_id, command_id, 1, lines, "3 fans")
    assert (first.version, first.replayed, again.version, again.replayed) == (2, False, 2, True)


def test_quotations_are_invisible_to_other_tenants(
    harness: Any, pending: tuple[UUID, Any], office_tenant: UUID
) -> None:
    enquiry, _ = pending
    office = harness.context("sales@office.example", office_tenant)
    assert harness.quotations.list(office) == []
    with pytest.raises(QuotationNotFound):
        harness.quotations.get(office, harness.quotation(enquiry).quotation_id)
