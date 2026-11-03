"""Delivery of approved quotations: only approved versions, honest outcomes, no duplicates.

Since phase 1 a delivery is carried out by one outbox message (deliver_quotation queues it,
send_message sends it); harness.deliver runs both, and the delivery mirrors the message.
"""

import hashlib
from datetime import timedelta
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader

from sales_platform.integrations.application.messaging import NOT_LIVE
from sales_platform.integrations.domain.messages import MessageStatus
from sales_platform.integrations.ports.providers import SendRejected, SendUncertain
from sales_platform.platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.platform.domain.tenancy import TenantStatus
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.quotations.domain.documents import DeliveryRecord, DeliveryStatus
from sales_platform.quotations.domain.quotations import ApprovalAction

FAN = ("IND-FAN-450", "2", "each")
MANAGER = "manager@industrial.example"
EMAIL = ("email", "buyer@customer.example")


def claimed(harness: Any, delivery_id: UUID, attempt: int = 1) -> ClaimedJob:
    return ClaimedJob(
        uuid4(),
        harness.tenant_id,
        JobKind.DELIVER_QUOTATION,
        {"delivery_id": str(delivery_id)},
        attempt,
        5,
        uuid4(),
        uuid4(),
    )


def approve(harness: Any, sender: tuple[str, str] | None = None) -> tuple[UUID, UUID]:
    """Draft and approve a quotation; return (enquiry, delivery) with the delivery job unrun."""
    enquiry = harness.enquiry(FAN)
    if sender is not None:
        harness.db.senders[enquiry] = sender
    harness.run_jobs()
    harness.approvals.decide(MANAGER, harness.command(harness.pending_task(enquiry)))
    handler = harness.handler()
    for job in harness.db.take_jobs(JobKind.RESUME_QUOTATION):
        handler(ClaimedJob(uuid4(), job.tenant_id, job.kind, job.payload, 1, 5, uuid4(), uuid4()))
    [job] = harness.db.take_jobs(JobKind.DELIVER_QUOTATION)
    return enquiry, UUID(job.payload["delivery_id"])


def record(harness: Any, delivery_id: UUID) -> DeliveryRecord:
    found: DeliveryRecord = harness.db.delivery_rows[delivery_id][1]
    return found


def test_api_enquiry_gets_the_approved_pdf_made_available(harness: Any) -> None:
    enquiry, delivery_id = approve(harness)
    pending = record(harness, delivery_id)
    assert pending.status is DeliveryStatus.PENDING
    assert (pending.channel, pending.recipient) == (
        "api",
        "api-key:00000000-0000-4000-8000-0000000000c1",
    )

    harness.deliver(delivery_id)

    done = record(harness, delivery_id)
    assert done.status is DeliveryStatus.AVAILABLE and done.delivered_at is not None
    [(_, document)] = harness.db.documents.values()
    assert done.document_id == document.document_id
    data = harness.storage.get(document.storage_key)
    assert hashlib.sha256(data).hexdigest() == document.sha256 and len(data) == document.size_bytes
    version = harness.db.versions[-1].view
    assert document.content_sha256 == version.content_sha256
    text = "".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)
    assert str(version.content["total"]) in text  # the PDF matches the approved total
    # Valid for the tenant's days (default 7) from delivery, printed on the PDF (M6).
    assert document.valid_until == harness.clock() + timedelta(days=7)
    assert "Valid until " + document.valid_until.strftime("%Y-%m-%d %H:%M") + " UTC" in text
    assert harness.db.run_status == {}  # the run now waits for the customer's answer (M6)
    [expiry] = harness.db.take_jobs(JobKind.EXPIRE_QUOTATION)
    assert expiry.run_at == document.valid_until
    assert enquiry


def test_a_push_channel_sends_once_with_its_idempotency_key(harness: Any) -> None:
    _, delivery_id = approve(harness, EMAIL)
    harness.deliver(delivery_id)
    harness.deliver(delivery_id)  # a duplicate job does nothing more
    [(message, data)] = harness.email.documents
    assert message.recipient == EMAIL[1]
    assert message.idempotency_key == f"quotation-delivery:{delivery_id}"
    assert message.attachment is not None
    assert hashlib.sha256(data).hexdigest() == message.attachment.sha256
    done = record(harness, delivery_id)
    assert done.status is DeliveryStatus.SENT and done.provider_reference == "msg-1"


def test_rejected_and_uncertain_outcomes_are_recorded_not_retried(harness: Any) -> None:
    _, rejected = approve(harness, EMAIL)
    harness.email.outcome = SendRejected("mailbox does not exist")
    harness.deliver(rejected)
    failed = record(harness, rejected)
    assert failed.status is DeliveryStatus.FAILED
    assert failed.last_error == "Rejected by the provider: mailbox does not exist"

    _, uncertain = approve(harness, ("email", "other@customer.example"))
    harness.email.outcome = SendUncertain("timed out after sending")
    harness.deliver(uncertain)
    unknown = record(harness, uncertain)
    assert unknown.status is DeliveryStatus.UNKNOWN
    harness.deliver(uncertain, attempt=2)  # never resent automatically
    assert harness.email.documents == []


def test_transient_errors_are_retried_and_the_last_attempt_fails(harness: Any) -> None:
    _, delivery_id = approve(harness, EMAIL)
    harness.email.outcome = ConnectionError("provider unavailable")
    with pytest.raises(ConnectionError):
        harness.deliver(delivery_id)
    retry = record(harness, delivery_id)
    assert retry.status is DeliveryStatus.PENDING
    assert retry.last_error == "ConnectionError: provider unavailable"

    harness.deliver(delivery_id, attempt=2)
    assert record(harness, delivery_id).status is DeliveryStatus.SENT
    assert record(harness, delivery_id).last_error is None

    _, last = approve(harness, ("email", "late@customer.example"))
    harness.email.outcome = ConnectionError("still down")
    with pytest.raises(ConnectionError):
        harness.deliver(last, attempt=5)
    assert record(harness, last).status is DeliveryStatus.FAILED


