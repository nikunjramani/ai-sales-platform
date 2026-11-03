"""Approval decision storage contract.

A decision is recorded inside one transaction that holds a lock on the approval task row, so
two simultaneous decisions (portal and channel, or two managers) produce exactly one result.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sales_platform.quotations.domain.quotations import (
    DecisionCommand,
    RecordedDecision,
    TaskStatus,
)


@dataclass(frozen=True)
class TaskState:
    """The locked task plus the facts needed to validate a decision against it."""

    task_id: UUID
    quotation_id: UUID
    version: int
    content_sha256: str
    status: TaskStatus
    current_version: int | None  # the quotation's current version, for staleness checks
    version_created_by: str
    decision: RecordedDecision | None


class DecisionUnit(Protocol):
    """An open transaction holding the task lock. Leaving the context commits."""

    @property
    def task(self) -> TaskState: ...

    def record(
        self,
        decision: RecordedDecision,
        command: DecisionCommand,
        consume_reference: UUID | None,
    ) -> None:
        """Insert the decision, close the task, update the quotation status, consume the
        channel action reference (if any) and queue the workflow resume job, all atomically."""
        ...


class ApprovalStore(Protocol):
    def find_by_command(self, tenant_id: UUID, command_id: UUID) -> RecordedDecision | None: ...

    def locked_task(
        self, tenant_id: UUID, task_id: UUID
    ) -> AbstractContextManager[DecisionUnit | None]:
        """Lock the task row for this transaction; yields None if not found in this tenant."""
        ...
