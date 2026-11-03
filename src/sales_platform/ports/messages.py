"""Outbox contracts (ADR 0018): storing outgoing messages and preparing them at send time."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.domain.connections import Purpose
from sales_platform.domain.messages import MessageStatus, OutboundMessage, TemplateKey
from sales_platform.ports.providers import Attachment, SendReceipt


@dataclass(frozen=True)
class MessageLinks:
    """What a message is about, for the portal and the audit trail."""

    document_id: UUID | None = None
    quotation_id: UUID | None = None
    version: int | None = None
    approval_task_id: UUID | None = None
    binding_id: UUID | None = None
    enquiry_id: UUID | None = None
    response_id: UUID | None = None
    delivery_id: UUID | None = None  # the quotation_deliveries row this message carries out
    # A message to a customer is also a row of their conversation (phase 4).
    conversation_id: UUID | None = None
    # A message to a manager about one approval request (phase 5): a reply to it finds it.
    action_ref_id: UUID | None = None


@dataclass(frozen=True)
class NewMessage:
    tenant_id: UUID
    connection_id: UUID
    provider_key: str
    purpose: Purpose
    template_key: TemplateKey
    params: Mapping[str, str]
    recipient: str
    idempotency_key: str
    body_text: str
    metadata: Mapping[str, str]
    links: MessageLinks
    correlation_id: UUID


class MessageOutbox(Protocol):
    def enqueue(self, message: NewMessage) -> UUID:
        """Store the message and its send_message job in one transaction.

        The same idempotency key returns the first message's ID and queues nothing new.
        """
        ...

    def get(self, tenant_id: UUID, message_id: UUID) -> OutboundMessage | None: ...

    def set_status(
        self,
        tenant_id: UUID,
        message_id: UUID,
        status: MessageStatus,
        *,
        expected: frozenset[MessageStatus],
        provider_message_id: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Compare-and-set: change only from an expected status; False if it was not.

        A message carrying a quotation delivery mirrors its status onto that delivery in the
        same transaction (and queues the expiry job when it becomes available or sent).
        """
        ...

    def resend(self, tenant_id: UUID, message_id: UUID, actor: str) -> UUID | None:
        """A copy of a failed customer message as a new pending message (with its send job,
        its conversation row and its quotation delivery pointed at it), once per failed
        message. None if it is not a failed customer message. Phase 5."""
        ...

    def apply_receipt(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        provider_message_id: str,
        status: MessageStatus,
        detail: str | None,
    ) -> bool:
        """A provider's later delivered/failed report; False if no such message."""
        ...


@dataclass(frozen=True)
class SendPreparation:
    """Extra fields a send hook adds just before sending, or why the message must not go."""

    cancel_reason: str | None = None  # the message becomes failed with this reason
    already_sent: bool = False  # nothing to do; the message becomes sent
    action_reference: str | None = None
    expires_at: datetime | None = None
    actions: tuple[str, ...] = ()
    attachment: Attachment | None = None
    context: Mapping[str, str] = field(default_factory=dict)  # passed back to after_send
    link: str | None = None  # a portal page for this message (phase 5: signed approval link)
    attachment_url: str | None = None  # a short-lived public link to the attachment


class SendHook(Protocol):
    """Template-specific work at send time (application code, never provider-specific)."""

    def before_send(self, message: OutboundMessage) -> SendPreparation: ...

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None: ...