def test_a_crash_while_sending_becomes_unknown_instead_of_a_second_send(harness: Any) -> None:
    _, delivery_id = approve(harness, EMAIL)
    harness.deliveries()(claimed(harness, delivery_id))  # queues the message
    message_id = harness.db.delivery_messages[delivery_id]
    harness.outbox.set_status(
        harness.tenant_id,
        message_id,
        MessageStatus.SENDING,
        expected=frozenset({MessageStatus.PENDING}),
    )  # the previous worker stopped right after handing it over
    harness.send(message_id, attempt=2)
    assert record(harness, delivery_id).status is DeliveryStatus.UNKNOWN
    assert harness.email.documents == []


def test_a_channel_without_a_connection_fails_visibly(harness: Any) -> None:
    _, delivery_id = approve(harness, ("telegram", "chat-42"))
    harness.deliver(delivery_id)
    failed = record(harness, delivery_id)
    assert failed.status is DeliveryStatus.FAILED
    assert failed.last_error == "No active connection for 'telegram' to send the quotation"


def test_a_connection_whose_provider_is_not_installed_fails_visibly(harness: Any) -> None:
    _, delivery_id = approve(harness, EMAIL)
    harness.email.key = "email-uninstalled"  # the registry no longer has an "email" provider
    harness.deliver(delivery_id)
    failed = record(harness, delivery_id)
    assert failed.status is DeliveryStatus.FAILED
    assert failed.last_error == "No provider is installed for 'email'"


def test_the_document_is_rendered_once_and_reused(harness: Any) -> None:
    _, first = approve(harness, EMAIL)
    harness.email.outcome = ConnectionError("down")
    with pytest.raises(ConnectionError):
        harness.deliver(first)
    [(_, document)] = harness.db.documents.values()
    harness.renderer = None  # a retry must not need to render again
    harness.deliver(first, attempt=2)
    assert harness.email.documents[0][1] == harness.storage.get(document.storage_key)


def test_a_changed_stored_file_is_never_sent(harness: Any) -> None:
    _, delivery_id = approve(harness, EMAIL)
    harness.email.outcome = ConnectionError("down")
    with pytest.raises(ConnectionError):
        harness.deliver(delivery_id)
    [(_, document)] = harness.db.documents.values()
    harness.storage.put(document.storage_key, b"%PDF-1.4 tampered")
    harness.deliver(delivery_id, attempt=2)
    failed = record(harness, delivery_id)
    assert failed.status is DeliveryStatus.FAILED and "does not match" in (failed.last_error or "")
    assert harness.email.documents == []


def test_unrenderable_content_fails_the_delivery_with_the_reason(harness: Any) -> None:
    _, delivery_id = approve(harness)
    harness.db.tenant_names[harness.tenant_id] = "Demo 工業"
    harness.deliver(delivery_id)
    failed = record(harness, delivery_id)
    assert failed.status is DeliveryStatus.FAILED
    assert failed.last_error == "business name contains characters the PDF fonts cannot show"


def test_only_an_approved_current_version_is_delivered(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    task = harness.pending_task(enquiry)
    harness.approvals.decide(MANAGER, harness.command(task, ApprovalAction.REJECT, "too high"))
    harness.run_jobs()
    assert harness.db.delivery_rows == {}
    quotation = harness.quotation(enquiry)
    # Even if someone queued one, a version that is not approved cannot be rendered.
    assert harness.db.queue_delivery(harness.tenant_id, quotation.quotation_id, 1) is False
    assert harness.db.approved_source(harness.tenant_id, quotation.quotation_id, 1) is None


def test_approved_run_without_a_current_approved_version_stops(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    harness.approvals.decide(MANAGER, harness.command(harness.pending_task(enquiry)))
    harness.quotation(enquiry).current_version = 2  # simulates a corrupted/raced state
    [job] = harness.db.take_jobs(JobKind.RESUME_QUOTATION)
    with pytest.raises(PermanentJobError):
        harness.handler()(
            ClaimedJob(uuid4(), job.tenant_id, job.kind, job.payload, 1, 5, uuid4(), uuid4())
        )
    assert harness.db.delivery_rows == {}


def test_missing_or_foreign_deliveries_are_permanent_errors(harness: Any) -> None:
    with pytest.raises(PermanentJobError):
        harness.deliveries()(claimed(harness, uuid4()))
    bad = ClaimedJob(
        uuid4(), harness.tenant_id, JobKind.DELIVER_QUOTATION, {}, 1, 5, uuid4(), uuid4()
    )
    with pytest.raises(PermanentJobError):
        harness.deliveries()(bad)


def test_nothing_is_pushed_to_customers_before_go_live(harness: Any) -> None:
    """Q11/D3: while setting up, a push channel's quotation is recorded as not sent; the
    business's own API client (pull, Q27) still gets it."""
    harness.business_status[harness.tenant_id] = TenantStatus.SETTING_UP
    _, pushed = approve(harness, ("email", "early@customer.example"))
    harness.deliver(pushed)
    assert harness.email.documents == []
    [message] = [m for m in harness.outbox.messages.values() if m.recipient.startswith("early")]
    assert message.status is MessageStatus.FAILED and message.last_error == NOT_LIVE
    _, pulled = approve(harness)
    harness.deliver(pulled)
    assert record(harness, pulled).status is DeliveryStatus.AVAILABLE
