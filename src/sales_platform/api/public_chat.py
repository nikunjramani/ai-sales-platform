"""Public routes of hosted chat (website chat, phase 4). No cookies; JSON only.

/api/v1/public/chat/{handle}/...
  GET    config                  chat page settings (no sign-in)
  POST   sessions                start: name + email or phone, honeypot "website", form_ms
  POST   messages                Bearer token + Idempotency-Key; {text, attachments?}
  GET    messages?after=<seq>    the visitor's thread (polled)
  POST   actions/{ref}           {action: accept|decline, reason?}
  GET    documents/{message}.pdf the quotation PDF sent in this chat
  POST   session/end             sign out (the token stops working)
/q/{token} (GET page) and POST /api/v1/public/actions/{token}: the secure answer link (Q1).

Rules: the business always comes from the handle or the session, never from the body. Unknown,
disabled, suspended and other businesses' handles all give the same 404. A browser on a
website not in the chat's allowed list gets 404 too. CORS headers echo only allowed origins,
never with credentials. Tokens are never logged.
"""

import base64
import binascii
import html
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.application.chat import (
    ChatNotFound,
    ChatRateLimited,
    ChatRefused,
    HostedChatService,
    SessionRejected,
)
from sales_platform.application.customer_actions import (
    CustomerActionRejected,
    CustomerActionService,
)
from sales_platform.domain.acceptance import (
    NotCurrentOffer,
    OfferNotOpen,
    PricesChanged,
    ResponseAction,
)
from sales_platform.domain.chat import origin_allowed
from sales_platform.domain.contacts import InvalidContact
from sales_platform.domain.documents import DocumentIntegrityError
from sales_platform.domain.intake import MAX_ATTACHMENT_BYTES, AttachmentUpload
from sales_platform.domain.quotations import quotation_number
from sales_platform.ports.chat import ChatEndpoint, ChatSession, ThreadMessage
from sales_platform.ports.erp import ErpError
from sales_platform.ports.intake import IdempotencyConflict

ALLOWED_HEADERS = "Authorization, Content-Type, Idempotency-Key"
POLL_MS = 3000
_KINDS = {
    "clarification_questions": "questions",
    "quotation_ready": "quotation",
    "answer_accepted": "confirmation",
    "answer_declined": "confirmation",
    "prices_changed": "confirmation",
    "quotation_withdrawn": "notice",
    "quotation_expired": "notice",
    "opted_out": "notice",
    "opted_in": "notice",
    "enquiry_received": "notice",
}


class ChatConfigOut(BaseModel):
    business_name: str
    greeting: str
    require: Literal["email_or_phone"] = "email_or_phone"
    max_message_chars: int
    files_per_message: int
    max_file_bytes: int


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    website: str = Field(default="", max_length=200)  # honeypot: people leave it empty
    form_ms: int = Field(ge=0, le=86_400_000)


