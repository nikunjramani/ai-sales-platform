"""Linking a manager's own phone or chat account to one of the business's tools.

POST /api/v1/tenants/{t}/connections/{connection_id}/enrollments   a one-time code (approvers)

Moved from the connections router (ADR 0026): linking accounts for approvals is quotations'
rule; the URL is unchanged.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from sales_platform.integrations.ports.connections import ConnectionNotFound
from sales_platform.platform.api.auth import PortalAuth
from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.quotations.application.channel_actions import ChannelActionService


class EnrollmentHintOut(BaseModel):
    text: str
    url: str | None


class ConnectionEnrollmentOut(BaseModel):
    connection_id: UUID
    channel: str
    code: str
    expires_at: datetime
    hint: EnrollmentHintOut | None = None


def build_enrollment_router(auth: PortalAuth, channels: ChannelActionService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["portal connections"])
    Context = Annotated[TenantContext, Depends(auth.tenant_context())]

    def guarded[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except ConnectionNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None

    @router.post(
        "/connections/{connection_id}/enrollments",
        response_model=ConnectionEnrollmentOut,
        status_code=status.HTTP_201_CREATED,
        responses={403: {}, 404: {}, 422: {}},
    )
    def enroll(connection_id: UUID, context: Context) -> ConnectionEnrollmentOut:
        new = guarded(lambda: channels.start_enrollment(context, "", connection_id))
        return ConnectionEnrollmentOut(
            connection_id=connection_id,
            channel=new.provider_key,
            code=new.code,
            expires_at=new.expires_at,
            hint=None
            if new.hint is None
            else EnrollmentHintOut(text=new.hint.text, url=new.hint.url),
        )

    return router
