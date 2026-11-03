"""Intake use case: durability order, replay, conflicts and cleanup of quarantined files."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from sales_platform.application.intake import EnquiryIntakeService
from sales_platform.domain.intake import AttachmentUpload, EnquiryStatus, EnquirySubmission
from sales_platform.domain.tenancy import PermissionDenied, Role, TenantContext
from sales_platform.ports.intake import IdempotencyConflict, Receipt

type ContextFactory = Callable[[UUID, Role], TenantContext]


@pytest.fixture
def service(
    intake_store: Any,
    memory_storage: Any,
    connection_store: Any,
    provider_registry: Any,
    now: datetime,
) -> EnquiryIntakeService:
    return EnquiryIntakeService(
        intake_store, memory_storage, connection_store, provider_registry, clock=lambda: now
    )


@pytest.fixture
def intake_context(make_context: ContextFactory, industrial_tenant: UUID) -> TenantContext:
    return make_context(industrial_tenant, Role.INTAKE_SERVICE)


def submission(text: str = "Quote 50 fans") -> EnquirySubmission:
    return EnquirySubmission(text, "CUST-1", (AttachmentUpload("a.txt", "text/plain", b"spec"),))


def test_new_enquiry_is_recorded_with_attachment_in_quarantine(
    service: EnquiryIntakeService,
    intake_context: TenantContext,
    memory_storage: Any,
    intake_store: Any,
    now: datetime,
) -> None:
    result = service.submit(intake_context, submission(), "request-0001", "api")
    assert not result.duplicate
    assert result.receipt.status is EnquiryStatus.RECEIVED
    envelope = intake_store.envelopes[result.receipt.enquiry_id]
    assert envelope.received_at == now
    assert envelope.connection_id == intake_context.subject
    [key] = memory_storage.objects
    assert key == envelope.attachments[0].storage_key
    assert f"tenants/{intake_context.tenant_id}/quarantine/" in key


def test_replay_returns_original_and_writes_nothing(
    service: EnquiryIntakeService, intake_context: TenantContext, memory_storage: Any
) -> None:
    first = service.submit(intake_context, submission(), "request-0001", "api")
    again = service.submit(intake_context, submission(), "request-0001", "api")
    assert again.duplicate and again.receipt == first.receipt
    assert len(memory_storage.objects) == 1


def test_reused_key_with_different_payload_conflicts(
    service: EnquiryIntakeService, intake_context: TenantContext
) -> None:
    service.submit(intake_context, submission(), "request-0001", "api")
    with pytest.raises(IdempotencyConflict):
        service.submit(intake_context, submission("Quote 60 fans"), "request-0001", "api")


def test_failed_receipt_removes_quarantined_files(
    service: EnquiryIntakeService,
    intake_context: TenantContext,
    intake_store: Any,
    memory_storage: Any,
) -> None:
    intake_store.fail_record = RuntimeError("database down")
    with pytest.raises(RuntimeError):
        service.submit(intake_context, submission(), "request-0001", "api")
    assert memory_storage.objects == {}
    assert intake_store.receipts == {}


def test_storage_failure_stores_nothing(
    service: EnquiryIntakeService,
    intake_context: TenantContext,
    intake_store: Any,
    memory_storage: Any,
) -> None:
    memory_storage.fail_puts = True
    with pytest.raises(OSError):
        service.submit(intake_context, submission(), "request-0001", "api")
    assert intake_store.receipts == {}


def test_losing_a_concurrent_race_returns_the_winner(
    service: EnquiryIntakeService,
    intake_context: TenantContext,
    intake_store: Any,
    memory_storage: Any,
    now: datetime,
) -> None:
    winner = Receipt(
        UUID(int=7), EnquiryStatus.RECEIVED, submission().fingerprint(), UUID(int=8), now
    )
    intake_store.concurrent_winner = winner
    result = service.submit(intake_context, submission(), "request-0001", "api")
    assert result.duplicate and result.receipt == winner
    assert memory_storage.objects == {}  # our copy of the files was discarded


@pytest.mark.parametrize("role", [Role.VIEWER, Role.APPROVER])
def test_roles_without_submit_permission_are_refused(
    service: EnquiryIntakeService,
    make_context: ContextFactory,
    industrial_tenant: UUID,
    role: Role,
) -> None:
    with pytest.raises(PermissionDenied):
        service.submit(make_context(industrial_tenant, role), submission(), "request-0001", "api")


def test_status_is_tenant_scoped(
    service: EnquiryIntakeService,
    intake_context: TenantContext,
    make_context: ContextFactory,
    office_tenant: UUID,
) -> None:
    enquiry_id = service.submit(
        intake_context, submission(), "request-0001", "api"
    ).receipt.enquiry_id
    view = service.status(intake_context, enquiry_id, "api")
    assert view is not None and view.attachments[0].filename == "a.txt"
    assert service.status(make_context(office_tenant, Role.VIEWER), enquiry_id, "api") is None


def test_invalid_idempotency_key_is_refused(
    service: EnquiryIntakeService, intake_context: TenantContext
) -> None:
    with pytest.raises(ValueError, match="Idempotency-Key"):
        service.submit(intake_context, submission(), "short", "api")


def test_a_disabled_connection_cannot_submit(
    service: EnquiryIntakeService, intake_context: TenantContext, connection_store: Any
) -> None:
    connection_store.disabled.add(intake_context.subject)
    with pytest.raises(PermissionDenied):
        service.submit(intake_context, submission(), "request-0001", "api")
    with pytest.raises(PermissionDenied):  # a provider that is not installed
        service.submit(intake_context, submission(), "request-0002", "telegram")


def test_the_reply_address_is_fixed_at_intake(
    service: EnquiryIntakeService, intake_context: TenantContext, intake_store: Any
) -> None:
    result = service.submit(intake_context, submission(), "request-0001", "api")
    envelope = intake_store.envelopes[result.receipt.enquiry_id]
    # A pull provider: the quotation goes back to the submitting key, not the reference label.
    assert envelope.reply_to == intake_context.subject and envelope.channel == "api"


# --- Phase 4: follow-ups continue the customer's open enquiry ------------------------------


def test_a_message_while_the_first_is_still_read_continues_it(
    service: EnquiryIntakeService, intake_context: TenantContext
) -> None:
    first = service.submit(intake_context, submission(), "request-0001", "api")
    second = service.submit(intake_context, submission("10 meter"), "request-0002", "api")
    assert second.receipt.enquiry_id == first.receipt.enquiry_id
    assert second.receipt.continued and not first.receipt.continued
    replay = service.submit(intake_context, submission("10 meter"), "request-0002", "api")
    assert replay.duplicate and replay.receipt.continued


def test_an_answer_reopens_a_waiting_enquiry(
    service: EnquiryIntakeService, intake_context: TenantContext, intake_store: Any, now: datetime
) -> None:
    first = service.submit(intake_context, submission(), "request-0001", "api")
    intake_store.statuses[first.receipt.enquiry_id] = EnquiryStatus.NEEDS_CLARIFICATION
    intake_store.asked[first.receipt.enquiry_id] = now
    answer = service.submit(intake_context, submission("10 meter"), "request-0002", "api")
    assert answer.receipt.continued and answer.receipt.status is EnquiryStatus.RECEIVED


def test_a_question_seven_days_old_starts_a_new_enquiry(
    service: EnquiryIntakeService, intake_context: TenantContext, intake_store: Any, now: datetime
) -> None:
    first = service.submit(intake_context, submission(), "request-0001", "api")
    intake_store.statuses[first.receipt.enquiry_id] = EnquiryStatus.NEEDS_CLARIFICATION
    intake_store.asked[first.receipt.enquiry_id] = now - timedelta(days=7)
    later = service.submit(intake_context, submission("10 meter"), "request-0002", "api")
    assert not later.receipt.continued
    assert later.receipt.enquiry_id != first.receipt.enquiry_id


def test_without_a_customer_reference_messages_are_never_continued(
    service: EnquiryIntakeService, intake_context: TenantContext
) -> None:
    def anonymous(text: str) -> EnquirySubmission:
        return EnquirySubmission(text, None)

    first = service.submit(intake_context, anonymous("Quote 5 fans"), "request-0001", "api")
    second = service.submit(intake_context, anonymous("10 meter"), "request-0002", "api")
    assert second.receipt.enquiry_id != first.receipt.enquiry_id
    assert not second.receipt.continued
