"""Moved here by ADR 0026 (module order)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sales_platform.conversations.application.customer_messages import CustomerMessenger
from sales_platform.conversations.domain.contacts import ConsentStatus
from sales_platform.conversations.ports.conversations import CustomerTargets, StaffMessages
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.domain.events import (
    ConsentChange,
    CustomerMessage,
    InboundEvent,
)
from sales_platform.integrations.domain.messages import TemplateKey


class ConsentRoute:
    """STOP / START from a customer (D15): update the identity's consent, then confirm.

    The confirmation is the only message an opted-out identity may still receive. Its key
    (consent:<event>) makes a retried job harmless.
    """

    def __init__(self, targets: CustomerTargets, messenger: CustomerMessenger) -> None:
        self._targets = targets
        self._messenger = messenger

    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        assert isinstance(event, ConsentChange)
        status = ConsentStatus.OPTED_IN if event.opted_in else ConsentStatus.OPTED_OUT
        tenant_id = connection.tenant_id
        self._targets.set_consent(
            tenant_id,
            connection.connection_id,
            event.sender_external_id,
            status,
            f"{connection.provider_key}:{event.provider_event_id}",
        )
        target = self._targets.for_identity(
            tenant_id, connection.connection_id, event.sender_external_id
        )
        if target is None:
            return "unknown sender"
        template = TemplateKey.OPTED_IN if event.opted_in else TemplateKey.OPTED_OUT
        self._messenger.send_to(
            tenant_id,
            target,
            template,
            {"business": target.business_name},
            f"consent:{connection.connection_id}:{event.provider_event_id}",
            uuid4(),
        )
        return status.value


class StaffMessageRoute:
    """What the business typed itself on its own tool account: a staff row of the customer's
    conversation (phase 5). Customer messages never come here: they are taken at once."""

    def __init__(self, staff: StaffMessages) -> None:
        self._staff = staff

    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        assert isinstance(event, CustomerMessage)
        if not event.from_business:
            return "unsupported"
        recorded = self._staff.record(
            connection.tenant_id,
            connection.connection_id,
            event.sender_external_id,
            inbox_event_id,
            event.text or "[no text]",
            event.occurred_at or datetime.now(UTC),
        )
        return "recorded" if recorded else "duplicate"
