"""The outbox's send_message job: honest outcomes, send hooks, and never storing tokens."""

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from sales_platform.integrations.domain.connections import ConnectionStatus
from sales_platform.integrations.domain.messages import MessageStatus, TemplateKey
from sales_platform.integrations.ports.providers import SendRejected, SendUncertain
from sales_platform.quotations.application.channel_actions import ChannelEvent, ChannelEventKind

MANAGER = "manager@industrial.example"


def link_phone(harness: Any) -> None:
    code = harness.channels.start_enrollment(harness.context(MANAGER), "simulated").code
    harness.channels.handle(
        ChannelEvent(
            "simulated",
            "simulated-local",
            harness.tenant_id,
            "enroll-1",
            "phone",
            ChannelEventKind.ENROLL,
            code=code,
        )  # fmt: skip
    )


def queued_request(harness: Any) -> UUID:
    """An approval request queued in the outbox, its send job not yet run."""
    link_phone(harness)
    harness.enquiry(("IND-FAN-450", "2", "each"))
    while not harness.outbox.messages:
        harness.run_one()
    [message_id] = harness.outbox.messages
    return UUID(str(message_id))


def status(harness: Any, message_id: UUID) -> MessageStatus:
    found: MessageStatus = harness.outbox.messages[message_id].status
    return found


def test_an_approval_request_is_stored_without_its_token_and_sent_with_one(harness: Any) -> None:
    message_id = queued_request(harness)
    stored = harness.outbox.messages[message_id]
    assert stored.template_key is TemplateKey.APPROVAL_REQUEST
    assert (
        stored.idempotency_key == f"approval-request:{stored.approval_task_id}:{stored.binding_id}"
    )
    assert set(stored.params) == {"task_id", "binding_id"}
    assert "approval needed for quotation" in stored.body_text
    harness.send(message_id)
    [(recipient, sent, token)] = harness.notifier.sent
    assert recipient == "phone" and token and sent.actions == (
        "approve", "reject", "request_revision",
    )  # fmt: skip
    stored = harness.outbox.messages[message_id]
    assert token not in repr(stored)  # the single-use token exists only in the sent message
    assert stored.status is MessageStatus.SENT and stored.provider_message_id == "msg-1"
    [reference] = harness.db.references.values()
    assert reference.sent_at is not None


def test_rejected_uncertain_and_interrupted_sends_are_never_resent(harness: Any) -> None:
    message_id = queued_request(harness)
    harness.notifier.outcome = SendUncertain("timeout after sending")
    harness.send(message_id)
    assert status(harness, message_id) is MessageStatus.UNKNOWN
    harness.send(message_id, attempt=2)
    assert harness.notifier.messages == []

    harness.outbox.messages[message_id] = replace(
        harness.outbox.messages[message_id], status=MessageStatus.PENDING
    )
    harness.notifier.outcome = SendRejected("blocked")
    harness.send(message_id)
    assert status(harness, message_id) is MessageStatus.FAILED
    assert harness.outbox.messages[message_id].last_error == "Rejected by the provider: blocked"

    harness.outbox.messages[message_id] = replace(
        harness.outbox.messages[message_id], status=MessageStatus.SENDING
    )  # a worker stopped while handing it over
    harness.send(message_id, attempt=2)
    assert status(harness, message_id) is MessageStatus.UNKNOWN
    assert harness.notifier.messages == []


def test_transient_errors_retry_and_the_last_attempt_fails(harness: Any) -> None:
    message_id = queued_request(harness)
    harness.notifier.fail = True
    with pytest.raises(OSError):
        harness.send(message_id)
    assert status(harness, message_id) is MessageStatus.PENDING
    harness.notifier.fail = True
    with pytest.raises(OSError):
        harness.send(message_id, attempt=5)
    assert status(harness, message_id) is MessageStatus.FAILED


def test_a_decided_task_cancels_its_request(harness: Any) -> None:
    message_id = queued_request(harness)
    task = harness.db.tasks[harness.outbox.messages[message_id].approval_task_id]
    harness.approvals.decide(MANAGER, harness.command(task))
    harness.send(message_id)
    assert status(harness, message_id) is MessageStatus.FAILED
    assert harness.outbox.messages[message_id].last_error == "The approval task was already decided"
    assert harness.notifier.messages == []


def test_an_inactive_connection_fails_the_message(harness: Any) -> None:
    message_id = queued_request(harness)
    connection_id = harness.outbox.messages[message_id].connection_id
    harness.connections.add(
        replace(harness.connections.rows[connection_id], status=ConnectionStatus.DISABLED)
    )
    harness.send(message_id)
    assert status(harness, message_id) is MessageStatus.FAILED
    assert "not active" in (harness.outbox.messages[message_id].last_error or "")


def test_a_duplicate_job_queues_nothing_new(harness: Any) -> None:
    message_id = queued_request(harness)
    harness.send(message_id)
    harness.send(message_id)  # already sent: a repeat does nothing
    assert len(harness.notifier.messages) == 1
