"""Sending through the outbox (ADR 0018).

OutboundService.queue renders a template (fixed wording) and stores the message with its
send_message job. SendMessageHandler then hands it to the connection's provider, with the same
honesty rules as quotation deliveries (ADR 0015):
1. Already finished? Do nothing. Left in 'sending' by a crashed attempt? We cannot know if it
   arrived: 'unknown' for a person to check, never resent automatically.
2. Connection gone, not active, or its provider not installed: 'failed' with the reason.
3. The template's send hook prepares what is created only at send time (an approval request's
   single-use action token, a quotation's PDF) or cancels the message (task already decided).
4. Compare-and-set pending -> sending, so only one worker hands it over.
5. The provider sends: accepted -> available/sent; refused -> failed; maybe sent -> unknown;
   any other error -> back to pending and retried (after the last attempt: failed).
6. The hook's after_send (e.g. mark the approval reference as sent).
7. Platform-webhook tools (phase 5) name keys that route a reply to this message back to its
   connection; they are stored so the reply finds its business.
"""

import logging
from collections.abc import Mapping
from uuid import UUID

from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection, DeliveryMode, Purpose
from sales_platform.integrations.domain.messages import (
    CUSTOMER_TEMPLATES,
    FINAL_MESSAGE_STATUSES,
    MANAGER_TEMPLATES,
    MessageStatus,
    OutboundMessage,
    Rendered,
    TemplateKey,
    render_template,
)
from sales_platform.integrations.ports.connections import ConnectionStore, RouteWriter
from sales_platform.integrations.ports.messages import (
    MessageLinks,
    MessageOutbox,
    NewMessage,
    SendHook,
    SendPreparation,
)
from sales_platform.integrations.ports.providers import (
    ReceiptKind,
    RenderedMessage,
    SendRejected,
    SendUncertain,
)
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.domain.tenancy import may_contact_customers
from sales_platform.platform.domain.text import clean_error
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.platform.ports.secrets import SecretStore
from sales_platform.platform.ports.tenancy import TenantStatusReader

logger = logging.getLogger(__name__)

_PENDING = frozenset({MessageStatus.PENDING})
_SENDING = frozenset({MessageStatus.SENDING})
_INTERRUPTED = "The worker stopped while handing the message over; check with the provider"
_PURPOSE = {
    TemplateKey.APPROVAL_REQUEST: Purpose.MANAGER_APPROVALS,
    TemplateKey.RESPONSE_NOTICE: Purpose.MANAGER_APPROVALS,
    TemplateKey.QUOTATION_DOCUMENT: Purpose.CUSTOMER_MESSAGES,
    **{template: Purpose.CUSTOMER_MESSAGES for template in CUSTOMER_TEMPLATES},
    **{template: Purpose.MANAGER_APPROVALS for template in MANAGER_TEMPLATES},
}


NOT_LIVE = "The business is not live yet; nothing is sent to customers"


class OutboundService:
    def __init__(self, outbox: MessageOutbox) -> None:
        self._outbox = outbox

    def queue(
        self,
        connection: Connection,
        *,
        template: TemplateKey,
        render: Mapping[str, object] | Rendered,
        params: Mapping[str, str],
        recipient: str,
        idempotency_key: str,
        links: MessageLinks,
        correlation_id: UUID,
    ) -> UUID:
        """Store one message (and its send job). The same idempotency key queues nothing new.

        `render` is what the template needs, or the text already rendered by the module that
        owns the message (quotations, ADR 0026); `params` are the IDs stored with the message.
        Neither may contain tokens or secrets.
        """
        rendered = render if isinstance(render, Rendered) else render_template(template, render)
        return self._outbox.enqueue(
            NewMessage(
                tenant_id=connection.tenant_id,
                connection_id=connection.connection_id,
                provider_key=connection.provider_key,
                purpose=_PURPOSE[template],
                template_key=template,
                params=dict(params),
                recipient=recipient,
                idempotency_key=idempotency_key,
                body_text=rendered.text,
                metadata=rendered.metadata,
                links=links,
                correlation_id=correlation_id,
            )
        )


