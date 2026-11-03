"""Worker step after intake: inspect quarantined attachments and mark the enquiry ready.

Idempotent by design, because a job can run again after a crash: already-decided attachments
are skipped, and a file already moved to the clean area is recognised.
"""

from typing import Protocol
from uuid import UUID

from sales_platform.enquiries.domain.attachments import (
    AttachmentStatus,
    StorageArea,
    attachment_key,
    inspect_attachment,
)
from sales_platform.enquiries.domain.intake import EnquiryStatus
from sales_platform.enquiries.ports.intake import PreparationStore
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.platform.ports.storage import ObjectStorage


class Acknowledger(Protocol):
    def acknowledge(self, tenant_id: UUID, enquiry_id: UUID, correlation_id: UUID) -> None: ...


class EnquiryPreparationHandler:
    def __init__(
        self,
        store: PreparationStore,
        storage: ObjectStorage,
        acknowledger: Acknowledger | None = None,
    ) -> None:
        self._store = store
        self._storage = storage
        self._acknowledger = acknowledger

    def __call__(self, job: ClaimedJob) -> None:
        try:
            enquiry_id = UUID(job.payload["enquiry_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid enquiry_id") from None
        attachments = self._store.attachments_for(job.require_tenant(), enquiry_id)
        if attachments is None:
            raise PermanentJobError("Enquiry not found for this tenant")
        if job.payload.get("acknowledge") == "true" and self._acknowledger is not None:
            # A new enquiry from a conversation (phase 4); the outbox key makes it once only.
            self._acknowledger.acknowledge(job.require_tenant(), enquiry_id, job.correlation_id)

        all_clean = True
        for attachment in attachments:
            status = attachment.status
            if status is AttachmentStatus.QUARANTINED:
                status = self._inspect(
                    job.require_tenant(), attachment.attachment_id, attachment.declared_content_type
                )
            all_clean = all_clean and status is AttachmentStatus.CLEAN
        # "hold": the sender was over its message limits (phase 5); staff read it instead.
        held = job.payload.get("hold") == "true"
        ready = all_clean and not held
        final = EnquiryStatus.READY_FOR_EXTRACTION if ready else EnquiryStatus.NEEDS_REVIEW
        self._store.finish_preparation(job.require_tenant(), enquiry_id, final, job.correlation_id)

    def _inspect(
        self, tenant_id: UUID, attachment_id: UUID, declared_content_type: str
    ) -> AttachmentStatus:
        quarantine = attachment_key(tenant_id, attachment_id, StorageArea.QUARANTINE)
        clean = attachment_key(tenant_id, attachment_id, StorageArea.CLEAN)
        if not self._storage.exists(quarantine) and self._storage.exists(clean):
            # A previous attempt moved the file but crashed before recording it.
            self._store.mark_attachment(
                tenant_id, attachment_id, AttachmentStatus.CLEAN, declared_content_type, None, clean
            )
            return AttachmentStatus.CLEAN
        result = inspect_attachment(self._storage.get(quarantine), declared_content_type)
        if result.status is AttachmentStatus.CLEAN:
            self._storage.move(quarantine, clean)
            key = clean
        else:
            key = quarantine  # rejected files stay isolated in quarantine for review
        self._store.mark_attachment(
            tenant_id,
            attachment_id,
            result.status,
            result.detected_content_type,
            result.reason,
            key,
        )
        return result.status
