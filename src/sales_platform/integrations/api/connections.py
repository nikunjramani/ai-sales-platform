"""Portal routes for a business's connections (ADR 0018, phase 1).

GET  /api/v1/tenants/{t}/connections[?purpose=...]
  Administrators: every connection (with last_error). Approvers: the active manager-approval
  connections they can link (P1-Q8). Others: 403. Never config, secrets or token hashes.
POST /api/v1/tenants/{t}/connections/{connection_id}/enrollments
  An approver starts linking their own account on that connection: a one-time code that works
  only there. 404 unknown (or another business's); 422 not an active approval connection.
Hosted chat (website chat, phase 4), administrators only:
GET  /connections/available                       tools that can be enabled here
POST /connections/hosted-chat {provider_key}       enable with one click (or return the
                                                   active one); gives chat_link and snippet
PATCH /connections/{id}/chat {allowed_origins?, greeting?}
PUT  /connections/{id}/chat/address {handle}      change the address once (409 if taken)
PUT  /connections/{id}/chat/active {active}       switch the chat off or on
One-click tools (phase 5), administrators only:
POST /connections/enable {provider_key, accept_costs, purposes?}
     creates what the business needs; answers the connection and, if one is left, the step
     the business must do (one-time values such as a link code appear only here). 422 when a
     tool with costs is enabled without accept_costs. Enable again: a new step (a new code).
POST /connections/{id}/actions/{action_id} {submission}   the data a step returned
GET  /connections/{id}/actions/{action_id}/page            a provider-hosted setup page
POST /connections/{id}/test       -> {ok, detail}
POST /connections/{id}/disable    -> {steps}  what the business must still do in the tool
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from sales_platform.integrations.application.connections import (
    ConnectionService,
    ConnectionView,
    HandleTaken,
)
from sales_platform.integrations.application.lifecycle import (
    ActionNotFound,
    ConnectionLifecycle,
    EnableOutcome,
    pending_from_json,
)
from sales_platform.integrations.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.integrations.ports.connections import ConnectionNotFound, RouteTaken
from sales_platform.integrations.ports.providers import PendingAction, PendingActionKind
from sales_platform.platform.api.auth import PortalAuth
from sales_platform.platform.domain.tenancy import TenantContext


class ConnectionOut(BaseModel):
    connection_id: UUID
    provider_key: str
    provider_name: str
    display_name: str
    purposes: list[Purpose]
    status: ConnectionStatus
    last_error: str | None  # administrators only
    created_at: datetime
    # Hosted chat only (administrators): the public address and how to embed it.
    chat: "HostedChatOut | None" = None
    # Phase 5 (administrators): what the tool reported, and the step left for the business.
    detail: str | None = None
    pending: "PendingActionOut | None" = None


class PendingActionOut(BaseModel):
    action_id: str
    kind: PendingActionKind
    title: str
    steps: list[str]  # one-time values filled in only in the answer that created them
    url: str | None
    form_schema: dict[str, object] | None
    expires_at: datetime | None
    once: dict[str, str]  # e.g. {"code": "..."}: shown now, never again


class EnableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_key: str = Field(min_length=1, max_length=32)
    accept_costs: bool = False
    purposes: list[Purpose] | None = None


class EnableOut(BaseModel):
    connection: ConnectionOut
    pending: PendingActionOut | None
    detail: str | None


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission: dict[str, object] = Field(default_factory=dict)


class TestOut(BaseModel):
    ok: bool
    detail: str | None


class DisableOut(BaseModel):
    steps: list[str]


class HostedChatOut(BaseModel):
    handle: str
    chat_link: str
    snippet: str
    allowed_origins: list[str]
    greeting: str
    address_changed: bool  # the address can be changed once


class AvailableToolOut(BaseModel):
    provider_key: str
    provider_name: str
    kind: str  # "hosted_chat" | "one_click"
    purposes: list[Purpose]
    free: bool
    cost_note: str


class EnableChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_key: str = Field(min_length=1, max_length=32)


class ChatSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_origins: list[Annotated[str, Field(max_length=200)]] | None = Field(
        default=None, max_length=10
    )
    greeting: str | None = Field(default=None, max_length=300)


class ChatAddressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(min_length=3, max_length=64)


class ChatActiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


def pending_out(action: PendingAction) -> PendingActionOut:
    """Steps name one-time values as {name}; without them (a stored step) a placeholder
    tells the administrator to ask for a new one."""
    values = _Placeholder(action.once)
    return PendingActionOut(
        action_id=action.action_id,
        kind=action.kind,
        title=action.title,
        steps=[step.format_map(values) for step in action.steps],
        url=action.url,
        form_schema=None if action.form_schema is None else dict(action.form_schema),
        expires_at=action.expires_at,
        once=dict(action.once),
    )


class _Placeholder(dict[str, str]):
    def __missing__(self, name: str) -> str:
        return "(press Enable again for a new one)"


def hosted_chat(row: Connection, base_url: str) -> HostedChatOut | None:
    if row.public_handle is None:
        return None
    origins = row.config.get("allowed_origins", [])
    return HostedChatOut(
        handle=row.public_handle,
        chat_link=f"{base_url}/chat/{row.public_handle}",
        snippet=(
            f'<script src="{base_url}/chat/widget.js" data-chat="{row.public_handle}" '
            "async></script>"
        ),
        allowed_origins=[str(item) for item in origins] if isinstance(origins, list) else [],
        greeting=str(row.config.get("greeting") or ""),
        address_changed=row.public_handle_changed,
    )


def _view(item: ConnectionView, base_url: str) -> ConnectionOut:
    row = item.connection
    return ConnectionOut(
        connection_id=row.connection_id,
        provider_key=row.provider_key,
        provider_name=item.provider_name,
        display_name=row.display_name,
        purposes=sorted(row.purposes),
        status=row.status,
        last_error=row.last_error if item.show_errors else None,
        created_at=row.created_at,
        chat=hosted_chat(row, base_url) if item.show_errors else None,
        detail=_public_detail(row) if item.show_errors else None,
        pending=(
            pending_out(pending_from_json(row.pending_action))
            if item.show_errors and row.pending_action is not None
            else None
        ),
    )


def _public_detail(row: Connection) -> str | None:
    value = row.config.get("public_detail")
    return None if value is None else str(value)


def build_connection_router(
    auth: PortalAuth,
    connections: ConnectionService,
    base_url: str,
    lifecycle: ConnectionLifecycle | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["portal connections"])
    Context = Annotated[TenantContext, Depends(auth.tenant_context())]

    def guarded[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except ConnectionNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
        except (HandleTaken, RouteTaken) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except ActionNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None

    @router.get("/connections", response_model=list[ConnectionOut])
    def list_connections(
        context: Context, purpose: Annotated[Purpose | None, Query()] = None
    ) -> list[ConnectionOut]:
        return [
            _view(item, base_url) for item in guarded(lambda: connections.list(context, purpose))
        ]

    def admin_view(row: Connection) -> ConnectionOut:
        return _view(ConnectionView(row, connections.provider_name(row), True), base_url)

    @router.get("/connections/available", response_model=list[AvailableToolOut])
    def available(context: Context) -> list[AvailableToolOut]:
        tools = [
            AvailableToolOut(
                provider_key=key,
                provider_name=name,
                kind="hosted_chat",
                purposes=[Purpose.CUSTOMER_MESSAGES],
                free=True,
                cost_note="Free",
            )
            for key, name in guarded(lambda: connections.enablable(context))
        ]
        if lifecycle is not None:
            tools += [
                AvailableToolOut(
                    provider_key=provider.key,
                    provider_name=provider.display_name,
                    kind="one_click",
                    purposes=sorted(provider.purposes),
                    free=provider.cost.free,
                    cost_note=provider.cost.text,
                )
                for provider in guarded(lambda: lifecycle.available(context))
            ]
        return tools

    def outcome(result: EnableOutcome) -> EnableOut:
        return EnableOut(
            connection=admin_view(result.connection),
            pending=None if result.pending is None else pending_out(result.pending),
            detail=result.detail,
        )

    def require_lifecycle() -> ConnectionLifecycle:
        if lifecycle is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        return lifecycle

    @router.post(
        "/connections/enable", response_model=EnableOut, responses={403: {}, 409: {}, 422: {}}
    )
    def enable(body: EnableIn, context: Context) -> EnableOut:
        service = require_lifecycle()
        purposes = None if body.purposes is None else [item.value for item in body.purposes]
        return outcome(
            guarded(lambda: service.enable(context, body.provider_key, body.accept_costs, purposes))
        )

    @router.post(
        "/connections/{connection_id}/actions/{action_id}",
        response_model=EnableOut,
        responses={403: {}, 404: {}, 409: {}, 422: {}},
    )
    def resume(
        connection_id: UUID, action_id: str, body: SubmissionIn, context: Context
    ) -> EnableOut:
        service = require_lifecycle()
        return outcome(
            guarded(lambda: service.resume(context, connection_id, action_id, body.submission))
        )

    @router.get(
        "/connections/{connection_id}/actions/{action_id}/page",
        response_class=HTMLResponse,
        responses={403: {}, 404: {}},
    )
    def provider_page(connection_id: UUID, action_id: str, context: Context) -> HTMLResponse:
        service = require_lifecycle()
        html = guarded(lambda: service.pending_page(context, connection_id, action_id))
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )

    @router.post(
        "/connections/{connection_id}/test", response_model=TestOut, responses={403: {}, 404: {}}
    )
    def test_connection(connection_id: UUID, context: Context) -> TestOut:
        service = require_lifecycle()
        result = guarded(lambda: service.test(context, connection_id))
        return TestOut(ok=result.ok, detail=result.detail)

    @router.post(
        "/connections/{connection_id}/disable",
        response_model=DisableOut,
        responses={403: {}, 404: {}},
    )
    def disable(connection_id: UUID, context: Context) -> DisableOut:
        service = require_lifecycle()
        return DisableOut(steps=list(guarded(lambda: service.disable(context, connection_id))))

    @router.post(
        "/connections/hosted-chat", response_model=ConnectionOut, responses={403: {}, 422: {}}
    )
    def enable_chat(body: EnableChatIn, context: Context) -> ConnectionOut:
        return admin_view(
            guarded(lambda: connections.enable_hosted_chat(context, body.provider_key))
        )

    @router.patch(
        "/connections/{connection_id}/chat",
        response_model=ConnectionOut,
        responses={403: {}, 404: {}, 422: {}},
    )
    def configure_chat(
        connection_id: UUID, body: ChatSettingsIn, context: Context
    ) -> ConnectionOut:
        return admin_view(
            guarded(
                lambda: connections.configure_hosted_chat(
                    context, connection_id, body.allowed_origins, body.greeting
                )
            )
        )

    @router.put(
        "/connections/{connection_id}/chat/address",
        response_model=ConnectionOut,
        responses={403: {}, 404: {}, 409: {}, 422: {}},
    )
    def change_address(connection_id: UUID, body: ChatAddressIn, context: Context) -> ConnectionOut:
        return admin_view(
            guarded(lambda: connections.change_chat_address(context, connection_id, body.handle))
        )

    @router.put(
        "/connections/{connection_id}/chat/active",
        response_model=ConnectionOut,
        responses={403: {}, 404: {}},
    )
    def chat_active(connection_id: UUID, body: ChatActiveIn, context: Context) -> ConnectionOut:
        return admin_view(
            guarded(lambda: connections.set_hosted_chat_active(context, connection_id, body.active))
        )

    return router
