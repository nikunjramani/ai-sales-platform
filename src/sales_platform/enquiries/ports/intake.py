"""Contracts for durable enquiry receipt, status reads and worker-side preparation."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.enquiries.domain.attachments import AttachmentStatus
from sales_platform.enquiries.domain.follow_ups import FollowUp, OpenEnquiry
from sales_platform.enquiries.domain.intake import EnquiryStatus, InboundEnvelope


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different payload."""


@dataclass(frozen=True)
class Receipt:
    enquiry_id: UUID
    status: EnquiryStatus
    payload_sha256: str
    correlation_id: UUID
    received_at: datetime
    continued: bool = False  # the message joined an existing enquiry (phase 4)


# Decides, inside the conversation's locked transaction, which open enquiry (if any) a message
# continues. None: it starts a new enquiry. See application/conversations.py.
type EnquiryRouter = Callable[[Sequence[OpenEnquiry], datetime], FollowUp | None]


@dataclass(frozen=True)
class AttachmentView:
    attachment_id: UUID
    filename: str
    declared_content_type: str
    size_bytes: int
    status: AttachmentStatus
    rejection_reason: str | None


@dataclass(frozen=True)
class ExtractedLineView:
    sku: str
    quantity: str
    unit: str


@dataclass(frozen=True)
class ExtractionView:
    """Latest workflow result: requested lines ready for pricing, or what is still needed."""

    outcome: str
    lines: tuple[ExtractedLineView, ...]
    questions: tuple[str, ...]
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class EnquiryView:
    enquiry_id: UUID
    channel: str  # provider key, e.g. "api"
    status: EnquiryStatus
    customer_reference: str | None
    received_at: datetime
    correlation_id: UUID
    attachments: tuple[AttachmentView, ...]
    extraction: ExtractionView | None = None
    connection_ref: UUID | None = None  # the connection it arrived through


class IntakeStore(Protocol):
    def find_receipt(
        self, tenant_id: UUID, connection_ref: UUID, provider_event_id: str
    ) -> Receipt | None: ...

    def record(
        self,
        envelope: InboundEnvelope,
        payload_sha256: str,
        enquiry_id: UUID,
        router: EnquiryRouter | None = None,
        hold_for_review: bool = False,
    ) -> tuple[Receipt, bool]:
        """Atomically store the inbox event, the conversation message and its attachments, and
        either continue an open enquiry or create one (enquiry_id) with its preparation job.

        A sender (envelope.sender_external_id) gets an identity, contact and conversation on
        first contact; the conversation row is locked while router decides, so one customer's
        messages are routed one at a time. Without a sender, or without a router, the message
        always starts a new enquiry (Q5).

        hold_for_review (a sender over its message limits, phase 5): the enquiry goes to
        needs_review after preparation instead of being read, and no acknowledgement is sent.

        Returns (receipt, created). If another request already recorded the same provider event,
        nothing is written and the existing receipt is returned with created=False.
        """
        ...

    def get_enquiry(self, tenant_id: UUID, enquiry_id: UUID) -> EnquiryView | None: ...


@dataclass(frozen=True)
class StoredAttachment:
    attachment_id: UUID
    declared_content_type: str
    status: AttachmentStatus


class PreparationStore(Protocol):
    """Worker-side access. Every call is scoped to the job's tenant."""

    def attachments_for(self, tenant_id: UUID, enquiry_id: UUID) -> list[StoredAttachment] | None:
        """None if the enquiry does not exist for this tenant."""
        ...

    def mark_attachment(
        self,
        tenant_id: UUID,
        attachment_id: UUID,
        status: AttachmentStatus,
        detected_content_type: str | None,
        reason: str | None,
        storage_key: str,
    ) -> None: ...

    def finish_preparation(
        self, tenant_id: UUID, enquiry_id: UUID, status: EnquiryStatus, correlation_id: UUID
    ) -> None:
        """Set the enquiry status; if READY_FOR_EXTRACTION, queue the extraction job in the
        same transaction so the next step can never be lost."""
        ...


@dataclass(frozen=True)
class EnquirySummary:
    enquiry_id: UUID
    channel: str  # provider key, e.g. "api"
    status: EnquiryStatus
    customer_reference: str | None
    received_at: datetime
    preview: str  # the start of the customer's text, for lists


class EnquiryListReader(Protocol):
    def list_enquiries(
        self, tenant_id: UUID, statuses: frozenset[EnquiryStatus] | None, limit: int
    ) -> list[EnquirySummary]: ...
