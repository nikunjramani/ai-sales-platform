"""Quotation document identity, storage keys and delivery error text."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from sales_platform.adapters.storage.local import LocalObjectStorage
from sales_platform.domain.connections import DeliveryMode
from sales_platform.domain.documents import (
    MAX_ERROR_CHARACTERS,
    DocumentKind,
    DocumentSource,
    NoDeliveryAddress,
    UnrenderableContent,
    clean_error,
    delivery_recipient,
    document_filename,
    document_id,
    document_key,
    printable_text,
)


def test_document_id_is_stable_for_the_same_bytes_and_differs_for_new_bytes() -> None:
    tenant, quotation = uuid4(), uuid4()
    first = document_id(tenant, quotation, 1, DocumentKind.APPROVED, "a" * 64)
    assert first == document_id(tenant, quotation, 1, DocumentKind.APPROVED, "a" * 64)
    # A different render (e.g. a renderer upgrade) never reuses, so never overwrites, the key.
    assert first != document_id(tenant, quotation, 1, DocumentKind.APPROVED, "b" * 64)
    assert first != document_id(tenant, quotation, 2, DocumentKind.APPROVED, "a" * 64)
    assert first != document_id(uuid4(), quotation, 1, DocumentKind.APPROVED, "a" * 64)


def test_document_keys_stay_inside_the_tenant_folder(tmp_path: Path) -> None:
    tenant, document = uuid4(), uuid4()
    key = document_key(tenant, document)
    assert key == f"tenants/{tenant}/documents/{document}"
    storage = LocalObjectStorage(tmp_path)
    storage.put(key, b"%PDF-1.4")
    assert storage.get(key) == b"%PDF-1.4"
    with pytest.raises(ValueError):
        storage.put(f"tenants/{tenant}/documents/../clean/{document}", b"x")


def test_filenames_use_the_quotation_number() -> None:
    quotation = uuid4()
    number = f"Q-{quotation.hex[:8].upper()}"
    assert document_filename(quotation, 2) == f"{number}-v2.pdf"
    assert document_filename(quotation, 2, preview=True) == f"{number}-v2-preview.pdf"


def test_error_text_is_one_short_line() -> None:
    assert clean_error("  timed out\n  after 30s ") == "timed out after 30s"
    assert clean_error("") == "Unknown error"
    assert len(clean_error("x" * 1000)) == MAX_ERROR_CHARACTERS


def test_approval_time_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError):
        DocumentSource("Demo", uuid4(), 1, {}, "a" * 64, None, datetime(2026, 10, 23))


def test_the_recipient_is_whoever_sent_the_enquiry() -> None:
    # API: the submitting key, never the customer's free-text reference label.
    assert delivery_recipient(DeliveryMode.PULL, "api-key:1", "CUST-42") == "api-key:1"
    assert delivery_recipient(DeliveryMode.PUSH, "mailbox-1", "buyer@customer.example") == (
        "buyer@customer.example"
    )
    with pytest.raises(NoDeliveryAddress):
        delivery_recipient(DeliveryMode.PUSH, "mailbox-1", None)


def test_printable_text_is_one_line_and_refuses_what_the_pdf_cannot_show() -> None:
    assert printable_text("  Acme \n Pvt  Ltd ", "legal name") == "Acme Pvt Ltd"
    assert printable_text("Müller & Söhne", "legal name") == "Müller & Söhne"
    with pytest.raises(UnrenderableContent, match="legal name"):
        printable_text("株式会社", "legal name")
