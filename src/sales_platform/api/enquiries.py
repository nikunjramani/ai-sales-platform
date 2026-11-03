"""Versioned enquiry intake API.

POST /api/v1/enquiries  accepts an enquiry (202), or returns the original result for a replay
                        with the same Idempotency-Key (200). A different payload under a used key
                        is rejected (409). 202 is sent only after the database commit.
GET  /api/v1/enquiries/{id}  returns status. Other tenants' IDs return 404, like unknown IDs.
GET  /api/v1/enquiries/{id}/quotation      the approved quotation issued for it (404 until then)
GET  /api/v1/enquiries/{id}/quotation.pdf  its PDF; the first download is recorded
POST /api/v1/enquiries/{id}/quotation/response  accept or decline that exact version (M6);
                        Idempotency-Key required. Accepting re-checks ERP prices first: any
                        change refuses it (409) and a revised version goes to manager approval.
GET  /api/v1/enquiries/{id}/order          the order made from the accepted quotation (phase 7):
                        number, status and total; 404 until there is one

Only quotations that a manager approved and the delivery job made available are visible here;
drafts, pending and rejected versions never are.

The tenant always comes from the API key, never from the request body.
"""

import base64
import binascii
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from sales_platform.adapters.providers.api import PROVIDER_KEY
from sales_platform.api.pdf import integrity_failure, pdf_response
from sales_platform.application.acceptance import QuotationResponseService
from sales_platform.application.api_keys import ApiKeyAuthenticator, AuthenticationFailed
from sales_platform.application.documents import OfferedQuotation, QuotationDocumentService
from sales_platform.application.intake import EnquiryIntakeService, IntakeResult
from sales_platform.application.orders import OrderService
from sales_platform.domain.acceptance import (
    CustomerStatus,
    NotCurrentOffer,
    OfferNotOpen,
    PricesChanged,
    ResponseAction,
    ResponseConflict,
    ResponseOutcome,
    ResponseRequest,
)
from sales_platform.domain.documents import DocumentIntegrityError, DocumentNotAvailable
from sales_platform.domain.intake import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_TEXT_CHARACTERS,
    MAX_TOTAL_ATTACHMENT_BYTES,
    AttachmentUpload,
    EnquirySubmission,
)
from sales_platform.domain.quotations import quotation_number
from sales_platform.domain.tenancy import PermissionDenied, TenantContext, accepts_intake
from sales_platform.ports.erp import ErpError
from sales_platform.ports.intake import EnquiryView, IdempotencyConflict
from sales_platform.ports.storage import ObjectNotFound
from sales_platform.ports.tenancy import TenantStatusReader

# Base64 is about 4/3 of the binary size; allow headroom for JSON and text.
MAX_REQUEST_BYTES = MAX_TOTAL_ATTACHMENT_BYTES * 4 // 3 + MAX_TEXT_CHARACTERS * 4 + 64 * 1024
_UNAVAILABLE = "Temporarily unavailable; retry with the same Idempotency-Key"
logger = logging.getLogger(__name__)


class AttachmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100, examples=["application/pdf"])
    content_base64: str = Field(min_length=1, max_length=MAX_ATTACHMENT_BYTES * 4 // 3 + 4)


class EnquiryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARACTERS)
    customer_reference: str | None = Field(default=None, max_length=200)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class EnquiryAccepted(BaseModel):
    enquiry_id: UUID
    status: str
    duplicate: bool
    correlation_id: UUID
    received_at: datetime
    # True when the message continued the customer's open enquiry (same key and
    # customer_reference, our question less than 7 days old, or the enquiry still being read).
    continued: bool = False


class AttachmentOut(BaseModel):
    attachment_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    rejection_reason: str | None


class LineOut(BaseModel):
    sku: str
    quantity: str
    unit: str


class ExtractionOut(BaseModel):
    outcome: str
    lines: list[LineOut]
    questions: list[str]
    review_reasons: list[str]


