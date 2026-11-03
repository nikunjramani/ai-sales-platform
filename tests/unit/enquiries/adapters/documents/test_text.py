from collections.abc import Callable
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from sales_platform.enquiries.adapters.documents.text import (
    MAX_DOCUMENT_CHARACTERS,
    PdfAndTextReader,
)

reader = PdfAndTextReader()


def test_text_and_pdf_with_text_layer_are_read(make_pdf: Callable[[list[str]], bytes]) -> None:
    assert reader.read("a.txt", "text/plain", b"50 fans").text == "50 fans"
    pdf = reader.read("po.pdf", "application/pdf", make_pdf(["Purchase order", "150 pcs M8 bolts"]))
    assert pdf.unsupported_reason is None and "150 pcs M8 bolts" in (pdf.text or "")


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)  # like a scan: no text layer
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def encrypted_pdf(make_pdf: Callable[[list[str]], bytes]) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(BytesIO(make_pdf(["secret order"]))))
    writer.encrypt("password")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.mark.parametrize(
    ("name", "content_type", "build", "reason"),
    [
        ("scan.pdf", "application/pdf", lambda make: blank_pdf(), "no_text_layer"),
        ("locked.pdf", "application/pdf", encrypted_pdf, "encrypted_pdf"),
        ("bad.pdf", "application/pdf", lambda make: b"%PDF-1.7 broken", "unreadable_pdf"),
        ("photo.jpg", "image/jpeg", lambda make: b"\xff\xd8\xff", "image_requires_ocr"),
        ("big.txt", "text/plain", lambda make: b"x" * (MAX_DOCUMENT_CHARACTERS + 1),
         "too_much_text"),
    ],
)  # fmt: skip
def test_unreadable_documents_are_reported_not_emptied(
    make_pdf: Callable[[list[str]], bytes],
    name: str,
    content_type: str,
    build: Callable[[Callable[[list[str]], bytes]], bytes],
    reason: str,
) -> None:
    result = reader.read(name, content_type, build(make_pdf))
    assert result.text is None and result.unsupported_reason == reason
