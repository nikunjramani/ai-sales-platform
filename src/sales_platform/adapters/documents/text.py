"""Bounded text extraction from plain text and PDF attachments (pypdf, no OCR).

Images and PDFs without a text layer (e.g. scans) are reported as unsupported instead of being
treated as empty, so they go to a person rather than silently disappearing.
"""

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from sales_platform.ports.interpretation import DocumentText

MAX_PDF_PAGES = 20
MAX_DOCUMENT_CHARACTERS = 50_000
MIN_TEXT_CHARACTERS = 20  # fewer readable characters than this suggests a scanned page


class PdfAndTextReader:
    def read(self, name: str, content_type: str, data: bytes) -> DocumentText:
        if content_type == "text/plain":
            return self._bounded(name, data.decode("utf-8", errors="replace"))
        if content_type == "application/pdf":
            return self._pdf(name, data)
        if content_type.startswith("image/"):
            return DocumentText(name, None, "image_requires_ocr")
        return DocumentText(name, None, "unsupported_type")

    def _pdf(self, name: str, data: bytes) -> DocumentText:
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                return DocumentText(name, None, "encrypted_pdf")
            if len(reader.pages) > MAX_PDF_PAGES:
                return DocumentText(name, None, "too_many_pages")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except (PyPdfError, ValueError, KeyError, TypeError):
            return DocumentText(name, None, "unreadable_pdf")
        if len(text.strip()) < MIN_TEXT_CHARACTERS:
            return DocumentText(name, None, "no_text_layer")
        return self._bounded(name, text)

    @staticmethod
    def _bounded(name: str, text: str) -> DocumentText:
        if len(text) > MAX_DOCUMENT_CHARACTERS:
            return DocumentText(name, None, "too_much_text")
        return DocumentText(name, text, None)