class EnquiryOut(BaseModel):
    enquiry_id: UUID
    channel: str
    status: str
    customer_reference: str | None
    received_at: datetime
    correlation_id: UUID
    attachments: list[AttachmentOut]
    extraction: ExtractionOut | None = None


class QuotedLineOut(BaseModel):
    sku: str
    description: str
    quantity: str
    unit: str
    unit_price: str
    line_total: str


class QuotationDocumentOut(BaseModel):
    filename: str
    sha256: str
    size_bytes: int
    download_path: str


class IssuedQuotationOut(BaseModel):
    """Customer view of an approved quotation: amounts exactly as approved, no internal data."""

    quotation_number: str
    version: int
    currency: str
    lines: list[QuotedLineOut]
    subtotal: str
    tax: str
    total: str
    issued_at: datetime | None
    document: QuotationDocumentOut
    status: CustomerStatus = Field(description="open = can be accepted or declined")
    valid_until: datetime
    content_sha256: str = Field(description="Send this back, with version, to answer")
    response_path: str


class QuotationResponseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ResponseAction
    version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=1000, description="Only when declining")


class QuotationResponseOut(BaseModel):
    outcome: ResponseOutcome
    version: int
    recorded_at: datetime
    replayed: bool


def _issued(enquiry_id: UUID, offer: OfferedQuotation) -> IssuedQuotationOut:
    content = offer.content
    lines = content["lines"]
    assert isinstance(lines, list)
    return IssuedQuotationOut(
        quotation_number=offer.number,
        version=offer.document.version,
        currency=str(content["currency"]),
        lines=[
            QuotedLineOut(
                sku=str(line["sku"]),
                description=str(line["description"]),
                quantity=str(line["quantity"]),
                unit=str(line["unit"]),
                unit_price=str(line["unit_price"]),
                line_total=str(line["line_total"]),
            )
            for line in lines
        ],
        subtotal=str(content["subtotal"]),
        tax=str(content["tax"]),
        total=str(content["total"]),
        issued_at=offer.delivery.delivered_at,
        document=QuotationDocumentOut(
            filename=offer.filename,
            sha256=offer.document.sha256,
            size_bytes=offer.document.size_bytes,
            download_path=f"/api/v1/enquiries/{enquiry_id}/quotation.pdf",
        ),
        status=CustomerStatus.OPEN,  # replaced by the route with the live status
        valid_until=offer.document.valid_until,
        content_sha256=offer.document.content_sha256,
        response_path=f"/api/v1/enquiries/{enquiry_id}/quotation/response",
    )


def _to_submission(body: EnquiryIn) -> EnquirySubmission:
    uploads = []
    for item in body.attachments:
        try:
            data = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"Attachment {item.filename!r} is not valid base64") from None
        uploads.append(AttachmentUpload(item.filename, item.content_type, data))
    return EnquirySubmission(body.text, body.customer_reference, tuple(uploads))


def _view(enquiry: EnquiryView) -> EnquiryOut:
    return EnquiryOut(
        enquiry_id=enquiry.enquiry_id,
        channel=enquiry.channel,
        status=enquiry.status.value,
        customer_reference=enquiry.customer_reference,
        received_at=enquiry.received_at,
        correlation_id=enquiry.correlation_id,
        attachments=[
            AttachmentOut(
                attachment_id=item.attachment_id,
                filename=item.filename,
                content_type=item.declared_content_type,
                size_bytes=item.size_bytes,
                status=item.status.value,
                rejection_reason=item.rejection_reason,
            )
            for item in enquiry.attachments
        ],
        extraction=None
        if enquiry.extraction is None
        else ExtractionOut(
            outcome=enquiry.extraction.outcome,
            lines=[
                LineOut(sku=line.sku, quantity=line.quantity, unit=line.unit)
                for line in enquiry.extraction.lines
            ],
            questions=list(enquiry.extraction.questions),
            review_reasons=list(enquiry.extraction.review_reasons),
        ),
    )


