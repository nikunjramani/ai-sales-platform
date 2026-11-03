"""Moved here by ADR 0026 (module order)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.conversations.domain.conversations import Author, Direction
from sales_platform.conversations.persistence.conversations import ensure_conversation
from sales_platform.conversations.persistence.tables import Conversation, ConversationMessage
from sales_platform.enquiries.persistence.follow_ups import open_enquiries
from sales_platform.platform.persistence.tenancy import tenant_transaction


class SqlStaffMessages:
    """What the business typed itself on its own tool account (phase 5, e.g. a Telegram
    business chat): a staff row of the customer's conversation, attached to the open enquiry
    (the reader sees it as context, never as the customer's words). Worker role."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        external_id: str,
        inbox_event_id: UUID,
        body: str,
        at: datetime,
    ) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            conversation = ensure_conversation(
                connection, tenant_id, connection_id, external_id, True, None, at
            )
            candidates = open_enquiries(connection, tenant_id, conversation.conversation_id)
            enquiry_id = candidates[-1][0].enquiry_id if candidates else None
            inserted = connection.execute(
                insert(ConversationMessage)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    conversation_id=conversation.conversation_id,
                    enquiry_id=enquiry_id,
                    direction=Direction.INBOUND.value,
                    author=Author.STAFF.value,
                    text=body[:20000],
                    inbox_event_id=inbox_event_id,
                    at=at,
                )
                .on_conflict_do_nothing(index_elements=["inbox_event_id"])
                .returning(ConversationMessage.id)
            ).scalar_one_or_none()
            if inserted is not None:
                connection.execute(
                    update(Conversation)
                    .where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.id == conversation.conversation_id,
                    )
                    .values(last_message_at=func.greatest(Conversation.last_message_at, at))
                )
        return inserted is not None
