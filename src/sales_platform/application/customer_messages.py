"""Messages to customers on the tool they wrote from (phase 4).

CustomerMessenger.send is the one place that decides whether a customer message is queued:
- the enquiry must have a conversation (API enquiries without customer_reference have none,
  Q5): otherwise there is nobody to write to, and nothing is queued;
- pull tools (the API) are skipped: the customer's own system reads the enquiry status and the
  quotation through the API, as before. Only push tools (website chat, later WhatsApp...) get
  messages;
- the message goes to the conversation's identity on the conversation's connection, through
  the outbox, with a fixed template and an idempotency key, so a retried job never repeats it.
Whether the customer may receive it (consent, D15) is checked at send time by
CustomerConsentHook: an opted-out identity's message becomes failed with the reason, never
silently dropped. Nothing is pushed before Go live (SendMessageHandler, phase 2).
"""

import logging
from collections.abc import Mapping
from uuid import UUID

from sales_platform.application.messaging import OutboundService
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.connections import DeliveryMode
from sales_platform.domain.contacts import may_receive
from sales_platform.domain.messages import OutboundMessage, TemplateKey
from sales_platform.ports.connections import ConnectionStore
from sales_platform.ports.conversations import CustomerTarget, CustomerTargets
from sales_platform.ports.messages import MessageLinks, SendHook, SendPreparation
from sales_platform.ports.providers import SendReceipt

logger = logging.getLogger(__name__)

OPTED_OUT_REASON = "The customer opted out of messages"


class CustomerMessenger:
    def __init__(
        self,
        targets: CustomerTargets,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        outbound: OutboundService,
    ) -> None:
        self._targets = targets
        self._connections = connections
        self._registry = registry
        self._outbound = outbound

    def push_target(self, tenant_id: UUID, enquiry_id: UUID) -> CustomerTarget | None:
        """The enquiry's conversation, if it is on a tool that pushes messages to customers."""
        target = self._targets.for_enquiry(tenant_id, enquiry_id)
        if target is None:
            return None
        connection = self._connections.get(tenant_id, target.connection_id)
        provider = None if connection is None else self._registry.find(connection.provider_key)
        if provider is None or provider.capabilities.delivery_mode is DeliveryMode.PULL:
            return None
        return target

    def acknowledge(self, tenant_id: UUID, enquiry_id: UUID, correlation_id: UUID) -> None:
        """ "Thanks, we are preparing your quotation." once per new enquiry, if the business
        wants it (D14, on by default)."""
        target = self._targets.for_enquiry(tenant_id, enquiry_id)
        if target is not None and target.acknowledge_enquiries:
            self.send(
                tenant_id,
                enquiry_id,
                TemplateKey.ENQUIRY_RECEIVED,
                {},
                f"ack:{enquiry_id}",
                correlation_id,
            )

    def send(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        template: TemplateKey,
        render: Mapping[str, object],
        idempotency_key: str,
        correlation_id: UUID,
        links: MessageLinks | None = None,
    ) -> UUID | None:
        """Queue one message; None when there is no push conversation to write to."""
        target = self._targets.for_enquiry(tenant_id, enquiry_id)
        if target is None:
            return None
        return self.send_to(
            tenant_id, target, template, render, idempotency_key, correlation_id, links, enquiry_id
        )

    def send_to(
        self,
        tenant_id: UUID,
        target: CustomerTarget,
        template: TemplateKey,
        render: Mapping[str, object],
        idempotency_key: str,
        correlation_id: UUID,
        links: MessageLinks | None = None,
        enquiry_id: UUID | None = None,
    ) -> UUID | None:
        """Queue one message to a known conversation (push tools only)."""
        connection = self._connections.get(tenant_id, target.connection_id)
        provider = None if connection is None else self._registry.find(connection.provider_key)
        if connection is None or provider is None:
            logger.warning(
                "Conversation %s: tool gone; %s not queued", target.conversation_id, template
            )
            return None
        if provider.capabilities.delivery_mode is DeliveryMode.PULL:
            return None
        extra = links or MessageLinks()
        return self._outbound.queue(
            connection,
            template=template,
            render=render,
            params={} if enquiry_id is None else {"enquiry_id": str(enquiry_id)},
            recipient=target.external_id,
            idempotency_key=idempotency_key,
            links=MessageLinks(
                document_id=extra.document_id,
                quotation_id=extra.quotation_id,
                version=extra.version,
                enquiry_id=enquiry_id,
                response_id=extra.response_id,
                delivery_id=extra.delivery_id,
                conversation_id=target.conversation_id,
            ),
            correlation_id=correlation_id,
        )


class CustomerConsentHook:
    """Send hook for customer templates: an opted-out identity gets nothing but the opt-out
    confirmation (D15). The message fails with the reason instead of vanishing. `inner` is the
    template's own hook (e.g. attaching the quotation PDF)."""

    def __init__(self, targets: CustomerTargets, inner: SendHook | None = None) -> None:
        self._targets = targets
        self._inner = inner

    def before_send(self, message: OutboundMessage) -> SendPreparation:
        consent = self._targets.consent(message.tenant_id, message.connection_id, message.recipient)
        if not may_receive(consent, message.template_key is TemplateKey.OPTED_OUT):
            return SendPreparation(cancel_reason=OPTED_OUT_REASON)
        return SendPreparation() if self._inner is None else self._inner.before_send(message)

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None:
        if self._inner is not None:
            self._inner.after_send(message, preparation, receipt)
