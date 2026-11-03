"""Quotation document and delivery storage. Every query is tenant-scoped (filter + RLS).

- queue_delivery: called by the approval workflow; writes the delivery row (outbox) and its job
  in one transaction.
- SqlDeliveryStore: worker role; rendering inputs, documents, delivery progress.
- SqlDocumentReader: API role; portal reads and customer downloads.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, Row, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.accounts.persistence.tables import TenantPricingPolicy, TenantProfile
from sales_platform.enquiries.persistence.tables import Enquiry
from sales_platform.integrations.persistence.tables import InboxEvent
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.persistence.jobs import queue_job
from sales_platform.platform.persistence.models import Tenant
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.quotations.domain.documents import (
    OFFERED_STATUSES,
    DeliveryRecord,
    DeliveryStatus,
    DocumentKind,
    DocumentSource,
    SellerDetails,
    StoredDocument,
)
from sales_platform.quotations.domain.quotations import ApprovalAction, QuotationStatus
from sales_platform.quotations.persistence.tables import (
    ApprovalDecision,
    Quotation,
    QuotationDelivery,
    QuotationDocument,
    QuotationVersion,
)
from sales_platform.quotations.ports.documents import DeliveryWork

_DELIVERY_COLUMNS = (
    QuotationDelivery.id,
    QuotationDelivery.quotation_id,
    QuotationDelivery.version,
    QuotationDelivery.channel,
    QuotationDelivery.recipient,
    QuotationDelivery.status,
    QuotationDelivery.document_id,
    QuotationDelivery.provider_reference,
    QuotationDelivery.last_error,
    QuotationDelivery.created_at,
    QuotationDelivery.updated_at,
    QuotationDelivery.delivered_at,
    QuotationDelivery.first_downloaded_at,
)
_DOCUMENT_COLUMNS = (
    QuotationDocument.id.label("doc_id"),
    QuotationDocument.quotation_id.label("doc_quotation_id"),
    QuotationDocument.version.label("doc_version"),
    QuotationDocument.content_sha256.label("doc_content_sha256"),
    QuotationDocument.storage_key.label("doc_storage_key"),
    QuotationDocument.sha256.label("doc_sha256"),
    QuotationDocument.size_bytes.label("doc_size_bytes"),
    QuotationDocument.renderer.label("doc_renderer"),
    QuotationDocument.created_at.label("doc_created_at"),
    QuotationDocument.valid_until.label("doc_valid_until"),
)
_APPROVED_DOCUMENT = QuotationDocument.kind == DocumentKind.APPROVED.value


def _delivery(row: Row[Any]) -> DeliveryRecord:
    return DeliveryRecord(
        delivery_id=row.id,
        quotation_id=row.quotation_id,
        version=row.version,
        channel=row.channel,
        recipient=row.recipient,
        status=DeliveryStatus(row.status),
        document_id=row.document_id,
        provider_reference=row.provider_reference,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        delivered_at=row.delivered_at,
        first_downloaded_at=row.first_downloaded_at,
    )


def _document(row: Row[Any]) -> StoredDocument | None:
    if row.doc_id is None:
        return None
    return StoredDocument(
        document_id=row.doc_id,
        quotation_id=row.doc_quotation_id,
        version=row.doc_version,
        content_sha256=row.doc_content_sha256,
        storage_key=row.doc_storage_key,
        sha256=row.doc_sha256,
        size_bytes=row.doc_size_bytes,
        renderer=row.doc_renderer,
        created_at=row.doc_created_at,
        valid_until=row.doc_valid_until,
    )


def _same_version_document() -> Any:
    return (
        (QuotationDocument.tenant_id == QuotationDelivery.tenant_id)
        & (QuotationDocument.quotation_id == QuotationDelivery.quotation_id)
        & (QuotationDocument.version == QuotationDelivery.version)
        & _APPROVED_DOCUMENT
    )


def _source_query(tenant_id: UUID, quotation_id: UUID, version: int) -> Any:
    """Version content with what the PDF prints; approved_at only for an approve decision on
    exactly this version and content hash."""
    return (
        select(
            QuotationVersion.content,
            QuotationVersion.content_sha256,
            Tenant.name,
            Enquiry.customer_reference,
            ApprovalDecision.decided_at,
            Quotation.status,
            Quotation.current_version,
            TenantProfile.legal_name,
            TenantProfile.address,
            TenantProfile.tax_id_label,
            TenantProfile.tax_id,
            TenantProfile.logo_storage_key,
            TenantProfile.logo_sha256,
        )
        .join(
            Quotation,
            (Quotation.tenant_id == QuotationVersion.tenant_id)
            & (Quotation.id == QuotationVersion.quotation_id),
        )
        .join(
            Enquiry,
            (Enquiry.tenant_id == Quotation.tenant_id) & (Enquiry.id == Quotation.enquiry_id),
        )
        .join(Tenant, Tenant.id == QuotationVersion.tenant_id)
        .outerjoin(TenantProfile, TenantProfile.tenant_id == QuotationVersion.tenant_id)
        .outerjoin(
            ApprovalDecision,
            (ApprovalDecision.tenant_id == QuotationVersion.tenant_id)
            & (ApprovalDecision.quotation_id == QuotationVersion.quotation_id)
            & (ApprovalDecision.version == QuotationVersion.version)
            & (ApprovalDecision.content_sha256 == QuotationVersion.content_sha256)
            & (ApprovalDecision.action == ApprovalAction.APPROVE.value),
        )
        .where(
            QuotationVersion.tenant_id == tenant_id,
            QuotationVersion.quotation_id == quotation_id,
            QuotationVersion.version == version,
        )
    )


def _source(row: Row[Any], quotation_id: UUID, version: int) -> DocumentSource:
    """The business's PDF details are read at render time; a document already rendered keeps
    the details it was made with (approved PDFs are stored once)."""
    seller = None
    if row.legal_name:
        seller = SellerDetails(
            legal_name=row.legal_name,
            address_lines=tuple((row.address or "").splitlines()),
            tax_id_label=row.tax_id_label,
            tax_id=row.tax_id,
        )
    logo = None if row.logo_storage_key is None else (row.logo_storage_key, row.logo_sha256)
    return DocumentSource(
        tenant_name=row.name,
        quotation_id=quotation_id,
        version=version,
        content=dict(row.content),
        content_sha256=row.content_sha256,
        customer_reference=row.customer_reference,
        approved_at=row.decided_at,
        seller=seller,
        seller_logo_ref=logo if seller is not None else None,
    )


def queue_delivery(
    connection: Connection, tenant_id: UUID, quotation_id: UUID, version: int
) -> bool:
    """Queue delivery of an approved version to whoever sent the enquiry, on its connection.

    The recipient is the envelope's reply_to, fixed at intake by
    domain.documents.delivery_recipient (and backfilled by migration 0008). Safe to repeat: one
    delivery
    per target. Returns False (and queues nothing) unless this version is the approved current
    version.
    """
    row = connection.execute(
        select(
            Quotation.status,
            Quotation.current_version,
            Enquiry.channel,
            Enquiry.correlation_id,
            # Envelopes written by a pre-phase-1 process have no reply_to; the only tool then was
            # the API, whose reply address is the submitting key's connection.
            func.coalesce(InboxEvent.envelope["reply_to"].astext, InboxEvent.connection_id).label(
                "reply_to"
            ),
        )
        .join(
            Enquiry,
            (Enquiry.tenant_id == Quotation.tenant_id) & (Enquiry.id == Quotation.enquiry_id),
        )
        .join(
            InboxEvent,
            (InboxEvent.tenant_id == Enquiry.tenant_id) & (InboxEvent.id == Enquiry.inbox_event_id),
        )
        .where(Quotation.tenant_id == tenant_id, Quotation.id == quotation_id)
    ).one_or_none()
    if (
        row is None
        or row.status != QuotationStatus.APPROVED.value
        or row.current_version != version
    ):
        return False
    delivery_id = uuid4()
    created = connection.execute(
        insert(QuotationDelivery)
        .values(
            id=delivery_id,
            tenant_id=tenant_id,
            quotation_id=quotation_id,
            version=version,
            channel=row.channel,
            recipient=row.reply_to,
            status=DeliveryStatus.PENDING.value,
        )
        .on_conflict_do_nothing(constraint="uq_quotation_deliveries_target")
    ).rowcount
    if created:
        queue_job(
            connection,
            tenant_id,
            JobKind.DELIVER_QUOTATION,
            {"delivery_id": str(delivery_id)},
            row.correlation_id,
        )
    return True


def queue_expiry(connection: Connection, tenant_id: UUID, delivery_id: UUID) -> None:
    """Schedule expire_quotation for the moment the delivered version stops being valid."""
    row = connection.execute(
        select(
            QuotationDelivery.quotation_id,
            QuotationDelivery.version,
            QuotationDocument.valid_until,
            Enquiry.correlation_id,
        )
        .join(
            QuotationDocument,
            (QuotationDocument.tenant_id == QuotationDelivery.tenant_id)
            & (QuotationDocument.id == QuotationDelivery.document_id),
        )
        .join(
            Quotation,
            (Quotation.tenant_id == QuotationDelivery.tenant_id)
            & (Quotation.id == QuotationDelivery.quotation_id),
        )
        .join(
            Enquiry,
            (Enquiry.tenant_id == Quotation.tenant_id) & (Enquiry.id == Quotation.enquiry_id),
        )
        .where(QuotationDelivery.tenant_id == tenant_id, QuotationDelivery.id == delivery_id)
    ).one()
    queue_job(
        connection,
        tenant_id,
        JobKind.EXPIRE_QUOTATION,
        {"quotation_id": str(row.quotation_id), "version": str(row.version)},
        row.correlation_id,
        run_at=row.valid_until,
    )


class SqlDeliveryStore:
    """Worker-role delivery storage."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def delivery(self, tenant_id: UUID, delivery_id: UUID) -> DeliveryWork | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(
                    *_DELIVERY_COLUMNS,
                    *_DOCUMENT_COLUMNS,
                    Tenant.name,
                    Enquiry.customer_reference,
                    TenantPricingPolicy.quote_validity_days,
                    Enquiry.id.label("enquiry_id"),
                    Enquiry.correlation_id,
                    InboxEvent.connection_ref,
                    QuotationDelivery.outbound_message_id,
                )
                .join(
                    Quotation,
                    (Quotation.tenant_id == QuotationDelivery.tenant_id)
                    & (Quotation.id == QuotationDelivery.quotation_id),
                )
                .join(
                    Enquiry,
                    (Enquiry.tenant_id == Quotation.tenant_id)
                    & (Enquiry.id == Quotation.enquiry_id),
                )
                .join(
                    InboxEvent,
                    (InboxEvent.tenant_id == Enquiry.tenant_id)
                    & (InboxEvent.id == Enquiry.inbox_event_id),
                )
                .join(Tenant, Tenant.id == QuotationDelivery.tenant_id)
                .join(
                    TenantPricingPolicy,
                    TenantPricingPolicy.tenant_id == QuotationDelivery.tenant_id,
                )
                .outerjoin(QuotationDocument, _same_version_document())
                .where(
                    QuotationDelivery.tenant_id == tenant_id, QuotationDelivery.id == delivery_id
                )
            ).one_or_none()
        if row is None:
            return None
        return DeliveryWork(
            _delivery(row),
            row.name,
            row.customer_reference,
            _document(row),
            row.quote_validity_days,
            row.connection_ref,
            row.enquiry_id,
            row.correlation_id,
            row.outbound_message_id,
        )

    def document(self, tenant_id: UUID, document_id: UUID) -> StoredDocument | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_DOCUMENT_COLUMNS).where(
                    QuotationDocument.tenant_id == tenant_id, QuotationDocument.id == document_id
                )
            ).one_or_none()
        return None if row is None else _document(row)

    def approved_source(
        self, tenant_id: UUID, quotation_id: UUID, version: int
    ) -> DocumentSource | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(_source_query(tenant_id, quotation_id, version)).one_or_none()
        if (
            row is None
            or row.decided_at is None
            or row.status != QuotationStatus.APPROVED.value
            or row.current_version != version
        ):
            return None
        return _source(row, quotation_id, version)

    def save_document(self, tenant_id: UUID, document: StoredDocument) -> StoredDocument:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                insert(QuotationDocument)
                .values(
                    id=document.document_id,
                    tenant_id=tenant_id,
                    quotation_id=document.quotation_id,
                    version=document.version,
                    kind=DocumentKind.APPROVED.value,
                    content_sha256=document.content_sha256,
                    storage_key=document.storage_key,
                    sha256=document.sha256,
                    size_bytes=document.size_bytes,
                    renderer=document.renderer,
                    created_at=document.created_at,
                    valid_until=document.valid_until,
                )
                .on_conflict_do_nothing(constraint="uq_quotation_documents_version")
            )
            row = connection.execute(
                select(*_DOCUMENT_COLUMNS).where(
                    QuotationDocument.tenant_id == tenant_id,
                    QuotationDocument.quotation_id == document.quotation_id,
                    QuotationDocument.version == document.version,
                    _APPROVED_DOCUMENT,
                )
            ).one()
        stored = _document(row)
        assert stored is not None
        return stored

    def set_status(
        self,
        tenant_id: UUID,
        delivery_id: UUID,
        status: DeliveryStatus,
        *,
        expected: frozenset[DeliveryStatus],
        document_id: UUID | None = None,
        provider_reference: str | None = None,
        error: str | None = None,
    ) -> bool:
        values: dict[str, object] = {
            "status": status.value,
            "last_error": error,
            "updated_at": func.now(),
        }
        if document_id is not None:
            values["document_id"] = document_id
        if provider_reference is not None:
            values["provider_reference"] = provider_reference
        if status in OFFERED_STATUSES:
            values["delivered_at"] = func.now()
        with tenant_transaction(self._engine, tenant_id) as connection:
            changed = connection.execute(
                update(QuotationDelivery)
                .where(
                    QuotationDelivery.tenant_id == tenant_id,
                    QuotationDelivery.id == delivery_id,
                    QuotationDelivery.status.in_([item.value for item in expected]),
                )
                .values(**values)
            ).rowcount
            if changed == 1 and status in OFFERED_STATUSES:
                queue_expiry(connection, tenant_id, delivery_id)
        return bool(changed == 1)


