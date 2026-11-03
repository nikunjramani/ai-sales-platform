"""Conversation rows inside a caller's transaction (phase 4).

Lock order, everywhere: the conversation row first, then its enquiries. A message from a
customer locks its conversation before looking at the open enquiries; the worker, when it
records an outcome that sends something, locks the conversation before the enquiry too. So the
API and the worker never wait for each other in opposite orders (no deadlock).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.conversations.domain.conversations import Author, Direction
from sales_platform.conversations.persistence.tables import (
    Contact,
    ContactIdentity,
    Conversation,
    ConversationMessage,
)

MAX_DISPLAY_NAME = 200


@dataclass(frozen=True)
class ConversationRef:
    conversation_id: UUID
    contact_id: UUID
    identity_id: UUID


def ensure_conversation(
    connection: Connection,
    tenant_id: UUID,
    connection_ref: UUID,
    external_id: str,
    verified: bool,
    display_name: str | None,
    at: datetime,
) -> ConversationRef:
    """Find or create the identity, its contact and its conversation, and lock the conversation.

    An advisory lock on (connection, sender) makes two first messages of a new customer wait
    for each other, so exactly one contact is created.
    """
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"identity:{tenant_id}:{connection_ref}:{external_id}"},
    )
    identity = connection.execute(
        select(ContactIdentity.id, ContactIdentity.contact_id).where(
            ContactIdentity.tenant_id == tenant_id,
            ContactIdentity.connection_ref == connection_ref,
            ContactIdentity.external_id == external_id,
        )
    ).one_or_none()
    if identity is None:
        contact_id, identity_id = uuid4(), uuid4()
        connection.execute(
            insert(Contact).values(
                id=contact_id,
                tenant_id=tenant_id,
                display_name=(display_name or external_id)[:MAX_DISPLAY_NAME],
            )
        )
        connection.execute(
            insert(ContactIdentity).values(
                id=identity_id,
                tenant_id=tenant_id,
                contact_id=contact_id,
                connection_ref=connection_ref,
                external_id=external_id,
                verified=verified,
                first_seen_at=at,
                last_seen_at=at,
            )
        )
    else:
        identity_id, contact_id = identity.id, identity.contact_id
        connection.execute(
            update(ContactIdentity)
            .where(ContactIdentity.tenant_id == tenant_id, ContactIdentity.id == identity_id)
            .values(last_seen_at=func.greatest(ContactIdentity.last_seen_at, at))
        )
    connection.execute(
        insert(Conversation)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            contact_id=contact_id,
            identity_id=identity_id,
            connection_ref=connection_ref,
            last_message_at=at,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "identity_id"])
    )
    conversation_id: UUID = connection.execute(
        select(Conversation.id)
        .where(Conversation.tenant_id == tenant_id, Conversation.identity_id == identity_id)
        .with_for_update()
    ).scalar_one()
    return ConversationRef(conversation_id, contact_id, identity_id)


def append_inbound(
    connection: Connection,
    tenant_id: UUID,
    conversation_id: UUID | None,
    enquiry_id: UUID,
    inbox_event_id: UUID,
    body: str,
    attachment_ids: Sequence[UUID],
    at: datetime,
) -> None:
    connection.execute(
        insert(ConversationMessage).values(
            id=uuid4(),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            enquiry_id=enquiry_id,
            direction=Direction.INBOUND.value,
            author=Author.CUSTOMER.value,
            text=body,
            inbox_event_id=inbox_event_id,
            attachment_ids=[str(item) for item in attachment_ids],
            at=at,
        )
    )
    if conversation_id is not None:
        connection.execute(
            update(Conversation)
            .where(Conversation.tenant_id == tenant_id, Conversation.id == conversation_id)
            .values(
                last_message_at=func.greatest(Conversation.last_message_at, at),
                last_inbound_at=func.greatest(func.coalesce(Conversation.last_inbound_at, at), at),
            )
        )


def append_outbound(
    connection: Connection,
    tenant_id: UUID,
    conversation_id: UUID,
    enquiry_id: UUID | None,
    outbound_message_id: UUID,
    template_key: str,
    body: str,
) -> None:
    """Our message in the thread, written with its outbox row (same transaction)."""
    connection.execute(
        insert(ConversationMessage).values(
            id=uuid4(),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            enquiry_id=enquiry_id,
            direction=Direction.OUTBOUND.value,
            author=Author.SYSTEM.value,
            text=body,
            template_key=template_key,
            outbound_message_id=outbound_message_id,
            at=func.now(),
        )
    )
    connection.execute(
        update(Conversation)
        .where(Conversation.tenant_id == tenant_id, Conversation.id == conversation_id)
        .values(last_message_at=func.now())
    )
