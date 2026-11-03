"""Quotation documents for people (portal) and customers (pull channels such as the API).

- Previews: any stored version can be rendered on request for staff, marked on every page as
  not an offer. Previews are never stored or sent.
- Approved documents: rendered once by the delivery job and stored; reading one re-checks its
  SHA-256 so a damaged or swapped file is never served.
- Customers only see a document after a delivery offered it to them (status available/sent).
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from sales_platform.domain.documents import (
    DeliveryRecord,
    DocumentIntegrityError,
    DocumentNotAvailable,
    DocumentSource,
    StoredDocument,
    document_filename,
)
from sales_platform.domain.quotations import QuotationNotFound, quotation_number
from sales_platform.domain.tenancy import Permission, TenantContext
from sales_platform.ports.documents import DocumentReader, QuotationRenderer
from sales_platform.ports.storage import ObjectNotFound, ObjectStorage

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def read_verified(storage: ObjectStorage, document: StoredDocument) -> bytes:
    """The stored bytes, only if they still match the hash recorded at rendering."""
    data = storage.get(document.storage_key)
    if hashlib.sha256(data).hexdigest() != document.sha256:
        raise DocumentIntegrityError(f"Stored document {document.document_id} does not match")
    return data


def with_logo(storage: ObjectStorage, source: DocumentSource) -> DocumentSource:
    """Adds the business's logo (phase 2) when it is stored and still matches its hash.

    The logo is decoration: a missing or changed file is logged and the PDF is made without it,
    rather than blocking the quotation. Everything commercial on the PDF is unaffected.
    """
    if source.seller is None or source.seller_logo_ref is None:
        return source
    key, digest = source.seller_logo_ref
    try:
        data = storage.get(key)
    except ObjectNotFound:
        logger.warning("Logo file %s is missing; the PDF is made without it", key)
        return source
    if hashlib.sha256(data).hexdigest() != digest:
        logger.warning("Logo file %s does not match its hash; the PDF is made without it", key)
        return source
    return replace(source, seller=replace(source.seller, logo_png=data))


@dataclass(frozen=True)
class PdfFile:
    filename: str
    data: bytes


@dataclass(frozen=True)
class DeliveryOverview:
    document: StoredDocument | None
    deliveries: list[DeliveryRecord]


@dataclass(frozen=True)
class OfferedQuotation:
    """What a customer may see: the delivered document and the version it was made from."""

    delivery: DeliveryRecord
    document: StoredDocument
    content: dict[str, object]

    @property
    def number(self) -> str:
        return quotation_number(self.document.quotation_id)

    @property
    def filename(self) -> str:
        return document_filename(self.document.quotation_id, self.document.version)


class QuotationDocumentService:
    def __init__(
        self,
        reader: DocumentReader,
        storage: ObjectStorage,
        renderer: QuotationRenderer,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._reader = reader
        self._storage = storage
        self._renderer = renderer
        self._clock = clock

    # --- portal (staff) -----------------------------------------------------------------
    def preview(self, context: TenantContext, quotation_id: UUID, version: int) -> PdfFile:
        """Raises QuotationNotFound for unknown versions; UnrenderableContent if impossible."""
        context.require(Permission.VIEW_QUOTATIONS)
        source = self._reader.source(context.tenant_id, quotation_id, version)
        if source is None:
            raise QuotationNotFound("Quotation version not found")
        return PdfFile(
            document_filename(quotation_id, version, preview=True),
            self._renderer.render(with_logo(self._storage, source), preview=True),
        )

    def approved(self, context: TenantContext, quotation_id: UUID) -> PdfFile:
        context.require(Permission.VIEW_QUOTATIONS)
        document = self._reader.approved_document(context.tenant_id, quotation_id)
        if document is None:
            raise DocumentNotAvailable("No approved document yet")
        return PdfFile(
            document_filename(quotation_id, document.version),
            read_verified(self._storage, document),
        )

    def overview(self, context: TenantContext, quotation_id: UUID) -> DeliveryOverview:
        context.require(Permission.VIEW_QUOTATIONS)
        return DeliveryOverview(
            self._reader.approved_document(context.tenant_id, quotation_id),
            self._reader.deliveries(context.tenant_id, quotation_id),
        )

    # --- customers on pull channels -----------------------------------------------------
    def offered(self, context: TenantContext, enquiry_id: UUID, channel: str) -> OfferedQuotation:
        """Raises DocumentNotAvailable until a quotation was delivered for this enquiry
        (also for unknown enquiries and other tenants' enquiries: one message for all)."""
        context.require(Permission.VIEW_ENQUIRIES)
        found = self._reader.offered(context.tenant_id, enquiry_id, channel)
        if found is None:
            raise DocumentNotAvailable("No quotation has been issued for this enquiry yet")
        return OfferedQuotation(*found)

    def download(self, context: TenantContext, enquiry_id: UUID, channel: str) -> PdfFile:
        offer = self.offered(context, enquiry_id, channel)
        data = read_verified(self._storage, offer.document)
        self._reader.record_download(context.tenant_id, offer.delivery.delivery_id, self._clock())
        return PdfFile(offer.filename, data)
