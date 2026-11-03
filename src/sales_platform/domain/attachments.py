"""Attachment storage keys and content inspection.

Every upload lands in quarantine first. Inspection checks size, allowed type, that the real
bytes match the declared type, and rejects PDFs we cannot safely process (encrypted or with
active content). This is NOT antivirus scanning; a malware scanner is a release gate (M8).
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sales_platform.domain.intake import MAX_ATTACHMENT_BYTES

ALLOWED_CONTENT_TYPES = frozenset({"application/pdf", "text/plain", "image/png", "image/jpeg"})

_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
# PDF features that can run code, open other files or hide content from inspection.
_PDF_ACTIVE_MARKERS = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/OpenAction")


class AttachmentStatus(StrEnum):
    QUARANTINED = "quarantined"
    CLEAN = "clean"
    REJECTED = "rejected"


class StorageArea(StrEnum):
    QUARANTINE = "quarantine"
    CLEAN = "clean"


def attachment_key(tenant_id: UUID, attachment_id: UUID, area: StorageArea) -> str:
    """Server-generated key; customer filenames never become paths."""
    return f"tenants/{tenant_id}/{area.value}/{attachment_id}"


@dataclass(frozen=True)
class Inspection:
    status: AttachmentStatus
    detected_content_type: str | None
    reason: str | None


def detect_content_type(data: bytes) -> str | None:
    for signature, content_type in _SIGNATURES:
        if data.startswith(signature):
            return content_type
    if b"\x00" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text/plain"


def inspect_attachment(data: bytes, declared_content_type: str) -> Inspection:
    def reject(reason: str, detected: str | None = None) -> Inspection:
        return Inspection(AttachmentStatus.REJECTED, detected, reason)

    if not data:
        return reject("empty_file")
    if len(data) > MAX_ATTACHMENT_BYTES:
        return reject("too_large")
    if declared_content_type not in ALLOWED_CONTENT_TYPES:
        return reject("unsupported_type")
    detected = detect_content_type(data)
    if detected != declared_content_type:
        return reject("type_mismatch", detected)
    if detected == "application/pdf":
        if b"/Encrypt" in data:
            return reject("encrypted_pdf", detected)
        if any(marker in data for marker in _PDF_ACTIVE_MARKERS):
            return reject("active_pdf_content", detected)
    return Inspection(AttachmentStatus.CLEAN, detected, None)