_NOT_ISSUED = "No quotation has been issued for this enquiry yet"


class CustomerOrderOut(BaseModel):
    """What a customer's system sees of its order: never ERP or staff details."""

    order_number: str
    quotation_number: str
    version: int
    status: str
    currency: str
    total: str
    updated_at: datetime


def build_enquiry_router(
    authenticator: ApiKeyAuthenticator,
    intake: EnquiryIntakeService,
    documents: QuotationDocumentService,
    responses: QuotationResponseService,
    statuses: TenantStatusReader | None = None,
    orders: OrderService | None = None,
) -> APIRouter:
    """statuses: when given, a suspended business's keys are refused (403, phase 2 Q13).
    orders: when given, the customer's system can follow its order (phase 7)."""
    router = APIRouter(prefix="/api/v1/enquiries", tags=["enquiries"])
    bearer = HTTPBearer(auto_error=False, description="API key: qk_<id>_<secret>")

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> TenantContext:
        try:
            if credentials is None:
                raise AuthenticationFailed("Invalid credentials")
            context = authenticator.authenticate(credentials.credentials)
            if statuses is not None and not accepts_intake(statuses.status(context.tenant_id)):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
            return context
        except AuthenticationFailed:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None
        except SQLAlchemyError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None

    def limit_body(request: Request) -> None:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_REQUEST_BYTES):
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Request too large")

    def guarded[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except PermissionDenied:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted") from None
        except IdempotencyConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except DocumentNotAvailable as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
        except PricesChanged as error:
            detail = {
                "message": str(error),
                "outcome": ResponseOutcome.REFUSED_CHANGED.value,
                "differences": list(error.differences),
                "revised_version": error.revised_version,
            }
            raise HTTPException(status.HTTP_409_CONFLICT, detail) from None
        except OfferNotOpen as error:
            detail = {"message": str(error), "status": error.status.value}
            raise HTTPException(status.HTTP_409_CONFLICT, detail) from None
        except (NotCurrentOffer, ResponseConflict) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
        except ErpError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Current prices could not be checked; nothing was recorded. Retry with the "
                "same Idempotency-Key",
            ) from None
        except (DocumentIntegrityError, ObjectNotFound):
            logger.exception("Stored quotation document failed its integrity check")
            raise integrity_failure() from None
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except (SQLAlchemyError, OSError):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None

    @router.post(
        "",
        response_model=EnquiryAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(limit_body)],
        responses={
            200: {"model": EnquiryAccepted, "description": "Replay of an accepted request"},
            401: {"description": "Missing or invalid API key"},
            403: {"description": "Key lacks enquiries:submit"},
            409: {"description": "Idempotency-Key reused with a different payload"},
            413: {"description": "Request too large"},
            503: {"description": "Not stored; retry with the same Idempotency-Key"},
        },
    )
    def submit(
        body: EnquiryIn,
        response: Response,
        context: Annotated[TenantContext, Depends(authenticate)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> EnquiryAccepted:
        def accept() -> IntakeResult:
            # Converting the body may raise ValueError (bad base64): guarded maps it to 422.
            return intake.submit(context, _to_submission(body), idempotency_key, PROVIDER_KEY)

        result = guarded(accept)
        if result.duplicate:
            response.status_code = status.HTTP_200_OK
        receipt = result.receipt
        response.headers["X-Correlation-ID"] = str(receipt.correlation_id)
        return EnquiryAccepted(
            enquiry_id=receipt.enquiry_id,
            status=receipt.status.value,
            duplicate=result.duplicate,
            correlation_id=receipt.correlation_id,
            received_at=receipt.received_at,
            continued=receipt.continued,
        )

    @router.get("/{enquiry_id}", response_model=EnquiryOut, responses={404: {}})
    def get_status(
        enquiry_id: UUID, context: Annotated[TenantContext, Depends(authenticate)]
    ) -> EnquiryOut:
        return _view(own(context, enquiry_id))

    def own(context: TenantContext, enquiry_id: UUID) -> EnquiryView:
        """Every per-enquiry route answers only for enquiries this key submitted (P1-Q9)."""
        enquiry = guarded(lambda: intake.status(context, enquiry_id, PROVIDER_KEY))
        if enquiry is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Enquiry not found")
        return enquiry

    def own_quotation(context: TenantContext, enquiry_id: UUID) -> None:
        """The quotation was delivered to this key (the recipient fixed at intake is the
        submitting key's account); another key's quotation looks like "not issued" (P1-Q9)."""
        offer = guarded(lambda: documents.offered(context, enquiry_id, PROVIDER_KEY))
        if offer.delivery.recipient != context.subject:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _NOT_ISSUED)

    @router.get("/{enquiry_id}/quotation", response_model=IssuedQuotationOut, responses={404: {}})
    def issued_quotation(
        enquiry_id: UUID, context: Annotated[TenantContext, Depends(authenticate)]
    ) -> IssuedQuotationOut:
        own_quotation(context, enquiry_id)

        def run() -> IssuedQuotationOut:
            issued = _issued(enquiry_id, documents.offered(context, enquiry_id, PROVIDER_KEY))
            offer = responses.offer(context, enquiry_id, PROVIDER_KEY)
            return issued.model_copy(update={"status": responses.status_of(offer)})

        return guarded(run)

    @router.post(
        "/{enquiry_id}/quotation/response",
        response_model=QuotationResponseOut,
        responses={
            404: {"description": "No quotation issued for this enquiry"},
            409: {
                "description": "Not the current version, not open, prices changed, or key reused"
            },
            503: {"description": "Prices could not be checked; nothing recorded; retry"},
        },
    )
    def respond(
        enquiry_id: UUID,
        body: QuotationResponseIn,
        context: Annotated[TenantContext, Depends(authenticate)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> QuotationResponseOut:
        own_quotation(context, enquiry_id)

        def run() -> QuotationResponseOut:
            request = ResponseRequest(body.action, body.version, body.content_sha256, body.reason)
            try:
                recorded, replayed = responses.respond(
                    context, enquiry_id, PROVIDER_KEY, idempotency_key, request
                )
            except IntegrityError:
                # Two requests with the same key raced; the retry returns the stored answer.
                raise ResponseConflict(
                    "A request with this Idempotency-Key is in progress"
                ) from None
            return QuotationResponseOut(
                outcome=recorded.outcome,
                version=recorded.version,
                recorded_at=recorded.created_at,
                replayed=replayed,
            )

        return guarded(run)

    @router.get(
        "/{enquiry_id}/quotation.pdf",
        response_class=Response,
        responses={200: {"content": {"application/pdf": {}}}, 404: {}},
    )
    def issued_quotation_pdf(
        enquiry_id: UUID, context: Annotated[TenantContext, Depends(authenticate)]
    ) -> Response:
        own_quotation(context, enquiry_id)
        return pdf_response(guarded(lambda: documents.download(context, enquiry_id, PROVIDER_KEY)))

    @router.get("/{enquiry_id}/order", response_model=CustomerOrderOut, responses={404: {}})
    def enquiry_order(
        enquiry_id: UUID, context: Annotated[TenantContext, Depends(authenticate)]
    ) -> CustomerOrderOut:
        own_quotation(context, enquiry_id)
        found = None if orders is None else guarded(lambda: orders.for_enquiry(context, enquiry_id))
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No order for this enquiry yet")
        return CustomerOrderOut(
            order_number=found.order_number,
            quotation_number=quotation_number(found.quotation_id),
            version=found.version,
            status=found.status.value,
            currency=found.currency,
            total=str(found.total),
            updated_at=found.updated_at,
        )

    return router
