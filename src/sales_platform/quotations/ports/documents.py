"""Quotation document rendering and delivery storage contracts.

Sending goes through the outbox and the connection's provider (ADR 0018): a delivery is carried
out by one outbound message, whose status the delivery mirrors. Adding a customer tool means
adding a provider package; see .docs/development/adding-providers.md.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.quotations.domain.documents import (
    DeliveryRecord,
    DeliveryStatus,
    DocumentSource,
    StoredDocument,
)


class QuotationRenderer(Protocol):
    renderer: str  # version label stored with each document, e.g. "quotation-pdf-v2"

    def render(self, source: DocumentSource, *, preview: bool) -> bytes:
        """Return PDF bytes. The same source must give the same bytes (retries are safe).

        A preview is marked on every page as not being an offer. Raises UnrenderableContent if
        the content cannot be shown faithfully.
        """
        ...


@dataclass(frozen=True)
class DeliveryWork:
    """A delivery with the facts needed to process it."""

    record: DeliveryRecord
    tenant_name: str
    customer_reference: str | None
    document: StoredDocument | None
    validity_days: int  # the tenant's setting, used when the document is first prepared
    connection_id: UUID | None  # the connection the enquiry arrived on (replies go there)
    enquiry_id: UUID
    correlation_id: UUID
    outbound_message_id: UUID | None  # set once the message carrying it is queued


class DeliveryStore(Protocol):
    """Worker-side delivery and document storage. Every method is safe to repeat."""

    def delivery(self, tenant_id: UUID, delivery_id: UUID) -> DeliveryWork | None: ...

    def approved_source(
        self, tenant_id: UUID, quotation_id: UUID, version: int
    ) -> DocumentSource | None:
        """The version's content, only if an approve decision exists for exactly this version
        and its content hash, and the version is still the quotation's current one."""
        ...

    def save_document(self, tenant_id: UUID, document: StoredDocument) -> StoredDocument:
        """Insert once; a repeat returns the row stored first."""
        ...

    def document(self, tenant_id: UUID, document_id: UUID) -> StoredDocument | None: ...

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
        """Move the delivery to status if it is currently in one of `expected` (compare and
        set). Returns False if it was not, so two workers can never both send it.

        Reaching available/sent also queues the expire_quotation job for the document's
        valid_until, in the same transaction (M6)."""
        ...


class DocumentReader(Protocol):
    """API-side reads for the portal and for customers on pull channels."""

    def source(self, tenant_id: UUID, quotation_id: UUID, version: int) -> DocumentSource | None:
        """Any stored version (for previews); approved_at is set if this version is approved."""
        ...

    def approved_document(self, tenant_id: UUID, quotation_id: UUID) -> StoredDocument | None:
        """The latest approved version's document, if it has been rendered."""
        ...

    def deliveries(self, tenant_id: UUID, quotation_id: UUID) -> list[DeliveryRecord]: ...

    def offered(
        self, tenant_id: UUID, enquiry_id: UUID, channel: str
    ) -> tuple[DeliveryRecord, StoredDocument, dict[str, object]] | None:
        """The document offered to the customer for an enquiry on this channel, with the
        version content, or None if nothing has been delivered there yet."""
        ...

    def record_download(self, tenant_id: UUID, delivery_id: UUID, at: datetime) -> None:
        """Remember the first time the customer fetched the document (later ones are ignored)."""
        ...
