"""Attachment preparation: inspection, quarantine release, crash recovery and idempotency."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.enquiries.application.preparation import EnquiryPreparationHandler
from sales_platform.enquiries.domain.attachments import (
    AttachmentStatus,
    StorageArea,
    attachment_key,
)
from sales_platform.enquiries.domain.intake import EnquiryStatus
from sales_platform.enquiries.ports.intake import StoredAttachment
from sales_platform.platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.platform.ports.jobs import ClaimedJob

PDF = b"%PDF-1.7\n%%EOF"


class FakePreparationStore:
    def __init__(self, tenant_id: UUID, enquiry_id: UUID) -> None:
        self.tenant_id, self.enquiry_id = tenant_id, enquiry_id
        self.attachments: dict[UUID, StoredAttachment] = {}
        self.reasons: dict[UUID, str | None] = {}
        self.enquiry_status: EnquiryStatus | None = None

    def attachments_for(self, tenant_id: UUID, enquiry_id: UUID) -> list[StoredAttachment] | None:
        if (tenant_id, enquiry_id) != (self.tenant_id, self.enquiry_id):
            return None
        return list(self.attachments.values())

    def mark_attachment(
        self,
        tenant_id: UUID,
        attachment_id: UUID,
        status: AttachmentStatus,
        detected_content_type: str | None,
        reason: str | None,
        storage_key: str,
    ) -> None:
        current = self.attachments[attachment_id]
        self.attachments[attachment_id] = StoredAttachment(
            attachment_id, current.declared_content_type, status
        )
        self.reasons[attachment_id] = reason

    def finish_preparation(
        self, tenant_id: UUID, enquiry_id: UUID, status: EnquiryStatus, correlation_id: UUID
    ) -> None:
        self.enquiry_status = status


@pytest.fixture
def store(industrial_tenant: UUID) -> FakePreparationStore:
    return FakePreparationStore(industrial_tenant, uuid4())


def add(store: FakePreparationStore, storage: Any, data: bytes, declared: str) -> UUID:
    attachment_id = uuid4()
    store.attachments[attachment_id] = StoredAttachment(
        attachment_id, declared, AttachmentStatus.QUARANTINED
    )
    storage.put(attachment_key(store.tenant_id, attachment_id, StorageArea.QUARANTINE), data)
    return attachment_id


def job_for(store: FakePreparationStore, enquiry_id: UUID | None = None) -> ClaimedJob:
    return ClaimedJob(
        uuid4(),
        store.tenant_id,
        JobKind.PREPARE_ENQUIRY,
        {"enquiry_id": str(enquiry_id or store.enquiry_id)},
        1,
        5,
        uuid4(),
        uuid4(),
    )


def test_clean_files_leave_quarantine_and_enquiry_is_ready(
    store: FakePreparationStore, memory_storage: Any
) -> None:
    attachment = add(store, memory_storage, PDF, "application/pdf")
    EnquiryPreparationHandler(store, memory_storage)(job_for(store))
    assert store.attachments[attachment].status is AttachmentStatus.CLEAN
    assert list(memory_storage.objects) == [
        attachment_key(store.tenant_id, attachment, StorageArea.CLEAN)
    ]
    assert store.enquiry_status is EnquiryStatus.READY_FOR_EXTRACTION


def test_rejected_file_stays_quarantined_and_enquiry_needs_review(
    store: FakePreparationStore, memory_storage: Any
) -> None:
    good = add(store, memory_storage, b"plain text", "text/plain")
    bad = add(store, memory_storage, b"\x89PNG\r\n\x1a\n...", "application/pdf")
    EnquiryPreparationHandler(store, memory_storage)(job_for(store))
    assert store.attachments[good].status is AttachmentStatus.CLEAN
    assert store.attachments[bad].status is AttachmentStatus.REJECTED
    assert store.reasons[bad] == "type_mismatch"
    assert memory_storage.exists(attachment_key(store.tenant_id, bad, StorageArea.QUARANTINE))
    assert store.enquiry_status is EnquiryStatus.NEEDS_REVIEW


def test_enquiry_without_attachments_is_ready(
    store: FakePreparationStore, memory_storage: Any
) -> None:
    EnquiryPreparationHandler(store, memory_storage)(job_for(store))
    assert store.enquiry_status is EnquiryStatus.READY_FOR_EXTRACTION


def test_rerun_after_crash_between_move_and_record(
    store: FakePreparationStore, memory_storage: Any
) -> None:
    attachment = add(store, memory_storage, PDF, "application/pdf")
    # Simulate: previous attempt moved the file, then crashed before updating the database.
    memory_storage.move(
        attachment_key(store.tenant_id, attachment, StorageArea.QUARANTINE),
        attachment_key(store.tenant_id, attachment, StorageArea.CLEAN),
    )
    handler = EnquiryPreparationHandler(store, memory_storage)
    handler(job_for(store))
    handler(job_for(store))  # and running again changes nothing
    assert store.attachments[attachment].status is AttachmentStatus.CLEAN
    assert store.enquiry_status is EnquiryStatus.READY_FOR_EXTRACTION


def test_missing_enquiry_or_bad_payload_is_permanent(
    store: FakePreparationStore, memory_storage: Any
) -> None:
    handler = EnquiryPreparationHandler(store, memory_storage)
    with pytest.raises(PermanentJobError):
        handler(job_for(store, enquiry_id=uuid4()))
    bad = ClaimedJob(uuid4(), store.tenant_id, JobKind.PREPARE_ENQUIRY, {}, 1, 5, uuid4(), uuid4())
    with pytest.raises(PermanentJobError):
        handler(bad)
