"""Quotation deliveries follow the outbox (ADR 0026; formerly inside the outbox store).

A message that carries a quotation delivery mirrors every status change onto that delivery in
the same transaction, so the portal's delivery list and the customer's view keep working
exactly as before; reaching available/sent also queues the expiry job, as deliveries did.
"""

from uuid import UUID

from sqlalchemy import Connection, func, select, update

from sales_platform.integrations.domain.messages import MessageStatus
from sales_platform.integrations.ports.messages import NewMessage
from sales_platform.quotations.domain.documents import OFFERED_STATUSES, DeliveryStatus
from sales_platform.quotations.persistence.documents import queue_expiry
from sales_platform.quotations.persistence.tables import QuotationDelivery

# What a customer delivery shows for each message status ("delivered" is still "sent").
_DELIVERY_STATUS = {
    MessageStatus.PENDING: DeliveryStatus.PENDING,
    MessageStatus.SENDING: DeliveryStatus.SENDING,
    MessageStatus.AVAILABLE: DeliveryStatus.AVAILABLE,
    MessageStatus.SENT: DeliveryStatus.SENT,
    MessageStatus.DELIVERED: DeliveryStatus.SENT,
    MessageStatus.FAILED: DeliveryStatus.FAILED,
    MessageStatus.UNKNOWN: DeliveryStatus.UNKNOWN,
}


class DeliveryOutboxListener:
    """OutboxListener: a quotation delivery is carried out by its outbound message."""

    def queued(self, connection: Connection, message_id: UUID, message: NewMessage) -> None:
        links = message.links
        if links.delivery_id is not None:
            connection.execute(
                update(QuotationDelivery)
                .where(
                    QuotationDelivery.tenant_id == message.tenant_id,
                    QuotationDelivery.id == links.delivery_id,
                )
                .values(outbound_message_id=message_id, document_id=links.document_id)
            )

    def resent(
        self,
        connection: Connection,
        tenant_id: UUID,
        original_id: UUID,
        copy_id: UUID,
        enquiry_id: UUID | None,
        template_key: str,
        body_text: str,
    ) -> None:
        connection.execute(
            update(QuotationDelivery)
            .where(
                QuotationDelivery.tenant_id == tenant_id,
                QuotationDelivery.outbound_message_id == original_id,
            )
            .values(outbound_message_id=copy_id)
        )

    def status_changed(
        self,
        connection: Connection,
        tenant_id: UUID,
        message_id: UUID,
        status: MessageStatus,
        provider_message_id: str | None,
        error: str | None,
    ) -> None:
        """Copy a message's new status onto the delivery it carries out (if any)."""
        delivery = connection.execute(
            select(QuotationDelivery.id, QuotationDelivery.status).where(
                QuotationDelivery.tenant_id == tenant_id,
                QuotationDelivery.outbound_message_id == message_id,
            )
        ).one_or_none()
        if delivery is None:
            return
        target = _DELIVERY_STATUS[status]
        values: dict[str, object] = {
            "status": target.value,
            "last_error": error,
            "updated_at": func.now(),
        }
        if provider_message_id is not None:
            values["provider_reference"] = provider_message_id
        newly_offered = target in OFFERED_STATUSES and DeliveryStatus(delivery.status) not in (
            OFFERED_STATUSES
        )
        if newly_offered:
            values["delivered_at"] = func.now()
        connection.execute(
            update(QuotationDelivery)
            .where(QuotationDelivery.tenant_id == tenant_id, QuotationDelivery.id == delivery.id)
            .values(**values)
        )
        if newly_offered:
            queue_expiry(connection, tenant_id, delivery.id)
