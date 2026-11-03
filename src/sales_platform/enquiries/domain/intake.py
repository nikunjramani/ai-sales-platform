"""Enquiry intake: what a provider submits, its limits, and the canonical envelope we store.

Every provider (the API now; email and chat tools later) is translated into the same envelope,
so the rest of the system never depends on a particular provider's message format.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sales_platform.platform.domain.validation import require_aware, require_text

MAX_TEXT_CHARACTERS = 20_000
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 15 * 1024 * 1024
# 2 (phase 1): the connection row (connection_ref) and the reply address (reply_to).
ENVELOPE_SCHEMA_VERSION = 2


class EnquiryStatus(StrEnum):
    RECEIVED = "received"
    READY_FOR_EXTRACTION = "ready_for_extraction"
    NEEDS_REVIEW = "needs_review"
    # Set by the extraction workflow (M3):
    READY_FOR_QUOTATION = "ready_for_quotation"
    NEEDS_CLARIFICATION = "needs_clarification"


@dataclass(frozen=True)
class AttachmentUpload:
    """A file as submitted. The filename is for display only and never used as a path."""

    filename: str
    declared_content_type: str
    data: bytes

    def __post_init__(self) -> None:
        require_text(self.filename, "filename", 255)
        require_text(self.declared_content_type, "content type", 100)
        if not 0 < len(self.data) <= MAX_ATTACHMENT_BYTES:
            raise ValueError(f"Each attachment must be 1 byte to {MAX_ATTACHMENT_BYTES} bytes")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class EnquirySubmission:
    """Untrusted customer content. It is stored and later read as data, never as instructions."""

    text: str
    customer_reference: str | None
    attachments: tuple[AttachmentUpload, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Enquiry text is required")
        if len(self.text) > MAX_TEXT_CHARACTERS:
            raise ValueError(f"Enquiry text is limited to {MAX_TEXT_CHARACTERS} characters")
        if self.customer_reference is not None:
            require_text(self.customer_reference, "customer_reference")
        if len(self.attachments) > MAX_ATTACHMENTS:
            raise ValueError(f"At most {MAX_ATTACHMENTS} attachments are allowed")
        if sum(len(item.data) for item in self.attachments) > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError(f"Attachments are limited to {MAX_TOTAL_ATTACHMENT_BYTES} bytes")

    def fingerprint(self) -> str:
        """Stable hash of the content. A replay must match it; a different payload conflicts."""
        canonical = {
            "text": self.text,
            "customer_reference": self.customer_reference,
            "attachments": [
                [item.filename, item.declared_content_type, item.sha256]
                for item in self.attachments
            ],
        }
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def validate_idempotency_key(key: str) -> str:
    """The caller's unique ID for one submission; retries must reuse it."""
    if not isinstance(key, str) or not 8 <= len(key) <= 200 or not key.isprintable():
        raise ValueError("Idempotency-Key must be 8-200 printable characters")
    return key


@dataclass(frozen=True)
class AttachmentRecord:
    attachment_id: UUID
    filename: str
    declared_content_type: str
    size_bytes: int
    sha256: str
    storage_key: str


@dataclass(frozen=True)
class InboundEnvelope:
    """Canonical event stored with each receipt (see architecture/integrations.md)."""

    event_id: UUID
    tenant_id: UUID
    channel: str  # the provider key, e.g. "api"
    connection_id: str  # the connection's legacy key (kept for old readers)
    connection_ref: UUID  # the connection row the enquiry arrived on
    reply_to: str  # where the quotation goes back, fixed at intake (delivery_recipient)
    provider_event_id: str
    sender_external_id: str | None
    received_at: datetime
    text: str
    attachments: tuple[AttachmentRecord, ...]
    correlation_id: UUID

    def __post_init__(self) -> None:
        require_aware(self.received_at, "received_at")
        require_text(self.connection_id, "connection_id", 100)
        require_text(self.reply_to, "reply_to", 200)
        validate_idempotency_key(self.provider_event_id)

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "event_id": str(self.event_id),
            "tenant_id": str(self.tenant_id),
            "channel": self.channel,
            "connection_id": self.connection_id,
            "connection_ref": str(self.connection_ref),
            "reply_to": self.reply_to,
            "provider_event_id": self.provider_event_id,
            "sender_external_id": self.sender_external_id,
            "received_at": self.received_at.isoformat(),
            "text": self.text,
            "attachments": [
                {
                    "attachment_id": str(item.attachment_id),
                    "filename": item.filename,
                    "declared_content_type": item.declared_content_type,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in self.attachments
            ],
            "correlation_id": str(self.correlation_id),
        }
