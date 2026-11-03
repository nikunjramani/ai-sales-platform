from uuid import UUID

import pytest

from sales_platform.domain.attachments import (
    AttachmentStatus,
    StorageArea,
    attachment_key,
    inspect_attachment,
)

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


@pytest.mark.parametrize(
    ("data", "declared"),
    [
        (PDF, "application/pdf"),
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (b"Need 50 fans for Pune", "text/plain"),
    ],
)
def test_matching_supported_files_are_clean(data: bytes, declared: str) -> None:
    result = inspect_attachment(data, declared)
    assert result.status is AttachmentStatus.CLEAN
    assert result.detected_content_type == declared


@pytest.mark.parametrize(
    ("data", "declared", "reason"),
    [
        (b"", "text/plain", "empty_file"),
        (PDF, "application/zip", "unsupported_type"),
        (PNG, "application/pdf", "type_mismatch"),  # renamed image pretending to be a PDF
        (b"MZ\x90\x00\x03\x00", "text/plain", "type_mismatch"),  # binary, e.g. an executable
        (PDF + b"/Encrypt 5 0 R", "application/pdf", "encrypted_pdf"),
        (PDF + b"/OpenAction << /S /JavaScript >>", "application/pdf", "active_pdf_content"),
        (PDF + b"/EmbeddedFile", "application/pdf", "active_pdf_content"),
    ],
)
def test_unsafe_or_mismatched_files_are_rejected(data: bytes, declared: str, reason: str) -> None:
    result = inspect_attachment(data, declared)
    assert result.status is AttachmentStatus.REJECTED
    assert result.reason == reason


def test_storage_keys_are_server_generated_per_tenant() -> None:
    tenant = UUID("11111111-1111-4111-8111-111111111111")
    attachment = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert attachment_key(tenant, attachment, StorageArea.QUARANTINE) == (
        f"tenants/{tenant}/quarantine/{attachment}"
    )
