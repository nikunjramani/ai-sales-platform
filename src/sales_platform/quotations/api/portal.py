"""Portal API for signed-in people, scoped to one tenant in the URL.

GET  /api/v1/tenants/{tenant}/quotations[?status=...]             list
GET  /api/v1/tenants/{tenant}/quotations/{id}                      versions, tasks, decisions
POST /api/v1/tenants/{tenant}/approval-tasks/{task}/decision       approve/reject/request revision
POST /api/v1/tenants/{tenant}/quotations/{id}/revisions            new version (sales operator)
GET  /api/v1/tenants/{tenant}/quotations/{id}/versions/{v}/preview.pdf  draft preview (not an offer)
GET  /api/v1/tenants/{tenant}/quotations/{id}/document.pdf         approved document as delivered
GET  /api/v1/tenants/{tenant}/quotations/{id}/delivery             document and delivery status
GET  /api/v1/tenants/{tenant}/quotations/{id}/customer             offer status and answers (M6)
POST /api/v1/tenants/{tenant}/quotations/{id}/withdrawal           withdraw an issued quotation
GET  /api/v1/tenants/{tenant}/enquiries[?status=...]               enquiry list (exceptions)
POST /api/v1/tenants/{tenant}/channel-enrollments                  get a code to link an account
GET  /api/v1/tenants/{tenant}/channel-bindings                     linked accounts
DELETE /api/v1/tenants/{tenant}/channel-bindings/{id}              revoke a linked account

The tenant in the URL is only a request: stored membership decides access, and non-members get
404. Decisions use the same ApprovalService as every manager channel.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from sales_platform.enquiries.application.intake import EnquiryListService
from sales_platform.enquiries.domain.intake import EnquiryStatus
from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.integrations.ports.connections import ConnectionStore
from sales_platform.platform.api.auth import PortalAuth
from sales_platform.platform.domain.tenancy import Permission, PermissionDenied, TenantContext
from sales_platform.platform.domain.text import UnrenderableContent
from sales_platform.platform.ports.storage import ObjectNotFound
from sales_platform.quotations.api.pdf import integrity_failure, pdf_response
from sales_platform.quotations.application.acceptance import (
    WITHDRAWING_ROLES,
    NotIssued,
    QuotationResponseService,
)
from sales_platform.quotations.application.approvals import ApprovalService
from sales_platform.quotations.application.channel_actions import ChannelActionService
from sales_platform.quotations.application.documents import QuotationDocumentService
from sales_platform.quotations.application.quotations import QuotationService
from sales_platform.quotations.domain.acceptance import (
    CustomerStatus,
    OfferNotOpen,
    RecordedResponse,
    ResponseConflict,
    ResponseOutcome,
)
from sales_platform.quotations.domain.documents import (
    DeliveryRecord,
    DeliveryStatus,
    DocumentIntegrityError,
    DocumentNotAvailable,
    StoredDocument,
)
from sales_platform.quotations.domain.pricing import PricingRejected
from sales_platform.quotations.domain.quotations import (
    PORTAL_TRANSPORT,
    REVISABLE,
    ApprovalAction,
    DecisionCommand,
    DecisionConflict,
    QuotationNotFound,
    QuotationStatus,
    RecordedDecision,
    RevisionConflict,
    RevisionNotAllowed,
    StaleVersion,
    TaskNotFound,
    TaskStatus,
)
from sales_platform.quotations.ports.channels import ChannelBinding
from sales_platform.quotations.ports.quotations import QuotationDetail, QuotationSummary

_UNAVAILABLE = "Temporarily unavailable; try again"
logger = logging.getLogger(__name__)


class QuotationSummaryOut(BaseModel):
    quotation_id: UUID
    enquiry_id: UUID
    number: str
    status: QuotationStatus
    current_version: int | None
    customer_reference: str | None
    currency: str | None
    total: str | None
    updated_at: datetime


class VersionOut(BaseModel):
    version: int
    content: dict[str, object]
    content_sha256: str
    created_by: str
    reason: str | None
    created_at: datetime


class TaskOut(BaseModel):
    task_id: UUID
    version: int
    content_sha256: str
    status: TaskStatus
    created_at: datetime
    decided_at: datetime | None


class DecisionOut(BaseModel):
    decision_id: UUID
    task_id: UUID
    version: int
    action: ApprovalAction
    actor_subject: str
    reason: str | None
    transport: str
    decided_at: datetime


class QuotationDetailOut(BaseModel):
    summary: QuotationSummaryOut
    enquiry_text: str
    pricing_issues: list[dict[str, object]]
    versions: list[VersionOut]
    tasks: list[TaskOut]
    decisions: list[DecisionOut]
    pending_task: TaskOut | None
    can_decide: bool
    can_revise: bool


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID = Field(description="New random UUID per click; a retry reuses it")
    action: ApprovalAction
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Hash of the version seen")
    reason: str | None = Field(default=None, max_length=1000)


class DecisionResultOut(BaseModel):
    decision: DecisionOut
    replayed: bool


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=64)
    quantity: str = Field(min_length=1, max_length=30, examples=["2"])
    unit: str = Field(min_length=1, max_length=32)


class RevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID
    base_version: int | None
    lines: list[LineIn] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=1000)


class RevisionOut(BaseModel):
    quotation_id: UUID
    version: int
    replayed: bool


class PricingIssueOut(BaseModel):
    line_index: int
    sku: str
    code: str
    message: str


class EnquirySummaryOut(BaseModel):
    enquiry_id: UUID
    channel: str
    status: EnquiryStatus
    customer_reference: str | None
    received_at: datetime
    preview: str


class EnrollmentIn(BaseModel):
    """Deprecated (phase 1): link on any active connection of this provider. New clients use
    POST /connections/{id}/enrollments."""

    model_config = ConfigDict(extra="forbid")
    channel: str = Field(min_length=1, max_length=32)


class EnrollmentOut(BaseModel):
    channel: str
    code: str
    expires_at: datetime


class BindingOut(BaseModel):
    binding_id: UUID
    subject: str
    channel: str
    connection_id: str  # the connection's legacy key, as before
    connection_ref: UUID
    connection_name: str
    external_user_id: str
    created_at: datetime
    revoked_at: datetime | None


class DocumentOut(BaseModel):
    version: int
    sha256: str
    size_bytes: int
    renderer: str
    created_at: datetime


class DeliveryOut(BaseModel):
    delivery_id: UUID
    version: int
    channel: str
    recipient: str
    status: DeliveryStatus
    provider_reference: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None
    first_downloaded_at: datetime | None


class CustomerResponseOut(BaseModel):
    response_id: UUID
    version: int
    outcome: ResponseOutcome
    actor: str
    reason: str | None
    differences: list[str]
    revised_version: int | None
    created_at: datetime


class CustomerViewOut(BaseModel):
    """What the customer holds and how they answered (M6)."""

    offered_version: int | None
    customer_status: CustomerStatus | None
    valid_until: datetime | None
    issued_at: datetime | None
    responses: list[CustomerResponseOut]
    can_withdraw: bool


class WithdrawalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID = Field(description="New random UUID per click; a retry reuses it")
    reason: str = Field(min_length=1, max_length=1000)


class WithdrawalOut(BaseModel):
    response: CustomerResponseOut
    replayed: bool


def _response(item: RecordedResponse) -> CustomerResponseOut:
    return CustomerResponseOut(
        response_id=item.response_id,
        version=item.version,
        outcome=item.outcome,
        actor=item.actor,
        reason=item.reason,
        differences=list(item.differences),
        revised_version=item.revised_version,
        created_at=item.created_at,
    )


class DeliveryOverviewOut(BaseModel):
    document: DocumentOut | None
    deliveries: list[DeliveryOut]


def _document(item: StoredDocument) -> DocumentOut:
    return DocumentOut(
        version=item.version,
        sha256=item.sha256,
        size_bytes=item.size_bytes,
        renderer=item.renderer,
        created_at=item.created_at,
    )


def _delivery(item: DeliveryRecord) -> DeliveryOut:
    return DeliveryOut(
        delivery_id=item.delivery_id,
        version=item.version,
        channel=item.channel,
        recipient=item.recipient,
        status=item.status,
        provider_reference=item.provider_reference,
        last_error=item.last_error,
        created_at=item.created_at,
        updated_at=item.updated_at,
        delivered_at=item.delivered_at,
        first_downloaded_at=item.first_downloaded_at,
    )


def _summary(item: QuotationSummary) -> QuotationSummaryOut:
    return QuotationSummaryOut(
        quotation_id=item.quotation_id,
        enquiry_id=item.enquiry_id,
        number=item.number,
        status=item.status,
        current_version=item.current_version,
        customer_reference=item.customer_reference,
        currency=item.currency,
        total=item.total,
        updated_at=item.updated_at,
    )


def _decision(item: RecordedDecision) -> DecisionOut:
    return DecisionOut(
        decision_id=item.decision_id,
        task_id=item.task_id,
        version=item.version,
        action=item.action,
        actor_subject=item.actor_subject,
        reason=item.reason,
        transport=item.transport,
        decided_at=item.decided_at,
    )


def _detail(item: QuotationDetail, context: TenantContext) -> QuotationDetailOut:
    tasks = [
        TaskOut(
            task_id=task.task_id,
            version=task.version,
            content_sha256=task.content_sha256,
            status=task.status,
            created_at=task.created_at,
            decided_at=task.decided_at,
        )
        for task in item.tasks
    ]
    pending = next((task for task in tasks if task.status is TaskStatus.PENDING), None)
    current = item.current
    return QuotationDetailOut(
        summary=_summary(item.summary),
        enquiry_text=item.enquiry_text,
        pricing_issues=[dict(issue) for issue in item.pricing_issues],
        versions=[
            VersionOut(
                version=version.version,
                content=dict(version.content),
                content_sha256=version.content_sha256,
                created_by=version.created_by,
                reason=version.reason,
                created_at=version.created_at,
            )
            for version in item.versions
        ],
        tasks=tasks,
        decisions=[_decision(decision) for decision in item.decisions],
        pending_task=pending,
        # UI hints only; the server re-checks everything when the action is submitted.
        can_decide=pending is not None
        and Permission.APPROVE_QUOTATIONS in context.permissions
        and (current is None or current.created_by != context.subject),
        can_revise=item.summary.status in REVISABLE
        and Permission.DRAFT_QUOTATIONS in context.permissions,
    )


def _binding(item: ChannelBinding, names: dict[UUID, str]) -> BindingOut:
    return BindingOut(
        binding_id=item.binding_id,
        subject=item.subject,
        channel=item.channel,
        connection_id=item.connection_id,
        connection_ref=item.connection_ref,
        connection_name=names.get(item.connection_ref, item.connection_id),
        external_user_id=item.external_user_id,
        created_at=item.created_at,
        revoked_at=item.revoked_at,
    )


def _quantity(value: str) -> Decimal:
    try:
        quantity = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Quantity {value!r} is not a number") from None
    if not quantity.is_finite():
        raise ValueError(f"Quantity {value!r} is not a number")
    return quantity


def build_portal_router(
    auth: PortalAuth,
    quotations: QuotationService,
    approvals: ApprovalService,
    channels: ChannelActionService,
    enquiries: EnquiryListService,
    documents: QuotationDocumentService,
    responses: QuotationResponseService,
    connections: ConnectionStore,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["portal"])
    Context = Annotated[TenantContext, Depends(auth.tenant_context())]

    def guarded[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except PermissionDenied as error:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(error) or "Not permitted") from None
        except (QuotationNotFound, TaskNotFound, DocumentNotAvailable):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
        except (DocumentIntegrityError, ObjectNotFound):
            logger.exception("Stored quotation document failed its integrity check")
            raise integrity_failure() from None
        except UnrenderableContent as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except OfferNotOpen as error:
            closed = {"message": str(error), "status": error.status.value}
            raise HTTPException(status.HTTP_409_CONFLICT, closed) from None
        except (NotIssued, ResponseConflict) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except DecisionConflict as error:
            detail: dict[str, object] = {"message": str(error)}
            if error.recorded is not None:
                detail["decision"] = _decision(error.recorded).model_dump(mode="json")
            raise HTTPException(status.HTTP_409_CONFLICT, detail) from None
        except (StaleVersion, RevisionConflict, RevisionNotAllowed, IntegrityError) as error:
            message = str(error) if not isinstance(error, IntegrityError) else "Conflict"
            raise HTTPException(status.HTTP_409_CONFLICT, message) from None
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except (SQLAlchemyError, OSError):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None

    @router.get("/quotations", response_model=list[QuotationSummaryOut])
    def list_quotations(
        context: Context,
        status_filter: Annotated[list[QuotationStatus] | None, Query(alias="status")] = None,
    ) -> list[QuotationSummaryOut]:
        statuses = frozenset(status_filter) if status_filter else None
        return [_summary(item) for item in guarded(lambda: quotations.list(context, statuses))]

    @router.get("/quotations/{quotation_id}", response_model=QuotationDetailOut)
    def get_quotation(quotation_id: UUID, context: Context) -> QuotationDetailOut:
        return _detail(guarded(lambda: quotations.get(context, quotation_id)), context)

    @router.get(
        "/quotations/{quotation_id}/versions/{version}/preview.pdf",
        response_class=Response,
        responses={200: {"content": {"application/pdf": {}}}, 404: {}, 422: {}},
    )
    def preview_pdf(quotation_id: UUID, version: int, context: Context) -> Response:
        return pdf_response(guarded(lambda: documents.preview(context, quotation_id, version)))

    @router.get(
        "/quotations/{quotation_id}/document.pdf",
        response_class=Response,
        responses={200: {"content": {"application/pdf": {}}}, 404: {}},
    )
    def approved_pdf(quotation_id: UUID, context: Context) -> Response:
        return pdf_response(guarded(lambda: documents.approved(context, quotation_id)))

    @router.get("/quotations/{quotation_id}/delivery", response_model=DeliveryOverviewOut)
    def delivery(quotation_id: UUID, context: Context) -> DeliveryOverviewOut:
        overview = guarded(lambda: documents.overview(context, quotation_id))
        return DeliveryOverviewOut(
            document=None if overview.document is None else _document(overview.document),
            deliveries=[_delivery(item) for item in overview.deliveries],
        )

    @router.get("/quotations/{quotation_id}/customer", response_model=CustomerViewOut)
    def customer_view(quotation_id: UUID, context: Context) -> CustomerViewOut:
        offer, answers = guarded(lambda: responses.history(context, quotation_id))
        state = None if offer is None else responses.status_of(offer)
        allowed = bool(context.roles & WITHDRAWING_ROLES)
        return CustomerViewOut(
            offered_version=None if offer is None else offer.version,
            customer_status=state,
            valid_until=None if offer is None else offer.valid_until,
            issued_at=None if offer is None else offer.issued_at,
            responses=[_response(item) for item in answers],
            # UI hint only; the server re-checks everything on withdrawal.
            can_withdraw=allowed and state is CustomerStatus.OPEN,
        )

    @router.post(
        "/quotations/{quotation_id}/withdrawal",
        response_model=WithdrawalOut,
        responses={403: {}, 409: {"description": "Not issued, or already answered/closed"}},
    )
    def withdraw(quotation_id: UUID, body: WithdrawalIn, context: Context) -> WithdrawalOut:
        recorded, replayed = guarded(
            lambda: responses.withdraw(context, quotation_id, body.command_id, body.reason)
        )
        return WithdrawalOut(response=_response(recorded), replayed=replayed)

    @router.post(
        "/approval-tasks/{task_id}/decision",
        response_model=DecisionResultOut,
        responses={403: {}, 404: {}, 409: {"description": "Already decided, or stale version"}},
    )
    def decide(task_id: UUID, body: DecisionIn, context: Context) -> DecisionResultOut:
        def run() -> DecisionResultOut:
            command = DecisionCommand(
                command_id=body.command_id,
                tenant_id=context.tenant_id,
                task_id=task_id,
                content_sha256=body.content_sha256,
                action=body.action,
                reason=body.reason,
                transport=PORTAL_TRANSPORT,
                evidence_ref=None,
                received_at=clock(),
            )
            result = approvals.decide(context.subject, command)
            return DecisionResultOut(decision=_decision(result.decision), replayed=result.replayed)

        return guarded(run)

    @router.post(
        "/quotations/{quotation_id}/revisions",
        response_model=RevisionOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"description": "Replay of an earlier revision command"},
            409: {"description": "Not waiting for a revision, or changed meanwhile"},
            422: {"model": list[PricingIssueOut], "description": "Lines cannot be priced"},
        },
    )
    def revise(
        quotation_id: UUID, body: RevisionIn, context: Context, response: Response
    ) -> RevisionOut | JSONResponse:
        def run() -> RevisionOut | JSONResponse:
            lines = [
                RequestedLine(line.sku.strip(), _quantity(line.quantity), line.unit.strip())
                for line in body.lines
            ]
            outcome = quotations.revise(
                context, quotation_id, body.command_id, body.base_version, lines, body.reason
            )
            if isinstance(outcome, PricingRejected):
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={
                        "detail": "These lines cannot be priced from current ERP data",
                        "issues": [
                            PricingIssueOut(
                                line_index=issue.line_index,
                                sku=issue.sku,
                                code=issue.code.value,
                                message=issue.message,
                            ).model_dump()
                            for issue in outcome.issues
                        ],
                    },
                )
            if outcome.replayed:
                response.status_code = status.HTTP_200_OK
            return RevisionOut(
                quotation_id=outcome.quotation_id,
                version=outcome.version,
                replayed=outcome.replayed,
            )

        return guarded(run)

    @router.get("/enquiries", response_model=list[EnquirySummaryOut])
    def list_enquiries(
        context: Context,
        status_filter: Annotated[list[EnquiryStatus] | None, Query(alias="status")] = None,
    ) -> list[EnquirySummaryOut]:
        statuses = frozenset(status_filter) if status_filter else None
        return [
            EnquirySummaryOut(
                enquiry_id=item.enquiry_id,
                channel=item.channel,
                status=item.status,
                customer_reference=item.customer_reference,
                received_at=item.received_at,
                preview=item.preview,
            )
            for item in guarded(lambda: enquiries.list(context, statuses))
        ]

    @router.post(
        "/channel-enrollments", response_model=EnrollmentOut, status_code=status.HTTP_201_CREATED
    )
    def enroll(body: EnrollmentIn, context: Context) -> EnrollmentOut:
        new = guarded(lambda: channels.start_enrollment(context, body.channel))
        return EnrollmentOut(channel=body.channel, code=new.code, expires_at=new.expires_at)

    @router.get("/channel-bindings", response_model=list[BindingOut])
    def list_bindings(context: Context) -> list[BindingOut]:
        def load() -> tuple[list[ChannelBinding], dict[UUID, str]]:
            names = {
                row.connection_id: row.display_name
                for row in connections.list_for(context.tenant_id)
            }
            return channels.bindings(context), names

        items, names = guarded(load)
        return [_binding(item, names) for item in items]

    @router.delete(
        "/channel-bindings/{binding_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={404: {}},
    )
    def revoke(binding_id: UUID, context: Context) -> None:
        if not guarded(lambda: channels.revoke(context, binding_id)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    return router