class SendMessageHandler:
    """Job handler for send_message, payload {"message_id": "..."}."""

    def __init__(
        self,
        outbox: MessageOutbox,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        secrets: SecretStore,
        hooks: Mapping[TemplateKey, SendHook],
        statuses: TenantStatusReader | None = None,
        routes: RouteWriter | None = None,
    ) -> None:
        self._statuses = statuses
        self._routes = routes
        self._outbox = outbox
        self._connections = connections
        self._registry = registry
        self._secrets = secrets
        self._hooks = dict(hooks)

    def __call__(self, job: ClaimedJob) -> None:
        try:
            message_id = UUID(job.payload["message_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid message_id") from None
        tenant_id = job.require_tenant()
        message = self._outbox.get(tenant_id, message_id)
        if message is None:
            raise PermanentJobError("Message not found")
        if message.status in FINAL_MESSAGE_STATUSES:
            return
        if message.status is MessageStatus.SENDING:
            self._set(message, MessageStatus.UNKNOWN, _SENDING, error=_INTERRUPTED)
            return

        def fail(reason: str, expected: frozenset[MessageStatus] = _PENDING) -> None:
            logger.warning("Message %s failed: %s", message_id, reason)
            self._set(message, MessageStatus.FAILED, expected, error=clean_error(reason))

        connection = self._connections.get(tenant_id, message.connection_id)
        provider = None if connection is None else self._registry.find(connection.provider_key)
        if connection is None or not connection.active:
            fail(f"The {message.provider_key} connection is not active")
            return
        if provider is None:
            fail(f"No provider is installed for '{message.provider_key}'")
            return
        if self._not_live_for(message, provider.capabilities.delivery_mode):
            fail(NOT_LIVE)
            return
        hook = self._hooks.get(message.template_key)
        preparation = hook.before_send(message) if hook is not None else SendPreparation()
        if preparation.cancel_reason is not None:
            fail(preparation.cancel_reason)
            return
        if preparation.already_sent:
            self._set(message, MessageStatus.SENT, _PENDING)
            return
        if not self._set(message, MessageStatus.SENDING, _PENDING):
            return  # another worker took it over
        rendered = RenderedMessage(
            message_id=message.message_id,
            idempotency_key=message.idempotency_key,
            recipient=message.recipient,
            text=message.body_text,
            metadata=message.metadata,
            actions=preparation.actions,
            action_reference=preparation.action_reference,
            expires_at=preparation.expires_at,
            attachment=preparation.attachment,
            link=preparation.link,
            attachment_url=preparation.attachment_url,
            purpose=message.purpose,
        )
        try:
            receipt = provider.send(connection, self._secrets, rendered)
        except SendRejected as error:
            fail(f"Rejected by the provider: {error}", _SENDING)
            return
        except SendUncertain as error:
            self._set(
                message,
                MessageStatus.UNKNOWN,
                _SENDING,
                error=clean_error(f"Outcome unknown: {error}"),
            )
            return
        except Exception as error:
            last = job.attempt >= job.max_attempts
            self._set(
                message,
                MessageStatus.FAILED if last else MessageStatus.PENDING,
                _SENDING,
                error=clean_error(f"{type(error).__name__}: {error}"),
            )
            raise  # the job queue retries with backoff (or marks the job dead after the last)
        self._set(
            message,
            MessageStatus.AVAILABLE
            if receipt.kind is ReceiptKind.AVAILABLE
            else MessageStatus.SENT,
            _SENDING,
            provider_message_id=receipt.provider_message_id,
        )
        if receipt.routing_keys and self._routes is not None:
            self._routes.add_routes(
                tenant_id,
                connection.connection_id,
                connection.provider_key,
                dict.fromkeys(receipt.routing_keys, preparation.expires_at),
            )
        if hook is not None:
            hook.after_send(message, preparation, receipt)

    def _not_live_for(self, message: OutboundMessage, mode: DeliveryMode) -> bool:
        """Q11/D3: nothing is pushed to customers before Go live. Pull (the business's own API
        client, Q27) and messages to the business's managers still work while setting up."""
        if self._statuses is None or message.purpose is not Purpose.CUSTOMER_MESSAGES:
            return False
        if mode is DeliveryMode.PULL:
            return False
        return not may_contact_customers(self._statuses.status(message.tenant_id))

    def _set(
        self,
        message: OutboundMessage,
        status: MessageStatus,
        expected: frozenset[MessageStatus],
        *,
        provider_message_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        return self._outbox.set_status(
            message.tenant_id,
            message.message_id,
            status,
            expected=expected,
            provider_message_id=provider_message_id,
            error=error,
        )
