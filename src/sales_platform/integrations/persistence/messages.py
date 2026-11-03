"""The outbox (ADR 0018): outbound_messages rows, written and advanced by the worker.

Other modules follow their messages through OutboxListener hooks, called in the outbox's own
transaction (ADR 0026): the conversation thread records each message we send, and a message
that carries a quotation delivery mirrors every status change onto that delivery (reaching
available/sent also queues the expiry job). The outbox itself writes only its own table.
"""

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, Row, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.integrations.domain.connections import Purpose
from sales_platform.integrations.domain.messages import MessageStatus, OutboundMessage, TemplateKey
from sales_platform.integrations.persistence.tables import OutboundMessageRow
from sales_platform.integrations.ports.messages import NewMessage
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.persistence.jobs import queue_job
from sales_platform.platform.persistence.tenancy import tenant_transaction

_SENT = frozenset({MessageStatus.AVAILABLE, MessageStatus.SENT})


class OutboxListener(Protocol):
    """A module that follows the messages it sends. Each call runs inside the outbox's
    transaction (`connection`), so the module's own rows change together with the message."""

    def queued(self, connection: Connection, message_id: UUID, message: NewMessage) -> None: ...

    def resent(
        self,
        connection: Connection,
        tenant_id: UUID,
        original_id: UUID,
        copy_id: UUID,
        enquiry_id: UUID | None,
        template_key: str,
        body_text: str,
    ) -> None: ...

    def status_changed(
        self,
        connection: Connection,
        tenant_id: UUID,
        message_id: UUID,
        status: MessageStatus,
        provider_message_id: str | None,
        error: str | None,
    ) -> None: ...


