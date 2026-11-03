"""Job handler: render an approved quotation's PDF (once) and send it to the customer.

Flow for one delivery (a row written in the same transaction as the job that processes it):
1. Already finished, or already handed to the outbox? Do nothing (a repeat is harmless).
2. Find the connection the enquiry arrived on; the quotation goes back the same way. No active
   connection: the delivery fails with the reason (shown in the portal).
3. Get the document: reuse the stored one (hash re-checked) or render it from the approved
   version and store it. Only a version with an approve decision for its exact content hash
   can be rendered.
4. Queue one outbound message (template quotation_document, idempotency key
   quotation-delivery:<id>) linked to the delivery. The send_message job does the rest, and
   the delivery mirrors the message's status (pending, sending, available/sent, failed,
   unknown; ADR 0015 and 0018).
At send time QuotationDocumentHook loads and hash-checks the PDF and attaches it.
Problems that retrying cannot fix are recorded on the delivery (failed + reason) so they show
in the portal, instead of silently vanishing.
"""

import hashlib
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sales_platform.application.customer_messages import CustomerMessenger
from sales_platform.application.documents import read_verified, with_logo
from sales_platform.application.messaging import OutboundService
from sales_platform.domain.acceptance import valid_until
from sales_platform.domain.documents import (
    FINAL_DELIVERY_STATUSES,
    DeliveryStatus,
    DocumentIntegrityError,
    DocumentKind,
    DocumentNotAvailable,
    StoredDocument,
    UnrenderableContent,
    clean_error,
    document_filename,
    document_id,
    document_key,
)
from sales_platform.domain.jobs import PermanentJobError
from sales_platform.domain.messages import OutboundMessage, TemplateKey, format_valid_until
from sales_platform.domain.quotations import quotation_number
from sales_platform.ports.connections import ConnectionStore
from sales_platform.ports.documents import DeliveryStore, DeliveryWork, QuotationRenderer
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.ports.messages import MessageLinks, SendPreparation
from sales_platform.ports.providers import Attachment, SendReceipt
from sales_platform.ports.storage import ObjectNotFound, ObjectStorage

logger = logging.getLogger(__name__)

_PENDING = frozenset({DeliveryStatus.PENDING})


def utc_now() -> datetime:
    return datetime.now(UTC)


