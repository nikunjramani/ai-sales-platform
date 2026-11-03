"""Quotation reading, revision and workflow storage contracts."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.domain.pricing import RequestedLine
from sales_platform.domain.quotations import QuotationStatus, RecordedDecision, TaskStatus


@dataclass(frozen=True)
class VersionView:
    version_id: UUID
    version: int
    content: Mapping[str, object]
    content_sha256: str
    created_by: str
    reason: str | None
    created_at: datetime


@dataclass(frozen=True)
class TaskView:
    task_id: UUID
    version: int
    content_sha256: str
    status: TaskStatus
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class QuotationSummary:
    quotation_id: UUID
    enquiry_id: UUID
    number: str
    status: QuotationStatus
    current_version: int | None
    customer_reference: str | None
    currency: str | None
    total: str | None
    updated_at: datetime


@dataclass(frozen=True)
class QuotationDetail:
    summary: QuotationSummary
    enquiry_text: str
    pricing_issues: Sequence[Mapping[str, object]]
    versions: tuple[VersionView, ...]  # oldest first
    tasks: tuple[TaskView, ...]
    decisions: tuple[RecordedDecision, ...]

    @property
    def current(self) -> VersionView | None:
        return self.versions[-1] if self.versions else None

    @property
    def pending_task(self) -> TaskView | None:
        return next((task for task in self.tasks if task.status is TaskStatus.PENDING), None)


class QuotationReader(Protocol):
    def list(
        self, tenant_id: UUID, statuses: frozenset[QuotationStatus] | None, limit: int
    ) -> list[QuotationSummary]: ...

    def get(self, tenant_id: UUID, quotation_id: UUID) -> QuotationDetail | None: ...


class QuotationRevisionStore(Protocol):
    def find_revision(self, tenant_id: UUID, command_id: UUID) -> tuple[UUID, int] | None:
        """(quotation_id, version) already created by this command, for idempotent replays."""
        ...

    def add_revision(
        self,
        tenant_id: UUID,
        quotation_id: UUID,
        base_version: int | None,
        content: Mapping[str, object],
        content_sha256: str,
        created_by: str,
        reason: str | None,
        command_id: UUID,
    ) -> int:
        """Store version base+1 and queue the workflow resume, atomically.

        Raises RevisionConflict if the quotation's current version or status changed meanwhile.
        """
        ...


class QuotationWorkflowStore(Protocol):
    """Worker-side storage for the quotation workflow. Every method is safe to repeat."""

    def start_run(self, tenant_id: UUID, enquiry_id: UUID, graph_version: str) -> UUID: ...

    def extraction_lines(self, tenant_id: UUID, enquiry_id: UUID) -> list[RequestedLine] | None:
        """Lines from the finished extraction run, or None if the enquiry is not ready."""
        ...

    def create_quotation(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        content: Mapping[str, object] | None,
        content_sha256: str | None,
        pricing_issues: Sequence[Mapping[str, object]] | None,
        created_by: str,
    ) -> tuple[UUID, int | None]:
        """Create the quotation (with version 1 when priced) once; return (id, current version).

        A repeat returns what the first call stored, even if prices changed in between.
        """
        ...

    def current_version(self, tenant_id: UUID, quotation_id: UUID) -> int | None: ...

    def request_approval(self, tenant_id: UUID, quotation_id: UUID, version: int) -> UUID:
        """Create the approval task for this version once, with its notification job."""
        ...

    def decision(self, tenant_id: UUID, decision_id: UUID) -> RecordedDecision | None: ...

    def queue_delivery(self, tenant_id: UUID, quotation_id: UUID, version: int) -> bool:
        """Queue delivery of the approved version to the customer (outbox row + job), once.

        Returns False if this version is not the quotation's approved current version.
        """
        ...

    def finish(self, tenant_id: UUID, run_id: UUID, status: str) -> None: ...

    def lock_run(self, run_id: UUID) -> "RunLock": ...


class RunLock(Protocol):
    """Serialises work on one workflow run across worker processes."""

    def __enter__(self) -> bool:
        """True if this process now holds the lock."""
        ...

    def __exit__(self, *exc: object) -> None: ...
