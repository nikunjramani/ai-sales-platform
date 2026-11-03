"""Provider callbacks stored in inbox_events and routed by the process_inbound job (ADR 0018).

The API role records events (and their jobs); the worker loads and marks them processed. The
dedupe key is (tenant, connection, provider event ID, event type), so a provider retrying the
same callback stores nothing new, while the same ID with another event type is a new event.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.domain.connections import Connection
from sales_platform.domain.intake import (
    InboundEvent,
    event_from_json,
    event_to_json,
    event_type,
)
from sales_platform.domain.jobs import JobKind, JobStatus
from sales_platform.persistence.connections import SqlConnectionStore
from sales_platform.persistence.intake import MAX_JOB_ATTEMPTS
from sales_platform.persistence.models import InboxEvent, Job
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.inbound import RecordedEvent, StoredEvent


def _fingerprint(data: dict[str, object]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SqlInboundStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connections = SqlConnectionStore(engine)

    def record(
        self,
        connection: Connection,
        events: Sequence[InboundEvent],
        received_at: datetime,
        *,
        queue_jobs: bool,
    ) -> list[RecordedEvent]:
        tenant_id = connection.tenant_id
        recorded: list[RecordedEvent] = []
        with tenant_transaction(self._engine, tenant_id) as db:
            for event in events:
                envelope = event_to_json(event)
                kind = event_type(event).value
                correlation_id = uuid4()
                inserted = db.execute(
                    insert(InboxEvent)
                    .values(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        channel=connection.provider_key,
                        connection_id=connection.legacy_key[:100],
                        connection_ref=connection.connection_id,
                        event_type=kind,
                        provider_event_id=event.provider_event_id,
                        payload_sha256=_fingerprint(envelope),
                        envelope=envelope,
                        received_at=received_at,
                        correlation_id=correlation_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            "tenant_id",
                            "connection_ref",
                            "provider_event_id",
                            "event_type",
                        ]
                    )
                    .returning(InboxEvent.id)
                ).scalar_one_or_none()
                if inserted is None:
                    existing = db.execute(
                        select(InboxEvent.id).where(
                            InboxEvent.tenant_id == tenant_id,
                            InboxEvent.connection_ref == connection.connection_id,
                            InboxEvent.provider_event_id == event.provider_event_id,
                            InboxEvent.event_type == kind,
                        )
                    ).scalar_one()
                    recorded.append(RecordedEvent(existing, new=False))
                    continue
                if queue_jobs:
                    db.execute(
                        insert(Job).values(
                            id=uuid4(),
                            tenant_id=tenant_id,
                            kind=JobKind.PROCESS_INBOUND.value,
                            payload={"inbox_event_id": str(inserted)},
                            status=JobStatus.PENDING.value,
                            max_attempts=MAX_JOB_ATTEMPTS,
                            run_at=func.now(),
                            correlation_id=correlation_id,
                        )
                    )
                recorded.append(RecordedEvent(inserted, new=True))
        return recorded

    def load(self, tenant_id: UUID, inbox_event_id: UUID) -> StoredEvent | None:
        with tenant_transaction(self._engine, tenant_id) as db:
            row = db.execute(
                select(
                    InboxEvent.connection_ref,
                    InboxEvent.envelope,
                    InboxEvent.processed_at,
                    InboxEvent.outcome,
                ).where(InboxEvent.tenant_id == tenant_id, InboxEvent.id == inbox_event_id)
            ).one_or_none()
        if row is None:
            return None
        connection = self._connections.get(tenant_id, row.connection_ref)
        if connection is None:
            return None
        return StoredEvent(
            inbox_event_id=inbox_event_id,
            connection=connection,
            event=event_from_json(dict(row.envelope)),
            processed_at=row.processed_at,
            outcome=row.outcome,
        )

    def mark_processed(self, tenant_id: UUID, inbox_event_id: UUID, outcome: str) -> None:
        with tenant_transaction(self._engine, tenant_id) as db:
            db.execute(
                update(InboxEvent)
                .where(
                    InboxEvent.tenant_id == tenant_id,
                    InboxEvent.id == inbox_event_id,
                    InboxEvent.processed_at.is_(None),
                )
                .values(processed_at=func.now(), outcome=outcome[:300])
            )