class QuotationDeliveryHandler:
    def __init__(
        self,
        store: DeliveryStore,
        storage: ObjectStorage,
        renderer: QuotationRenderer,
        connections: ConnectionStore,
        outbound: OutboundService,
        clock: Callable[[], datetime] = utc_now,
        customers: CustomerMessenger | None = None,
    ) -> None:
        self._customers = customers
        self._store = store
        self._storage = storage
        self._renderer = renderer
        self._connections = connections
        self._outbound = outbound
        self._clock = clock

    def __call__(self, job: ClaimedJob) -> None:
        try:
            delivery_id = UUID(job.payload["delivery_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid delivery_id") from None
        tenant_id = job.require_tenant()
        work = self._store.delivery(tenant_id, delivery_id)
        if work is None:
            raise PermanentJobError("Delivery not found")
        if work.record.status in FINAL_DELIVERY_STATUSES or work.outbound_message_id is not None:
            return

        def fail(reason: str) -> None:
            logger.warning("Delivery %s failed: %s", delivery_id, reason)
            self._store.set_status(
                tenant_id,
                delivery_id,
                DeliveryStatus.FAILED,
                expected=_PENDING,
                error=clean_error(reason),
            )

        connection = (
            None
            if work.connection_id is None
            else self._connections.get(tenant_id, work.connection_id)
        )
        if connection is None or not connection.active:
            fail(f"No active connection for '{work.record.channel}' to send the quotation")
            return
        try:
            document, _ = self._document(tenant_id, work)
        except (DocumentNotAvailable, UnrenderableContent, DocumentIntegrityError) as error:
            fail(str(error))
            return
        except ObjectNotFound:
            fail("The stored document file is missing")
            return
        record = work.record
        target = (
            None
            if self._customers is None
            else self._customers.push_target(tenant_id, work.enquiry_id)
        )
        if target is not None and target.connection_id == connection.connection_id:
            # A chat-like tool (phase 4): our fixed covering text, with the PDF attached at
            # send time and Accept/Decline offered by the send hook.
            source = self._store.approved_source(tenant_id, record.quotation_id, record.version)
            if source is None or document.valid_until is None:
                fail("This version is not approved or no longer current")
                return
            self._outbound.queue(
                connection,
                template=TemplateKey.QUOTATION_READY,
                render={
                    "number": quotation_number(record.quotation_id),
                    "version": record.version,
                    "total": source.content["total"],
                    "currency": source.content["currency"],
                    "date": format_valid_until(document.valid_until),
                },
                params={"delivery_id": str(delivery_id)},
                recipient=target.external_id,
                idempotency_key=f"quotation-delivery:{delivery_id}",
                links=MessageLinks(
                    document_id=document.document_id,
                    quotation_id=record.quotation_id,
                    version=record.version,
                    enquiry_id=work.enquiry_id,
                    delivery_id=delivery_id,
                    conversation_id=target.conversation_id,
                ),
                correlation_id=work.correlation_id,
            )
            return
        self._outbound.queue(
            connection,
            template=TemplateKey.QUOTATION_DOCUMENT,
            render={"quotation_id": record.quotation_id, "version": record.version},
            params={"delivery_id": str(delivery_id)},
            recipient=record.recipient,
            idempotency_key=f"quotation-delivery:{delivery_id}",
            links=MessageLinks(
                document_id=document.document_id,
                quotation_id=record.quotation_id,
                version=record.version,
                enquiry_id=work.enquiry_id,
                delivery_id=delivery_id,
            ),
            correlation_id=work.correlation_id,
        )

    def _document(self, tenant_id: UUID, work: DeliveryWork) -> tuple[StoredDocument, bytes]:
        """The stored approved document, rendering and storing it first if needed."""
        if work.document is not None:
            return work.document, read_verified(self._storage, work.document)
        record = work.record
        source = self._store.approved_source(tenant_id, record.quotation_id, record.version)
        if source is None:
            raise DocumentNotAvailable("This version is not approved or no longer current")
        # Validity counts from delivery (user decision): this is the moment the document is
        # prepared for it. A push retry later keeps this date, which is printed on the PDF.
        prepared_at = self._clock()
        until = valid_until(prepared_at, work.validity_days)
        source = replace(with_logo(self._storage, source), valid_until=until)
        data = self._renderer.render(source, preview=False)
        sha256 = hashlib.sha256(data).hexdigest()
        new_id = document_id(
            tenant_id, record.quotation_id, record.version, DocumentKind.APPROVED, sha256
        )
        key = document_key(tenant_id, new_id)
        self._storage.put(key, data)
        stored = self._store.save_document(
            tenant_id,
            StoredDocument(
                document_id=new_id,
                quotation_id=record.quotation_id,
                version=record.version,
                content_sha256=source.content_sha256,
                storage_key=key,
                sha256=sha256,
                size_bytes=len(data),
                renderer=self._renderer.renderer,
                created_at=prepared_at,
                valid_until=until,
            ),
        )
        if stored.document_id != new_id:
            # Another attempt stored a document first (e.g. an older renderer): use that one.
            self._storage.delete(key)
            return stored, read_verified(self._storage, stored)
        return stored, data


class QuotationDocumentHook:
    """Send hook for quotation_document: attach the stored PDF after re-checking its hash."""

    def __init__(self, store: DeliveryStore, storage: ObjectStorage) -> None:
        self._store = store
        self._storage = storage

    def before_send(self, message: OutboundMessage) -> SendPreparation:
        document = (
            None
            if message.document_id is None
            else self._store.document(message.tenant_id, message.document_id)
        )
        if document is None:
            return SendPreparation(cancel_reason="The quotation document is not available")
        try:
            data = read_verified(self._storage, document)
        except DocumentIntegrityError as error:
            return SendPreparation(cancel_reason=str(error))
        except ObjectNotFound:
            return SendPreparation(cancel_reason="The stored document file is missing")
        return SendPreparation(
            attachment=Attachment(
                filename=document_filename(document.quotation_id, document.version),
                content_type="application/pdf",
                data=data,
                sha256=document.sha256,
            )
        )

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None:
        return None
