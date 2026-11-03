"""Public quotation PDF links for tools that download attachments by URL (phase 5, C6).

GET /api/v1/media/{token}
  200 the PDF (private, never cached), or 404 "Not found" for an unknown, expired or changed
  document. No sign-in: the random, short-lived token is the permission, and it names one PDF.
"""

from fastapi import APIRouter, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from sales_platform.application.media import MediaNotFound, MediaService


def build_media_router(service: MediaService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/media", tags=["public media"])

    @router.get("/{token}", responses={404: {}})
    async def download(token: str) -> Response:
        try:
            filename, data = await run_in_threadpool(service.open, token)
        except MediaNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
        return Response(
            data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    return router
