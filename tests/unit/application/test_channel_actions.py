"""Manager channel enrollment, review delivery and in-channel decisions (simulated channel)."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.application.approvals import DecisionResult
from sales_platform.application.channel_actions import (
    ChannelActionRejected,
    ChannelEvent,
    ChannelEventKind,
)
from sales_platform.application.notifications import REFERENCE_LIFETIME
from sales_platform.domain.jobs import JobKind
from sales_platform.domain.quotations import ApprovalAction, DecisionConflict, QuotationStatus
from sales_platform.domain.tenancy import PermissionDenied, Role
from sales_platform.ports.channels import ChannelBinding
from sales_platform.ports.jobs import ClaimedJob

MANAGER = "manager@industrial.example"
CONNECTION = "simulated-local"


def event(tenant: UUID, user: str, event_id: str, **fields: Any) -> ChannelEvent:
    return ChannelEvent(
        channel="simulated",
        connection_id=fields.pop("connection_id", CONNECTION),
        tenant_id=tenant,
        event_id=event_id,
        external_user_id=user,
        **fields,
    )


def enroll(harness: Any, subject: str, user: str) -> ChannelBinding:
    code = harness.channels.start_enrollment(harness.context(subject), "simulated").code
    result = harness.channels.handle(
        event(harness.tenant_id, user, f"enroll-{user}", kind=ChannelEventKind.ENROLL, code=code)
    )
    assert isinstance(result, ChannelBinding)
    return result


def decide(harness: Any, user: str, event_id: str, reference: str, **fields: Any) -> Any:
    return harness.channels.handle(
        event(
            harness.tenant_id,
            user,
            event_id,
            kind=ChannelEventKind.DECISION,
            reference=reference,
            action=fields.pop("action", ApprovalAction.APPROVE),
            **fields,
        )
    )


@pytest.fixture
def delivered(harness: Any) -> tuple[UUID, str]:
    """An enrolled manager phone that has received a review message; returns (enquiry, ref)."""
    enroll(harness, MANAGER, "phone-manager")
    enquiry = harness.enquiry(("IND-FAN-450", "2", "each"))
    harness.run_jobs()
    (recipient, message, reference) = harness.notifier.sent[0]
    assert recipient == "phone-manager" and message.metadata["version"] == "1"
    return enquiry, reference


def test_enrollment_needs_the_approver_role_and_a_valid_code(harness: Any) -> None:
    with pytest.raises(PermissionDenied):
        harness.channels.start_enrollment(harness.context("sales@industrial.example"), "simulated")
    binding = enroll(harness, MANAGER, "phone-1")
    assert (binding.subject, binding.external_user_id) == (MANAGER, "phone-1")
    with pytest.raises(ChannelActionRejected):
        harness.channels.handle(
            event(harness.tenant_id, "phone-2", "e2", kind=ChannelEventKind.ENROLL, code="WRONG")
        )


def test_enrollment_code_is_single_use_but_replay_safe(harness: Any) -> None:
    code = harness.channels.start_enrollment(harness.context(MANAGER), "simulated").code
    first = event(harness.tenant_id, "phone-1", "e1", kind=ChannelEventKind.ENROLL, code=code)
    binding = harness.channels.handle(first)
    assert harness.channels.handle(first) == binding  # provider redelivered the same event
    with pytest.raises(ChannelActionRejected):  # someone else using the same code
        harness.channels.handle(
            event(harness.tenant_id, "phone-x", "e9", kind=ChannelEventKind.ENROLL, code=code)
        )


def test_expired_enrollment_code_is_refused(harness: Any) -> None:
    code = harness.channels.start_enrollment(harness.context(MANAGER), "simulated").code
    harness.channels._clock = lambda: harness.clock() + timedelta(minutes=11)
    with pytest.raises(ChannelActionRejected):
        harness.channels.handle(
            event(harness.tenant_id, "phone-1", "e1", kind=ChannelEventKind.ENROLL, code=code)
        )


def test_manager_approves_from_the_channel_with_the_same_service(
    harness: Any, delivered: tuple[UUID, str]
) -> None:
    enquiry, reference = delivered
    result = decide(harness, "phone-manager", "msg-1", reference)
    assert isinstance(result, DecisionResult) and not result.replayed
    assert result.decision.transport == "simulated" and result.decision.actor_subject == MANAGER
    assert harness.quotation(enquiry).status is QuotationStatus.APPROVED
    assert decide(harness, "phone-manager", "msg-1", reference).replayed  # redelivery
    with pytest.raises(DecisionConflict):  # a second, different tap after the decision
        decide(harness, "phone-manager", "msg-2", reference, action=ApprovalAction.REJECT,
               reason="no")  # fmt: skip


def test_forwarded_message_cannot_be_used_by_another_account(
    harness: Any, delivered: tuple[UUID, str]
) -> None:
    _, reference = delivered
    # Another approver with their own enrolled phone still cannot use the manager's reference.
    harness.db.add_member(harness.tenant_id, "deputy@industrial.example", Role.APPROVER)
    enroll(harness, "deputy@industrial.example", "phone-deputy")
    for user, connection in (("phone-deputy", CONNECTION), ("phone-manager", "other-connection")):
        with pytest.raises(ChannelActionRejected):
            decide(harness, user, f"fwd-{user}", reference, connection_id=connection)


def test_revoked_binding_and_expired_reference_are_refused(
    harness: Any, delivered: tuple[UUID, str]
) -> None:
    _, reference = delivered
    with pytest.raises(ChannelActionRejected):
        decide(harness, "phone-manager", "m1", "made-up-reference")
    harness.channels._clock = lambda: harness.clock() + REFERENCE_LIFETIME
    with pytest.raises(ChannelActionRejected):
        decide(harness, "phone-manager", "m2", reference)
    harness.channels._clock = harness.clock
    binding = harness.channels.bindings(harness.context(MANAGER))[0]
    assert harness.channels.revoke(harness.context(MANAGER), binding.binding_id)
    with pytest.raises(ChannelActionRejected):
        decide(harness, "phone-manager", "m3", reference)


def test_manager_who_lost_the_role_cannot_decide_via_channel(
    harness: Any, delivered: tuple[UUID, str]
) -> None:
    _, reference = delivered
    harness.db.roles[(harness.tenant_id, MANAGER)] = frozenset({Role.VIEWER})
    with pytest.raises(PermissionDenied):
        decide(harness, "phone-manager", "m1", reference)


def test_review_goes_only_to_current_approvers_and_is_not_resent(harness: Any) -> None:
    enroll(harness, MANAGER, "phone-manager")
    former = "former@industrial.example"
    harness.db.add_member(harness.tenant_id, former, Role.APPROVER)
    enroll(harness, former, "phone-former")
    harness.db.roles[(harness.tenant_id, former)] = frozenset({Role.VIEWER})  # role removed
    harness.enquiry(("IND-FAN-450", "2", "each"))
    harness.run_jobs()
    assert [recipient for recipient, _, _ in harness.notifier.sent] == ["phone-manager"]
    task_id = UUID(harness.notifier.sent[0][1].metadata["task_id"])
    harness.notifications()(_notify_job(harness, task_id))  # duplicate job
    harness.run_jobs()
    assert len(harness.notifier.sent) == 1  # same idempotency key: nothing new queued


def test_failed_delivery_is_retried_with_a_fresh_reference(harness: Any) -> None:
    enroll(harness, MANAGER, "phone-manager")
    harness.notifier.fail = True
    harness.enquiry(("IND-FAN-450", "2", "each"))
    with pytest.raises(OSError):
        harness.run_jobs()
    unsent = next(iter(harness.db.references.values()))
    assert unsent.sent_at is None
    old_hash = unsent.token_sha256
    [message_id] = harness.outbox.messages  # back to pending; the job queue retries it
    harness.send(message_id, attempt=2)
    assert unsent.sent_at is not None and unsent.token_sha256 != old_hash
    # Only the delivered token works.
    assert decide(harness, "phone-manager", "m1", harness.notifier.sent[0][2]).decision


def _notify_job(harness: Any, task_id: UUID) -> ClaimedJob:
    return ClaimedJob(uuid4(), harness.tenant_id, JobKind.NOTIFY_APPROVERS,
                      {"task_id": str(task_id)}, 1, 5, uuid4(), uuid4())  # fmt: skip
