"""The signed approval link page's API (phase 5, P5-14). Signed-in portal session + CSRF.

GET  /api/v1/approval-links/{token}
  200 the request: business, quotation number, version, state (open, decided, changed,
      expired) and, while open, the exact lines and totals with the content hash to decide on.
  401 not signed in (the page sends the person to sign in and back), 403 "This link is not
      for you" (another manager's link), 404 unknown link.
POST /api/v1/approval-links/{token}/decision {command_id, action, content_sha256, reason?}
  The same answers as a portal decision; 409 already decided or a newer version.
"""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.api.auth import PortalAuth, SignedIn
from sales_platform.application.approval_links import (
    ApprovalLinkService,
    LinkNotForYou,
    LinkNotFound,
    LinkState,
)
from sales_platform.domain.quotations import (
    ApprovalAction,
    DecisionConflict,
    SelfApprovalNotAllowed,
    StaleVersion,
)
from sales_platform.domain.tenancy import AuthorizationError


class ApprovalLinkOut(BaseModel):
    tenant_id: UUID
    business_name: str
    quotation_id: UUID
    number: str
    version: int
    state: LinkState
    decided: ApprovalAction | None
    expires_at: str
    content_sha256: str | None  # while open: decide on exactly this version
    customer_reference: str | None
    content: dict[str, object] | None  # lines, subtotal, tax, total, currency


class LinkDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID = Field(description="New random UUID per click; a retry reuses it")
    action: ApprovalAction
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=1000)


class LinkDecisionOut(BaseModel):
    action: ApprovalAction
    replayed: bool


def build_approval_link_router(auth: PortalAuth, links: ApprovalLinkService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/approval-links", tags=["portal approval links"])
    Person = Annotated[SignedIn, Depends(auth.signed_in())]

    def guarded[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except LinkNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
        except LinkNotForYou as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from None
        except (DecisionConflict, StaleVersion):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Already decided, or a newer version exists"
            ) from None
        except SelfApprovalNotAllowed as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from None
        except AuthorizationError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted") from None
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except SQLAlchemyError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Try again later") from None

    @router.get("/{token}", response_model=ApprovalLinkOut, responses={403: {}, 404: {}})
    def view(token: str, person: Person) -> ApprovalLinkOut:
        found = guarded(lambda: links.view(person.record.subject, token))
        request, packet = found.request, found.packet
        return ApprovalLinkOut(
            tenant_id=request.binding.tenant_id,
            business_name=request.business_name,
            quotation_id=request.quotation_id,
            number=request.number,
            version=request.version,
            state=found.state,
            decided=request.decided,
            expires_at=request.expires_at.isoformat(),
            content_sha256=None if packet is None else packet.content_sha256,
            customer_reference=None if packet is None else packet.customer_reference,
            content=None if packet is None else dict(packet.content),
        )

    @router.post(
        "/{token}/decision",
        response_model=LinkDecisionOut,
        responses={403: {}, 404: {}, 409: {}},
    )
    def decide(token: str, body: LinkDecisionIn, person: Person) -> LinkDecisionOut:
        result = guarded(
            lambda: links.decide(
                person.record.subject,
                token,
                body.command_id,
                body.action,
                body.content_sha256,
                body.reason,
            )
        )
        return LinkDecisionOut(action=result.decision.action, replayed=result.replayed)

    return router
