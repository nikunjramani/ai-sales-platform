"""Storage of provider callbacks (ADR 0018): recorded once, then routed by event type."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.domain.connections import Connection
from sales_platform.domain.intake import InboundEvent


@dataclass(frozen=True)
class RecordedEvent:
    inbox_event_id: UUID
    new: bool  # False: the same (connection, provider event ID, type) was already stored


@dataclass(frozen=True)
class StoredEvent:
    inbox_event_id: UUID
    connection: Connection
    event: InboundEvent
    processed_at: datetime | None
    outcome: str | None


class InboundStore(Protocol):
    def record(
        self,
        connection: Connection,
        events: Sequence[InboundEvent],
        received_at: datetime,
        *,
        queue_jobs: bool,
    ) -> list[RecordedEvent]:
        """Store each event once, in one transaction; with queue_jobs, add one
        process_inbound job per new event in the same transaction."""
        ...

    def load(self, tenant_id: UUID, inbox_event_id: UUID) -> StoredEvent | None: ...

    def mark_processed(self, tenant_id: UUID, inbox_event_id: UUID, outcome: str) -> None: ...
