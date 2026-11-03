"""Customer answers, withdrawal and expiry storage (M6). Every query is tenant-scoped.

Used by the API role (customer answers, staff withdrawal) and the worker role (expiry, workflow
resume, notifications). `record` re-checks the offer under a row lock on the quotation, so an
acceptance and a withdrawal (or expiry) racing each other produce exactly one closing outcome.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, Row, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.domain.acceptance import (
    QUOTATION_STATUS_FOR,
    SYSTEM_SUBJECT,
    CustomerStatus,
    Offer,
    OfferNotOpen,
    RecordedResponse,
    ResponseOutcome,
    customer_status,
)
from sales_platform.domain.documents import OFFERED_STATUSES
from sales_platform.domain.jobs import JobKind
from sales_platform.domain.quotations import QuotationStatus
from sales_platform.persistence.jobs import queue_job
from sales_platform.persistence.models import (
    CustomerActionRef,
    Quotation,
    QuotationDelivery,
    QuotationDocument,
    QuotationResponse,
    QuotationVersion,
    Tenant,
)
from sales_platform.persistence.quotations import enquiry_link, version_values
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.acceptance import NewResponse

_RESPONSE_COLUMNS = (
    QuotationResponse.id,
    QuotationResponse.quotation_id,
    QuotationResponse.version,
    QuotationResponse.content_sha256,
    QuotationResponse.outcome,
    QuotationResponse.actor,
    QuotationResponse.reason,
    QuotationResponse.differences,
    QuotationResponse.revised_version,
    QuotationResponse.created_at,
)


def _response(row: Row[Any]) -> RecordedResponse:
    return RecordedResponse(
        response_id=row.id,
        quotation_id=row.quotation_id,
        version=row.version,
        content_sha256=row.content_sha256,
        outcome=ResponseOutcome(row.outcome),
        actor=row.actor,
        reason=row.reason,
        differences=tuple(row.differences),
        revised_version=row.revised_version,
        created_at=row.created_at,
    )


def _offer_query(tenant_id: UUID) -> Any:
    """Delivered versions with what an answer needs, newest version first."""
    return (
        select(
            Quotation.id.label("quotation_id"),
            Quotation.enquiry_id,
            Quotation.status,
            Quotation.current_version,
            QuotationDelivery.version,
            QuotationDelivery.delivered_at,
            QuotationVersion.content,
            QuotationVersion.content_sha256,
            QuotationDocument.valid_until,
            Tenant.name,
        )
        .join(
            Quotation,
            (Quotation.tenant_id == QuotationDelivery.tenant_id)
            & (Quotation.id == QuotationDelivery.quotation_id),
        )
        .join(
            QuotationDocument,
            (QuotationDocument.tenant_id == QuotationDelivery.tenant_id)
            & (QuotationDocument.id == QuotationDelivery.document_id),
        )
        .join(
            QuotationVersion,
            (QuotationVersion.tenant_id == QuotationDelivery.tenant_id)
            & (QuotationVersion.quotation_id == QuotationDelivery.quotation_id)
            & (QuotationVersion.version == QuotationDelivery.version),
        )
        .join(Tenant, Tenant.id == QuotationDelivery.tenant_id)
        .where(
            QuotationDelivery.tenant_id == tenant_id,
            QuotationDelivery.status.in_([status.value for status in OFFERED_STATUSES]),
        )
        .order_by(QuotationDelivery.version.desc())
        .limit(1)
    )


def _offer(tenant_id: UUID, row: Row[Any] | None) -> Offer | None:
    if row is None:
        return None
    return Offer(
        tenant_id=tenant_id,
        tenant_name=row.name,
        quotation_id=row.quotation_id,
        enquiry_id=row.enquiry_id,
        status=QuotationStatus(row.status),
        current_version=row.current_version,
        version=row.version,
        content=dict(row.content),
        content_sha256=row.content_sha256,
        valid_until=row.valid_until,
        issued_at=row.delivered_at,
    )


def _revision_reason(differences: tuple[str, ...]) -> str:
    text = "Re-priced when the customer accepted: " + "; ".join(differences)
    return text if len(text) <= 1000 else text[:997] + "..."


class SqlResponseStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_response(
        self, tenant_id: UUID, idempotency_key: str
    ) -> tuple[RecordedResponse, str] | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_RESPONSE_COLUMNS, QuotationResponse.fingerprint).where(
                    QuotationResponse.tenant_id == tenant_id,
                    QuotationResponse.idempotency_key == idempotency_key,
                )
            ).one_or_none()
        return None if row is None else (_response(row), row.fingerprint)

    def offer_for_enquiry(self, tenant_id: UUID, enquiry_id: UUID, channel: str) -> Offer | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                _offer_query(tenant_id).where(
                    Quotation.enquiry_id == enquiry_id, QuotationDelivery.channel == channel
                )
            ).one_or_none()
        return _offer(tenant_id, row)

    def offer_for_quotation(self, tenant_id: UUID, quotation_id: UUID) -> Offer | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                _offer_query(tenant_id).where(Quotation.id == quotation_id)
            ).one_or_none()
        return _offer(tenant_id, row)

    def record(
        self,
        tenant_id: UUID,
        offer: Offer,
        response: NewResponse,
        now: datetime,
        consume_ref: UUID | None = None,
    ) -> RecordedResponse:
        expiring = response.outcome is ResponseOutcome.EXPIRED
        with tenant_transaction(self._engine, tenant_id) as connection:
            # FOR UPDATE: a racing answer, withdrawal or expiry waits here until we commit.
            current = connection.execute(
                select(Quotation.status, Quotation.current_version)
                .where(Quotation.tenant_id == tenant_id, Quotation.id == offer.quotation_id)
                .with_for_update()
            ).one()
            still_open = (
                current.status == QuotationStatus.APPROVED.value
                and current.current_version == offer.version
                and ((now >= offer.valid_until) if expiring else (now < offer.valid_until))
            )
            if not still_open:
                raise OfferNotOpen(
                    customer_status(
                        QuotationStatus(current.status),
                        offer.version,
                        current.current_version,
                        offer.valid_until,
                        now,
                    )
                )
            revised_version: int | None = None
            quotation_values: dict[str, object] = {
                "status": QUOTATION_STATUS_FOR[response.outcome].value,
                "updated_at": func.now(),
            }
            revised = response.revised
            if revised is not None and revised.content is not None and revised.content_sha256:
                revised_version = offer.version + 1
                connection.execute(
                    insert(QuotationVersion).values(
                        **version_values(
                            tenant_id,
                            offer.quotation_id,
                            revised_version,
                            revised.content,
                            revised.content_sha256,
                            SYSTEM_SUBJECT,
                            _revision_reason(response.differences),
                            None,
                        )
                    )
                )
                quotation_values["current_version"] = revised_version
            elif revised is not None:
                # The lines cannot be priced any more: a sales operator revises (as in M4).
                quotation_values["status"] = QuotationStatus.PRICING_FAILED.value
                quotation_values["pricing_issues"] = list(revised.pricing_issues or [])
            connection.execute(
                update(Quotation)
                .where(Quotation.tenant_id == tenant_id, Quotation.id == offer.quotation_id)
                .values(**quotation_values)
            )
            response_id = uuid4()
            connection.execute(
                insert(QuotationResponse).values(
                    id=response_id,
                    tenant_id=tenant_id,
                    quotation_id=offer.quotation_id,
                    version=offer.version,
                    content_sha256=offer.content_sha256,
                    outcome=response.outcome.value,
                    actor=response.actor,
                    reason=response.reason,
                    idempotency_key=response.idempotency_key,
                    fingerprint=response.fingerprint,
                    differences=list(response.differences),
                    revised_version=revised_version,
                    created_at=now,
                )
            )
            if consume_ref is not None:
                used = connection.execute(
                    update(CustomerActionRef)
                    .where(
                        CustomerActionRef.tenant_id == tenant_id,
                        CustomerActionRef.id == consume_ref,
                        CustomerActionRef.consumed_at.is_(None),
                    )
                    .values(consumed_at=now, response_id=response_id)
                ).rowcount
                if not used:
                    raise OfferNotOpen(CustomerStatus.CLOSED)  # used meanwhile
            link = enquiry_link(connection, tenant_id, offer.quotation_id)
            queue_job(
                connection,
                tenant_id,
                JobKind.RESUME_QUOTATION,
                {
                    "enquiry_id": str(link.enquiry_id),
                    "waiting_for": "customer",
                    "version": str(offer.version),
                    "response_id": str(response_id),
                },
                link.correlation_id,
            )
            # Every answer is announced: managers hear of accept/decline, the customer gets
            # the matching confirmation or notice on their tool (phase 4).
            queue_job(
                connection,
                tenant_id,
                JobKind.NOTIFY_RESPONSE,
                {"response_id": str(response_id)},
                link.correlation_id,
            )
            if response.outcome is ResponseOutcome.ACCEPTED:
                # Phase 7 (Q1): the order follows in the same transaction as the acceptance;
                # the job creates it only if the business chose automatic orders.
                queue_job(
                    connection,
                    tenant_id,
                    JobKind.CREATE_ORDER,
                    {"quotation_id": str(offer.quotation_id)},
                    link.correlation_id,
                )
        return RecordedResponse(
            response_id=response_id,
            quotation_id=offer.quotation_id,
            version=offer.version,
            content_sha256=offer.content_sha256,
            outcome=response.outcome,
            actor=response.actor,
            reason=response.reason,
            differences=response.differences,
            revised_version=revised_version,
            created_at=now,
        )

    def response(self, tenant_id: UUID, response_id: UUID) -> RecordedResponse | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_RESPONSE_COLUMNS).where(
                    QuotationResponse.tenant_id == tenant_id, QuotationResponse.id == response_id
                )
            ).one_or_none()
        return None if row is None else _response(row)

    def responses(self, tenant_id: UUID, quotation_id: UUID) -> list[RecordedResponse]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                select(*_RESPONSE_COLUMNS)
                .where(
                    QuotationResponse.tenant_id == tenant_id,
                    QuotationResponse.quotation_id == quotation_id,
                )
                .order_by(QuotationResponse.created_at, QuotationResponse.version)
            ).all()
        return [_response(row) for row in rows]