class StartOut(BaseModel):
    session_token: str
    expires_at: datetime
    after: int = 0


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    content_base64: str = Field(min_length=1, max_length=MAX_ATTACHMENT_BYTES * 4 // 3 + 4)


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    attachments: list[FileIn] = Field(default_factory=list, max_length=5)


class MessageAccepted(BaseModel):
    duplicate: bool
    received_at: datetime


class ActionOut(BaseModel):
    action: str
    label: str
    ref: UUID


class QuotationCard(BaseModel):
    number: str
    version: str
    document_path: str | None
    actions: list[ActionOut]


class ThreadMessageOut(BaseModel):
    seq: int
    direction: str
    kind: str  # text | questions | quotation | confirmation | notice
    text: str
    at: datetime
    quotation: QuotationCard | None = None


class ThreadOut(BaseModel):
    messages: list[ThreadMessageOut]
    after: int
    poll_ms: int = POLL_MS


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ResponseAction
    reason: str | None = Field(default=None, max_length=1000)


class LinkActionIn(ActionIn):
    confirm: Literal[True]


class ActionResult(BaseModel):
    outcome: str
    quotation_number: str
    version: int


def _thread_item(handle: str, item: ThreadMessage) -> ThreadMessageOut:
    kind = "text" if item.direction == "inbound" else _KINDS.get(item.template_key or "", "notice")
    card = None
    if item.template_key == "quotation_ready":
        actions = (
            [
                ActionOut(action="accept", label="Accept", ref=item.action_ref),
                ActionOut(action="decline", label="Decline", ref=item.action_ref),
            ]
            if item.action_ref is not None and item.action_ref_open
            else []
        )
        card = QuotationCard(
            number=item.metadata.get("number", ""),
            version=item.metadata.get("version", ""),
            document_path=(
                f"/api/v1/public/chat/{handle}/documents/{item.outbound_message_id}.pdf"
                if item.document_id is not None
                else None
            ),
            actions=actions,
        )
    return ThreadMessageOut(
        seq=item.seq, direction=item.direction, kind=kind, text=item.text, at=item.at,
        quotation=card,
    )  # fmt: skip


def _uploads(files: list[FileIn]) -> tuple[AttachmentUpload, ...]:
    uploads = []
    for item in files:
        try:
            data = base64.b64decode(item.content_base64, validate=True)
            uploads.append(AttachmentUpload(item.filename, item.content_type, data))
        except (binascii.Error, ValueError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "A file could not be read"
            ) from None
    return tuple(uploads)


def build_public_chat_router(
    chats: HostedChatService,
    actions: CustomerActionService,
    own_origin: str | None,
    widget_file: Path | None = None,
) -> APIRouter:
    router = APIRouter(tags=["public chat"])

    # --- the widget file and our hosted chat page -----------------------------------------
    @router.get("/chat/widget.js", include_in_schema=False)
    def widget() -> Response:
        if widget_file is None or not widget_file.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        return Response(
            widget_file.read_bytes(),
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @router.get("/chat/{handle}", response_class=HTMLResponse, include_in_schema=False)
    def chat_page(handle: str, request: Request) -> HTMLResponse:
        found = guarded(lambda: chats.endpoint(handle, None))
        # Only the business's own websites (and our own pages) may frame this page.
        ancestors = " ".join(["'self'", *chats.allowed_origins(found)])
        safe = html.escape(handle, quote=True)
        name = html.escape(found.business_name)
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Chat with {name}</title><style>body{{margin:0}}</style></head><body>"
            f"<script src='/chat/widget.js' data-chat='{safe}' data-mode='page'></script>"
            "</body></html>"
        )
        return HTMLResponse(
            body,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; "
                f"frame-ancestors {ancestors}",
            },
        )

    def guarded[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except ChatNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
        except SessionRejected:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in") from None
        except (ChatRefused, InvalidContact) as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except ChatRateLimited as error:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                str(error),
                headers={"Retry-After": str(int(error.retry_after.total_seconds()) + 1)},
            ) from None
        except IdempotencyConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except CustomerActionRejected as error:
            raise HTTPException(status.HTTP_410_GONE, str(error)) from None
        except PricesChanged:
            raise HTTPException(status.HTTP_409_CONFLICT, "prices_changed") from None
        except (OfferNotOpen, NotCurrentOffer) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except DocumentIntegrityError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Try again later") from None
        except (ErpError, SQLAlchemyError):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Try again later") from None

    def foreign_origin(request: Request) -> str | None:
        """The Origin header, unless it is this API's own address (our hosted chat page)."""
        origin = request.headers.get("origin")
        own = f"{request.url.scheme}://{request.url.netloc}"
        return None if origin is None or origin.lower() == own.lower() else origin

    def endpoint(handle: str, request: Request) -> ChatEndpoint:
        return guarded(lambda: chats.endpoint(handle, foreign_origin(request)))

    def signed_in(handle: str, request: Request) -> tuple[ChatEndpoint, ChatSession]:
        found = endpoint(handle, request)
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        return found, guarded(lambda: chats.authenticate(found, token))

    def cors(response: Response, request: Request, found: ChatEndpoint) -> None:
        origin = request.headers.get("origin")
        if origin is not None and origin_allowed(origin, chats.allowed_origins(found), own_origin):
            response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Cache-Control"] = "no-store"

    prefix = "/api/v1/public/chat/{handle}"

    @router.options(prefix + "/{path:path}", include_in_schema=False)
    def preflight(handle: str, path: str, request: Request) -> Response:
        found = endpoint(handle, request)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        cors(response, request, found)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
        response.headers["Access-Control-Max-Age"] = "600"
        return response

    @router.get(prefix + "/config", response_model=ChatConfigOut, responses={404: {}})
    def config(handle: str, request: Request, response: Response) -> ChatConfigOut:
        found = endpoint(handle, request)
        cors(response, request, found)
        settings = chats.config(found)
        return ChatConfigOut(
            business_name=settings.business_name,
            greeting=settings.greeting,
            max_message_chars=settings.max_message_chars,
            files_per_message=settings.files_per_message,
            max_file_bytes=settings.max_file_bytes,
        )

    @router.post(
        prefix + "/sessions",
        response_model=StartOut,
        status_code=status.HTTP_201_CREATED,
        responses={404: {}, 422: {}, 429: {}},
    )
    def start(handle: str, body: StartIn, request: Request, response: Response) -> StartOut:
        found = endpoint(handle, request)
        cors(response, request, found)
        client = request.client.host if request.client is not None else "unknown"
        new = guarded(
            lambda: chats.start(
                found, body.name, body.email, body.phone, body.website, body.form_ms,
                request.headers.get("origin"), client,
            )
        )  # fmt: skip
        return StartOut(session_token=new.token, expires_at=new.expires_at)

    @router.post(
        prefix + "/messages",
        response_model=MessageAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        responses={401: {}, 404: {}, 409: {}, 422: {}, 429: {}},
    )
    def send(
        handle: str,
        body: MessageIn,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> MessageAccepted:
        found, session = signed_in(handle, request)
        cors(response, request, found)
        uploads = _uploads(body.attachments)
        receipt = guarded(lambda: chats.send(found, session, body.text, uploads, idempotency_key))
        if receipt.duplicate:
            response.status_code = status.HTTP_200_OK
        return MessageAccepted(duplicate=receipt.duplicate, received_at=receipt.received_at)

    @router.get(prefix + "/messages", response_model=ThreadOut, responses={401: {}, 404: {}})
    def thread(
        handle: str,
        request: Request,
        response: Response,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> ThreadOut:
        found, session = signed_in(handle, request)
        cors(response, request, found)
        items = guarded(lambda: chats.thread(session, after))
        return ThreadOut(
            messages=[_thread_item(handle, item) for item in items],
            after=items[-1].seq if items else after,
        )

    @router.post(
        prefix + "/actions/{ref_id}",
        response_model=ActionResult,
        responses={401: {}, 404: {}, 409: {}, 410: {}, 503: {}},
    )
    def act(
        handle: str, ref_id: UUID, body: ActionIn, request: Request, response: Response
    ) -> ActionResult:
        found, session = signed_in(handle, request)
        cors(response, request, found)
        recorded, _ = guarded(lambda: chats.act(session, ref_id, body.action, body.reason))
        return ActionResult(
            outcome=recorded.outcome.value,
            quotation_number=quotation_number(recorded.quotation_id),
            version=recorded.version,
        )

    @router.get(
        prefix + "/documents/{message_id}.pdf",
        responses={200: {"content": {"application/pdf": {}}}, 401: {}, 404: {}},
    )
    def document(handle: str, message_id: UUID, request: Request) -> Response:
        found, session = signed_in(handle, request)
        name, data = guarded(lambda: chats.document(session, message_id))
        response = Response(
            data,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
        cors(response, request, found)
        return response

    @router.post(
        prefix + "/session/end",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={401: {}, 404: {}},
    )
    def end(handle: str, request: Request) -> Response:
        found, session = signed_in(handle, request)
        guarded(lambda: chats.end(session))
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        cors(response, request, found)
        return response

    # --- secure answer link (Q1): opening it never changes anything --------------------
    @router.get("/q/{token}", response_class=HTMLResponse, include_in_schema=False)
    def link_page(token: str) -> HTMLResponse:
        try:
            ref = actions.by_link(token)
        except CustomerActionRejected:
            return _page("This link is no longer valid.", None, status.HTTP_410_GONE)
        return _page(
            f"Quotation {quotation_number(ref.quotation_id)} (version {ref.version})",
            token,
            status.HTTP_200_OK,
        )

    @router.post(
        "/api/v1/public/actions/{token}",
        response_model=ActionResult,
        responses={409: {}, 410: {}, 503: {}},
    )
    def link_action(token: str, body: LinkActionIn) -> ActionResult:
        ref = guarded(lambda: actions.by_link(token))
        recorded, _ = guarded(lambda: actions.act(ref, body.action, body.reason))
        return ActionResult(
            outcome=recorded.outcome.value,
            quotation_number=quotation_number(recorded.quotation_id),
            version=recorded.version,
        )

    return router


def _page(title: str, token: str | None, code: int) -> HTMLResponse:
    """A minimal page: GET shows the choice; the buttons POST with confirm=true."""
    buttons = ""
    if token is not None:
        safe = html.escape(token, quote=True)
        buttons = (
            '<p><button data-a="accept">Accept</button> '
            '<button data-a="decline">Decline</button></p><p id="r"></p>'
            "<script>document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{"
            "if(!confirm(b.textContent+' this quotation?'))return;"
            f"const r=await fetch('/api/v1/public/actions/{safe}',{{method:'POST',"
            "headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({action:b.dataset.a,confirm:true})});"
            "document.getElementById('r').textContent=r.ok?'Thank you, your answer was "
            "recorded.':'This link is no longer valid or the quotation changed.';"
            "document.querySelectorAll('button').forEach(x=>x.disabled=true);});</script>"
        )
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'><title>Quotation</title></head>"
        f"<body><h1>{html.escape(title)}</h1>{buttons}</body></html>"
    )
    return HTMLResponse(
        body,
        status_code=code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'",
        },
    )
