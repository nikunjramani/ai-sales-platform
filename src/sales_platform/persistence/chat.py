"""Hosted chat rows (phase 4), API role.

Before a visitor is known, the tenant comes from what they present, never from the body:
- a public handle: the connection_handle_lookup policy shows only that connection;
- a session ID: the customer_session_lookup policy shows only that session.
After that, every read and write runs under that tenant's context as usual.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection as DbConnection
from sqlalchemy import Engine, and_, func, insert, select, text, update

from sales_platform.domain.contacts import ClaimedDetails
from sales_platform.domain.conversations import Direction
from sales_platform.domain.documents import StoredDocument
from sales_platform.domain.messages import MessageStatus
from sales_platform.domain.tenancy import TenantStatus
from sales_platform.persistence.connections import _COLUMNS as CONNECTION_COLUMNS
from sales_platform.persistence.connections import _connection
from sales_platform.persistence.documents import _DOCUMENT_COLUMNS, _document
from sales_platform.persistence.models import (
    ConnectionRow,
    Contact,
    ContactIdentity,
    Conversation,
    ConversationMessage,
    CustomerActionRef,
    CustomerSession,
    OutboundMessageRow,
    QuotationDocument,
    Tenant,
)
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.chat import ChatEndpoint, ChatSession, StoredSession, ThreadMessage

# Our messages are shown once handed over (sent), not while pending or after a failure.
_VISIBLE = [MessageStatus.SENT.value, MessageStatus.DELIVERED.value]


def _set(connection: DbConnection, name: str, value: str) -> None:
    connection.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})


class SqlChatStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def endpoint(self, handle: str) -> ChatEndpoint | None:
        with self._engine.begin() as connection:
            _set(connection, "app.public_handle", handle)
            row = connection.execute(
                select(*CONNECTION_COLUMNS).where(ConnectionRow.public_handle == handle)
            ).one_or_none()
            if row is None:
                return None
            _set(connection, "app.tenant_id", str(row.tenant_id))
            tenant = connection.execute(
                select(Tenant.name, Tenant.status).where(Tenant.id == row.tenant_id)
            ).one()
        return ChatEndpoint(_connection(row), tenant.name, TenantStatus(tenant.status))

    def start(
        self,
        endpoint: ChatEndpoint,
        details: ClaimedDetails,
        secret_sha256: str,
        ip_hmac: str,
        origin: str | None,
        expires_at: Any,
    ) -> ChatSession:
        conn = endpoint.connection
        tenant_id = conn.tenant_id
        contact_id, identity_id, conversation_id, session_id = uuid4(), uuid4(), uuid4(), uuid4()
        external_id = f"v_{uuid4()}"
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                insert(Contact).values(
                    id=contact_id,
                    tenant_id=tenant_id,
                    display_name=details.name,
                    email=details.email,  # claimed, not verified (Q2)
                    phone=details.phone,
                )
            )
            connection.execute(
                insert(ContactIdentity).values(
                    id=identity_id,
                    tenant_id=tenant_id,
                    contact_id=contact_id,
                    connection_ref=conn.connection_id,
                    external_id=external_id,
                    display_hint=details.name,
                    verified=True,  # the session proves the visitor ID, nothing more
                )
            )
            connection.execute(
                insert(Conversation).values(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    contact_id=contact_id,
                    identity_id=identity_id,
                    connection_ref=conn.connection_id,
                    last_message_at=func.now(),
                )
            )
            connection.execute(
                insert(CustomerSession).values(
                    id=session_id,
                    tenant_id=tenant_id,
                    connection_ref=conn.connection_id,
                    identity_id=identity_id,
                    conversation_id=conversation_id,
                    secret_sha256=secret_sha256,
                    ip_hmac=ip_hmac,
                    origin=origin,
                    expires_at=expires_at,
                )
            )
        return ChatSession(
            session_id,
            tenant_id,
            conn.connection_id,
            identity_id,
            conversation_id,
            external_id,
            expires_at,
        )

    def session(self, session_id: UUID) -> StoredSession | None:
        with self._engine.begin() as connection:
            _set(connection, "app.customer_session_id", str(session_id))
            row = connection.execute(
                select(
                    CustomerSession.tenant_id,
                    CustomerSession.connection_ref,
                    CustomerSession.identity_id,
                    CustomerSession.conversation_id,
                    CustomerSession.secret_sha256,
                    CustomerSession.expires_at,
                    CustomerSession.revoked_at,
                ).where(CustomerSession.id == session_id)
            ).one_or_none()
            if row is None:
                return None
            _set(connection, "app.tenant_id", str(row.tenant_id))
            external_id = connection.execute(
                select(ContactIdentity.external_id).where(
                    ContactIdentity.tenant_id == row.tenant_id,
                    ContactIdentity.id == row.identity_id,
                )
            ).scalar_one()
        return StoredSession(
            ChatSession(
                session_id,
                row.tenant_id,
                row.connection_ref,
                row.identity_id,
                row.conversation_id,
                external_id,
                row.expires_at,
            ),
            row.secret_sha256,
            row.revoked_at is not None,
        )

    def touch(self, session: ChatSession, expires_at: Any) -> None:
        with tenant_transaction(self._engine, session.tenant_id) as connection:
            connection.execute(
                update(CustomerSession)
                .where(
                    CustomerSession.tenant_id == session.tenant_id,
                    CustomerSession.id == session.session_id,
                )
                .values(last_seen_at=func.now(), expires_at=expires_at)
            )

    def revoke(self, session: ChatSession) -> None:
        with tenant_transaction(self._engine, session.tenant_id) as connection:
            connection.execute(
                update(CustomerSession)
                .where(
                    CustomerSession.tenant_id == session.tenant_id,
                    CustomerSession.id == session.session_id,
                    CustomerSession.revoked_at.is_(None),
                )
                .values(revoked_at=func.now())
            )

    def thread(self, session: ChatSession, after_seq: int, limit: int) -> list[ThreadMessage]:
        message = ConversationMessage
        outbound = OutboundMessageRow
        ref = CustomerActionRef
        query = (
            select(
                message.seq,
                message.direction,
                message.template_key,
                message.text,
                message.at,
                message.outbound_message_id,
                outbound.metadata_.label("message_metadata"),
                outbound.document_id,
                ref.id.label("ref_id"),
                and_(ref.consumed_at.is_(None), ref.expires_at > func.now()).label("ref_open"),
            )
            .outerjoin(
                outbound,
                and_(
                    outbound.tenant_id == message.tenant_id,
                    outbound.id == message.outbound_message_id,
                ),
            )
            .outerjoin(
                ref,
                and_(ref.tenant_id == message.tenant_id, ref.outbound_message_id == outbound.id),
            )
            .where(
                message.tenant_id == session.tenant_id,
                message.conversation_id == session.conversation_id,
                message.seq > after_seq,
                (message.direction == Direction.INBOUND.value) | outbound.status.in_(_VISIBLE),
            )
            .order_by(message.seq)
            .limit(limit)
        )
        with tenant_transaction(self._engine, session.tenant_id) as connection:
            rows = connection.execute(query).all()
        return [
            ThreadMessage(
                seq=row.seq,
                direction=row.direction,
                template_key=row.template_key,
                text=row.text,
                at=row.at,
                outbound_message_id=row.outbound_message_id,
                metadata=dict(row.message_metadata or {}),
                document_id=row.document_id,
                action_ref=row.ref_id,
                action_ref_open=bool(row.ref_open),
            )
            for row in rows
        ]

    def document(self, session: ChatSession, outbound_message_id: UUID) -> StoredDocument | None:
        with tenant_transaction(self._engine, session.tenant_id) as connection:
            row = connection.execute(
                select(*_DOCUMENT_COLUMNS)
                .join(
                    OutboundMessageRow,
                    and_(
                        OutboundMessageRow.tenant_id == QuotationDocument.tenant_id,
                        OutboundMessageRow.document_id == QuotationDocument.id,
                    ),
                )
                .join(
                    ConversationMessage,
                    and_(
                        ConversationMessage.tenant_id == OutboundMessageRow.tenant_id,
                        ConversationMessage.outbound_message_id == OutboundMessageRow.id,
                    ),
                )
                .where(
                    OutboundMessageRow.tenant_id == session.tenant_id,
                    OutboundMessageRow.id == outbound_message_id,
                    OutboundMessageRow.status.in_(_VISIBLE),
                    ConversationMessage.conversation_id == session.conversation_id,
                )
            ).one_or_none()
        return None if row is None else _document(row)
