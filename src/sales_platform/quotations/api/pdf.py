"""PDF download responses shared by the portal and customer APIs.

Files are always downloads (never shown inline under our origin), never cached by browsers or
proxies, and the browser must not guess another content type.
"""

from fastapi import HTTPException, Response, status

from sales_platform.quotations.application.documents import PdfFile

INTEGRITY_FAILURE = "The stored document failed its integrity check; it was not served"


def pdf_response(file: PdfFile) -> Response:
    return Response(
        content=file.data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{file.filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def integrity_failure() -> HTTPException:
    """A stored file is missing or changed: an operator must investigate (logged by caller)."""
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, INTEGRITY_FAILURE)
