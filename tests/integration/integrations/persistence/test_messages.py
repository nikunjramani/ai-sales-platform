"""The outbox on PostgreSQL: idempotent enqueue with its job, compare-and-set, isolation, grants."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from sales_platform.composition import build_outbox
from sales_platform.integrations.domain.connections import Purpose
from sales_platform.integrations.domain.messages import MessageStatus, TemplateKey
from sales_platform.integrations.persistence.connections import SqlConnectionStore
from sales_platform.integrations.ports.messages import MessageLinks, NewMessage
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.seed import INDUSTRIAL_TENANT, OFFICE_TENANT


def new_message(engine: Engine, key: str = "notice:test-1") -> NewMessage:
    connection = SqlConnectionStore(engine).find_by_account(
        INDUSTRIAL_TENANT, "simulated", "simulated-local"
    )
    assert connection is not None
    return NewMessage(
        tenant_id=INDUSTRIAL_TENANT,
        connection_id=connection.connection_id,
        provider_key="simulated",
        purpose=Purpose.MANAGER_APPROVALS,
        template_key=TemplateKey.RESPONSE_NOTICE,
        params={"response_id": str(uuid4())},
        recipient="phone",
        idempotency_key=key,
        body_text="Demo: the customer accepted quotation Q-1 version 1 (total 1.00 INR).",
        metadata={"notice_id": str(uuid4())},
        links=MessageLinks(),
        correlation_id=uuid4(),
    )


def jobs(owner_engine: Engine, message_id: UUID) -> int:
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as db:
        return int(
            db.execute(
                text("SELECT count(*) FROM jobs WHERE kind = 'send_message' "
                     "AND payload->>'message_id' = :id"),
                {"id": str(message_id)},
            ).scalar_one()
        )  # fmt: skip


def test_enqueue_is_idempotent_and_queues_one_send_job(
    worker_engine: Engine, owner_engine: Engine
) -> None:
    outbox = build_outbox(worker_engine)
    first = outbox.enqueue(new_message(worker_engine))
    assert outbox.enqueue(new_message(worker_engine)) == first
    assert jobs(owner_engine, first) == 1
    stored = outbox.get(INDUSTRIAL_TENANT, first)
    assert stored is not None and stored.status is MessageStatus.PENDING
    assert stored.metadata["notice_id"] and stored.template_key is TemplateKey.RESPONSE_NOTICE


def test_compare_and_set_lets_only_one_worker_send(worker_engine: Engine) -> None:
    outbox = build_outbox(worker_engine)
    message_id = outbox.enqueue(new_message(worker_engine))
    pending = frozenset({MessageStatus.PENDING})
    assert outbox.set_status(INDUSTRIAL_TENANT, message_id, MessageStatus.SENDING, expected=pending)
    assert not outbox.set_status(
        INDUSTRIAL_TENANT, message_id, MessageStatus.SENDING, expected=pending
    )
    sending = frozenset({MessageStatus.SENDING})
    assert outbox.set_status(
        INDUSTRIAL_TENANT, message_id, MessageStatus.SENT, expected=sending,
        provider_message_id="msg-1",
    )  # fmt: skip
    connection_id = new_message(worker_engine).connection_id
    assert outbox.apply_receipt(
        INDUSTRIAL_TENANT, connection_id, "msg-1", MessageStatus.DELIVERED, None
    )
    assert not outbox.apply_receipt(
        INDUSTRIAL_TENANT, connection_id, "unknown-id", MessageStatus.DELIVERED, None
    )
    delivered = outbox.get(INDUSTRIAL_TENANT, message_id)
    assert delivered is not None and delivered.status is MessageStatus.DELIVERED


def test_messages_are_isolated_and_the_api_cannot_write_them(
    worker_engine: Engine, runtime_engine: Engine
) -> None:
    outbox = build_outbox(worker_engine)
    message_id = outbox.enqueue(new_message(worker_engine))
    assert outbox.get(OFFICE_TENANT, message_id) is None
    assert build_outbox(runtime_engine).get(INDUSTRIAL_TENANT, message_id) is not None
    with (
        pytest.raises(ProgrammingError),
        tenant_transaction(runtime_engine, INDUSTRIAL_TENANT) as db,
    ):
        db.execute(text("UPDATE outbound_messages SET status = 'sent'"))
    # The worker may advance a message but never rewrite what it says or to whom.
    for column in ("body_text = 'x'", "recipient = 'x'", "params = '{}'"):
        with (
            pytest.raises(ProgrammingError),
            tenant_transaction(worker_engine, INDUSTRIAL_TENANT) as db,
        ):
            db.execute(text(f"UPDATE outbound_messages SET {column}"))


def test_the_api_resends_a_failed_customer_message_once(
    owner_engine: Engine, worker_engine: Engine, runtime_engine: Engine
) -> None:
    """Phase 5 Resend runs as the API role: a new pending copy with its own send job."""
    worker = build_outbox(worker_engine)
    base = new_message(worker_engine, f"customer:{uuid4()}")
    failed = worker.enqueue(NewMessage(**{**vars(base), "purpose": Purpose.CUSTOMER_MESSAGES}))
    pending = worker.enqueue(
        NewMessage(
            **{**vars(base), "purpose": Purpose.CUSTOMER_MESSAGES, "idempotency_key": "other"}
        )
    )
    assert worker.set_status(
        INDUSTRIAL_TENANT,
        failed,
        MessageStatus.FAILED,
        expected=frozenset({MessageStatus.PENDING}),
        error="Rejected by the provider: customer must message first",
    )
    api = build_outbox(runtime_engine)

    copy = api.resend(INDUSTRIAL_TENANT, failed, "user:operator")

    assert copy is not None and copy != failed
    assert api.resend(INDUSTRIAL_TENANT, failed, "user:operator") == copy
    assert api.resend(INDUSTRIAL_TENANT, pending, "user:operator") is None
    assert api.resend(OFFICE_TENANT, failed, "user:operator") is None
    resent = api.get(INDUSTRIAL_TENANT, copy)
    assert resent is not None and resent.status is MessageStatus.PENDING
    assert resent.params["resent_by"] == "user:operator"
    assert jobs(owner_engine, copy) == 1
