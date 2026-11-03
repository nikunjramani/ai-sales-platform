"""Generic provider callbacks (ADR 0018; platform routes and handshakes in phase 5).

POST /api/v1/hooks/{connection_id}/{webhook_token}
  The URL names the connection and carries its secret token; the provider's own signature is
  checked on the raw body before parsing. Events are stored once and routed by a job, so the
  answer is quick:
  200 {"received": n, "duplicates": d}   stored (or already stored)
  404 "Not found"        unknown connection, wrong token, not active or no webhook support
  413 "Event too large"  401 "Invalid signature"  400 "Invalid event"
  503 "Try again later"  the database is unavailable; providers retry and dedupe makes it safe
POST /api/v1/hooks/p/{provider_key}
  One URL for the whole platform (one bot, one app): the provider checks the request with our
  company-level secrets, then the request's own routing keys name the business. A genuine
  request is always answered 200, also when no business claims it or its content is refused,
  so the tool neither retries it nor switches the webhook off. 401 only for a bad signature.
GET on either URL
  A verification handshake (e.g. echo a challenge), answered by the provider; 404 otherwise.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from sales_platform.application.inbound import EventTooLarge, HookNotFound, InboundService
from sales_platform.ports.providers import (
    HandshakeResponse,
    InboundRejected,
    InboundRequest,
    InvalidInbound,
)

# Hard ceiling before reading more; each provider's own limit is checked after that.
MAX_HOOK_BYTES = 1024 * 1024


class HookReceiptOut(BaseModel):
    received: int
    duplicates: int


async def _inbound(request: Request) -> InboundRequest:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_HOOK_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Event too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_HOOK_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Event too large")
    return InboundRequest(
        headers={name.lower(): value for name, value in request.headers.items()},
        body=bytes(body),
        query=dict(request.query_params),
        received_at=datetime.now(UTC),
        method=request.method,
        url=str(request.url),
    )


def _answer(handshake: HandshakeResponse) -> Response:
    return Response(
        handshake.body,
        status_code=handshake.status,
        media_type=handshake.content_type,
        headers={"Cache-Control": "no-store"},
    )


def build_hook_router(service: InboundService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/hooks", tags=["provider webhooks"])

    async def guarded[T](call: Callable[[], T]) -> T:
        try:
            # Database and secret-store calls block; keep them off the event loop.
            return await run_in_threadpool(call)
        except HookNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
        except EventTooLarge:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Event too large") from None
        except InboundRejected:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature") from None
        except InvalidInbound:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid event") from None
        except SQLAlchemyError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Try again later") from None

    # The platform routes come first: "p" is never a connection ID.
    @router.post(
        "/p/{provider_key}",
        response_model=HookReceiptOut,
        responses={401: {}, 404: {}, 413: {}, 503: {}},
    )
    async def receive_platform(provider_key: str, request: Request) -> HookReceiptOut:
        inbound = await _inbound(request)
        receipt = await guarded(lambda: service.receive_platform(provider_key, inbound))
        return HookReceiptOut(received=receipt.received, duplicates=receipt.duplicates)

    @router.get("/p/{provider_key}", responses={404: {}})
    async def platform_handshake(provider_key: str, request: Request) -> Response:
        inbound = await _inbound(request)
        answer = await guarded(lambda: service.platform_handshake(provider_key, inbound))
        return _answer(answer)

    @router.post(
        "/{connection_id}/{webhook_token}",
        response_model=HookReceiptOut,
        responses={400: {}, 401: {}, 404: {}, 413: {}, 503: {}},
    )
    async def receive(connection_id: UUID, webhook_token: str, request: Request) -> HookReceiptOut:
        inbound = await _inbound(request)
        receipt = await guarded(lambda: service.receive(connection_id, webhook_token, inbound))
        return HookReceiptOut(received=receipt.received, duplicates=receipt.duplicates)

    @router.get("/{connection_id}/{webhook_token}", responses={404: {}})
    async def handshake(connection_id: UUID, webhook_token: str, request: Request) -> Response:
        inbound = await _inbound(request)
        answer = await guarded(lambda: service.handshake(connection_id, webhook_token, inbound))
        return _answer(answer)

    return router