class SqlDocumentReader:
    """API-role reads of documents and deliveries."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def source(self, tenant_id: UUID, quotation_id: UUID, version: int) -> DocumentSource | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(_source_query(tenant_id, quotation_id, version)).one_or_none()
        return None if row is None else _source(row, quotation_id, version)

    def approved_document(self, tenant_id: UUID, quotation_id: UUID) -> StoredDocument | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_DOCUMENT_COLUMNS)
                .where(
                    QuotationDocument.tenant_id == tenant_id,
                    QuotationDocument.quotation_id == quotation_id,
                    _APPROVED_DOCUMENT,
                )
                .order_by(QuotationDocument.version.desc())
                .limit(1)
            ).one_or_none()
        return None if row is None else _document(row)

    def deliveries(self, tenant_id: UUID, quotation_id: UUID) -> list[DeliveryRecord]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                select(*_DELIVERY_COLUMNS)
                .where(
                    QuotationDelivery.tenant_id == tenant_id,
                    QuotationDelivery.quotation_id == quotation_id,
                )
                .order_by(QuotationDelivery.version, QuotationDelivery.created_at)
            ).all()
        return [_delivery(row) for row in rows]

    def offered(
        self, tenant_id: UUID, enquiry_id: UUID, channel: str
    ) -> tuple[DeliveryRecord, StoredDocument, dict[str, object]] | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_DELIVERY_COLUMNS, *_DOCUMENT_COLUMNS, QuotationVersion.content)
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
                .where(
                    QuotationDelivery.tenant_id == tenant_id,
                    Quotation.enquiry_id == enquiry_id,
                    QuotationDelivery.channel == channel,
                    QuotationDelivery.status.in_([item.value for item in OFFERED_STATUSES]),
                )
                .order_by(QuotationDelivery.version.desc())
                .limit(1)
            ).one_or_none()
        if row is None:
            return None
        document = _document(row)
        assert document is not None
        return _delivery(row), document, dict(row.content)

    def record_download(self, tenant_id: UUID, delivery_id: UUID, at: datetime) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                update(QuotationDelivery)
                .where(
                    QuotationDelivery.tenant_id == tenant_id,
                    QuotationDelivery.id == delivery_id,
                    QuotationDelivery.first_downloaded_at.is_(None),
                )
                .values(first_downloaded_at=at)
            )