def _message(row: Row[Any]) -> OutboundMessage:
    m = row._mapping  # by column name ("metadata" is not a valid ORM attribute name)
    return OutboundMessage(
        message_id=m["id"],
        tenant_id=m["tenant_id"],
        connection_id=m["connection_ref"],
        provider_key=m["provider_key"],
        purpose=Purpose(m["purpose"]),
        template_key=TemplateKey(m["template_key"]),
        params=dict(m["params"]),
        recipient=m["recipient"],
        idempotency_key=m["idempotency_key"],
        body_text=m["body_text"],
        metadata=dict(m["metadata"]),
        status=MessageStatus(m["status"]),
        document_id=m["document_id"],
        quotation_id=m["quotation_id"],
        version=m["version"],
        approval_task_id=m["approval_task_id"],
        binding_id=m["binding_id"],
        enquiry_id=m["enquiry_id"],
        response_id=m["response_id"],
        correlation_id=m["correlation_id"],
        provider_message_id=m["provider_message_id"],
        last_error=m["last_error"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


class SqlMessageOutbox:
    def __init__(self, engine: Engine, listeners: Sequence[OutboxListener] = ()) -> None:
        self._engine = engine
        self._listeners = tuple(listeners)

    def enqueue(self, message: NewMessage) -> UUID:
        tenant_id = message.tenant_id
        links = message.links
        with tenant_transaction(self._engine, tenant_id) as connection:
            created = connection.execute(
                insert(OutboundMessageRow)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    connection_ref=message.connection_id,
                    provider_key=message.provider_key,
                    purpose=message.purpose.value,
                    template_key=message.template_key.value,
                    params=dict(message.params),
                    body_text=message.body_text,
                    metadata_=dict(message.metadata),
                    recipient=message.recipient,
                    idempotency_key=message.idempotency_key,
                    status=MessageStatus.PENDING.value,
                    document_id=links.document_id,
                    quotation_id=links.quotation_id,
                    version=links.version,
                    approval_task_id=links.approval_task_id,
                    binding_id=links.binding_id,
                    enquiry_id=links.enquiry_id,
                    response_id=links.response_id,
                    correlation_id=message.correlation_id,
                    action_ref_id=links.action_ref_id,
                )
                .on_conflict_do_nothing(constraint="uq_outbound_messages_idempotency")
                .returning(OutboundMessageRow.id)
            ).scalar_one_or_none()
            if created is None:
                existing: UUID = connection.execute(
                    select(OutboundMessageRow.id).where(
                        OutboundMessageRow.tenant_id == tenant_id,
                        OutboundMessageRow.idempotency_key == message.idempotency_key,
                    )
                ).scalar_one()
                return existing
            queue_job(
                connection,
                tenant_id,
                JobKind.SEND_MESSAGE,
                {"message_id": str(created)},
                message.correlation_id,
            )
            for listener in self._listeners:
                listener.queued(connection, created, message)
        return created

    def resend(self, tenant_id: UUID, message_id: UUID, actor: str) -> UUID | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*OutboundMessageRow.__table__.columns).where(
                    OutboundMessageRow.tenant_id == tenant_id,
                    OutboundMessageRow.id == message_id,
                    OutboundMessageRow.status == MessageStatus.FAILED.value,
                    OutboundMessageRow.purpose == Purpose.CUSTOMER_MESSAGES.value,
                )
            ).one_or_none()
            if row is None:
                return None
            created = connection.execute(
                insert(OutboundMessageRow)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    connection_ref=row.connection_ref,
                    provider_key=row.provider_key,
                    purpose=row.purpose,
                    template_key=row.template_key,
                    params={**row.params, "resent_by": actor[:200]},
                    body_text=row.body_text,
                    metadata_=dict(row.metadata),
                    recipient=row.recipient,
                    idempotency_key=f"resend:{message_id}",
                    status=MessageStatus.PENDING.value,
                    document_id=row.document_id,
                    quotation_id=row.quotation_id,
                    version=row.version,
                    approval_task_id=row.approval_task_id,
                    binding_id=row.binding_id,
                    enquiry_id=row.enquiry_id,
                    response_id=row.response_id,
                    correlation_id=row.correlation_id,
                    resent_from_id=message_id,
                )
                .on_conflict_do_nothing(constraint="uq_outbound_messages_idempotency")
                .returning(OutboundMessageRow.id)
            ).scalar_one_or_none()
            if created is None:  # already resent once: the same copy
                existing: UUID = connection.execute(
                    select(OutboundMessageRow.id).where(
                        OutboundMessageRow.tenant_id == tenant_id,
                        OutboundMessageRow.idempotency_key == f"resend:{message_id}",
                    )
                ).scalar_one()
                return existing
            queue_job(
                connection,
                tenant_id,
                JobKind.SEND_MESSAGE,
                {"message_id": str(created)},
                row.correlation_id,
            )
            for listener in self._listeners:
                listener.resent(
                    connection,
                    tenant_id,
                    message_id,
                    created,
                    row.enquiry_id,
                    row.template_key,
                    row.body_text,
                )
        return created

    def get(self, tenant_id: UUID, message_id: UUID) -> OutboundMessage | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*OutboundMessageRow.__table__.columns).where(
                    OutboundMessageRow.tenant_id == tenant_id, OutboundMessageRow.id == message_id
                )
            ).one_or_none()
        return None if row is None else _message(row)

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
        values: dict[str, object] = {
            "status": status.value,
            "last_error": error,
            "updated_at": func.now(),
        }
        if provider_message_id is not None:
            values["provider_message_id"] = provider_message_id
        if status in _SENT:
            values["sent_at"] = func.now()
        with tenant_transaction(self._engine, tenant_id) as connection:
            changed = connection.execute(
                update(OutboundMessageRow)
                .where(
                    OutboundMessageRow.tenant_id == tenant_id,
                    OutboundMessageRow.id == message_id,
                    OutboundMessageRow.status.in_([item.value for item in expected]),
                )
                .values(**values)
            ).rowcount
            if changed == 1:
                for listener in self._listeners:
                    listener.status_changed(
                        connection, tenant_id, message_id, status, provider_message_id, error
                    )
        return bool(changed == 1)

    def apply_receipt(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        provider_message_id: str,
        status: MessageStatus,
        detail: str | None,
    ) -> bool:
        if status not in {MessageStatus.DELIVERED, MessageStatus.FAILED}:
            raise ValueError("A receipt reports delivered or failed")
        values: dict[str, object] = {"status": status.value, "updated_at": func.now()}
        error: str | None = None
        if status is MessageStatus.DELIVERED:
            values["delivered_at"] = func.now()
        else:
            error = (detail or "The provider reported a failure")[:300]
            values["last_error"] = error
        with tenant_transaction(self._engine, tenant_id) as connection:
            message_id = connection.execute(
                update(OutboundMessageRow)
                .where(
                    OutboundMessageRow.tenant_id == tenant_id,
                    OutboundMessageRow.connection_ref == connection_id,
                    OutboundMessageRow.provider_message_id == provider_message_id,
                    OutboundMessageRow.status.in_(
                        [MessageStatus.SENT.value, MessageStatus.DELIVERED.value]
                    ),
                )
                .values(**values)
                .returning(OutboundMessageRow.id)
            ).scalar_one_or_none()
            if message_id is not None:
                for listener in self._listeners:
                    listener.status_changed(connection, tenant_id, message_id, status, None, error)
        return message_id is not None
