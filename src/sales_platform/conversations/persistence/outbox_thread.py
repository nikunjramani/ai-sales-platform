"""The conversation thread follows the outbox (ADR 0026): every message we send to a customer
is written into their conversation in the same transaction as its outbox row."""

from uuid import UUID

from sqlalchemy import Connection, select

from sales_platform.conversations.persistence.conversations import append_outbound
from sales_platform.conversations.persistence.tables import ConversationMessage
from sales_platform.integrations.domain.messages import MessageStatus
from sales_platform.integrations.ports.messages import NewMessage


class ConversationThreadListener:
    """OutboxListener: our messages in the customer's thread."""

    def queued(self, connection: Connection, message_id: UUID, message: NewMessage) -> None:
        links = message.links
        if links.conversation_id is not None:
            append_outbound(
                connection,
                message.tenant_id,
                links.conversation_id,
                links.enquiry_id,
                message_id,
                message.template_key.value,
                message.body_text,
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
        conversation_id = connection.execute(
            select(ConversationMessage.conversation_id).where(
                ConversationMessage.tenant_id == tenant_id,
                ConversationMessage.outbound_message_id == original_id,
            )
        ).scalar_one_or_none()
        if conversation_id is not None:
            append_outbound(
                connection, tenant_id, conversation_id, enquiry_id, copy_id, template_key, body_text
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
        """The thread reads the outbox's own status; nothing to copy."""
