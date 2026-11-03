"""Quotation, version and approval storage. Every query is tenant-scoped (filter + RLS).

- SqlQuotationWorkflowStore: worker role; drafting, approval tasks, delivery queueing, run locking.
- SqlApprovalStore, SqlQuotationReader, SqlRevisionStore: API role; decisions and revisions.
Versions are inserted, never updated. Business changes and the jobs they trigger are written
in the same transaction.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, Row, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.domain.jobs import JobKind
from sales_platform.domain.pricing import RequestedLine
from sales_platform.domain.quotations import (
    QUOTATION_STATUS_AFTER,
    REVISABLE,
    TASK_STATUS_AFTER,
    ApprovalAction,
    DecisionCommand,
    QuotationStatus,
    RecordedDecision,
    RevisionConflict,
    TaskStatus,
    quotation_number,
)
from sales_platform.persistence.documents import queue_delivery
from sales_platform.persistence.jobs import queue_job
from sales_platform.persistence.models import (
    ApprovalActionRef,
    ApprovalDecision,
    ApprovalTask,
    Enquiry,
    Quotation,
    QuotationVersion,
    WorkflowRun,
)
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.persistence.workflow import SqlWorkflowStore
from sales_platform.ports.approvals import TaskState
from sales_platform.ports.quotations import (
    QuotationDetail,
    QuotationSummary,
    TaskView,
    VersionView,
)

EXTRACTION_GRAPHS = "enquiry-extraction-%"
# The total exactly as stored in the version (policy decimals), not the numeric column.
_TOTAL = QuotationVersion.content["total"].astext.label("total")


def _decision(row: Row[Any]) -> RecordedDecision:
    return RecordedDecision(
        decision_id=row.id,
        command_id=row.command_id,
        task_id=row.task_id,
        quotation_id=row.quotation_id,
        version=row.version,
        action=ApprovalAction(row.action),
        actor_subject=row.actor_subject,
        reason=row.reason,
        transport=row.transport,
        decided_at=row.decided_at,
    )


_DECISION_COLUMNS = (
    ApprovalDecision.id,
    ApprovalDecision.command_id,
    ApprovalDecision.task_id,
    ApprovalDecision.quotation_id,
    ApprovalDecision.version,
    ApprovalDecision.action,
    ApprovalDecision.actor_subject,
    ApprovalDecision.reason,
    ApprovalDecision.transport,
    ApprovalDecision.decided_at,
)


def enquiry_link(connection: Connection, tenant_id: UUID, quotation_id: UUID) -> Row[Any]:
    """The quotation's enquiry ID and correlation ID, for the jobs a change queues."""
    return connection.execute(
        select(Quotation.enquiry_id, Enquiry.correlation_id)
        .join(
            Enquiry,
            (Enquiry.tenant_id == Quotation.tenant_id) & (Enquiry.id == Quotation.enquiry_id),
        )
        .where(Quotation.tenant_id == tenant_id, Quotation.id == quotation_id)
    ).one()


def version_values(
    tenant_id: UUID,
    quotation_id: UUID,
    version: int,
    content: Mapping[str, object],
    content_sha256: str,
    created_by: str,
    reason: str | None,
    command_id: UUID | None,
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "quotation_id": quotation_id,
        "version": version,
        "content": dict(content),
        "content_sha256": content_sha256,
        "currency": str(content["currency"]),
        "total": Decimal(str(content["total"])),
        "created_by": created_by,
        "reason": reason,
        "command_id": command_id,
    }


class SqlRunLock:
    """Session-level PostgreSQL advisory lock keyed by the run ID (held on its own connection)."""

    def __init__(self, engine: Engine, run_id: UUID) -> None:
        self._engine = engine
        self._key = int.from_bytes(run_id.bytes[:8], "big", signed=True)
        self._connection: Connection | None = None
        self._held = False

    def __enter__(self) -> bool:
        self._connection = self._engine.connect()
        self._held = bool(
            self._connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self._key}
            ).scalar_one()
        )
        self._connection.commit()  # session-level locks outlive the transaction
        return self._held

    def __exit__(self, *exc: object) -> None:
        if self._connection is None:
            return
        try:
            if self._held:
                self._connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                )
                self._connection.commit()
        finally:
            self._connection.close()
            self._connection = None


class SqlQuotationWorkflowStore:
    """Worker-role storage for the quotation approval workflow."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._runs = SqlWorkflowStore(engine)

    def start_run(self, tenant_id: UUID, enquiry_id: UUID, graph_version: str) -> UUID:
        return self._runs.start_run(tenant_id, enquiry_id, graph_version)

    def lock_run(self, run_id: UUID) -> SqlRunLock:
        return SqlRunLock(self._engine, run_id)

    def extraction_lines(self, tenant_id: UUID, enquiry_id: UUID) -> list[RequestedLine] | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            outcome = connection.execute(
                select(WorkflowRun.outcome)
                .where(
                    WorkflowRun.tenant_id == tenant_id,
                    WorkflowRun.enquiry_id == enquiry_id,
                    WorkflowRun.graph_version.like(EXTRACTION_GRAPHS),
                    WorkflowRun.status == "ready_for_quotation",
                )
                .order_by(WorkflowRun.updated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if outcome is None:
            return None
        lines = outcome.get("lines", [])
        assert isinstance(lines, list)
        return [
            RequestedLine(str(line["sku"]), Decimal(str(line["quantity"])), str(line["unit"]))
            for line in lines
        ]

    def create_quotation(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        content: Mapping[str, object] | None,
        content_sha256: str | None,
        pricing_issues: Sequence[Mapping[str, object]] | None,
        created_by: str,
    ) -> tuple[UUID, int | None]:
        priced = content is not None and content_sha256 is not None
        quotation_id = uuid4()
        with tenant_transaction(self._engine, tenant_id) as connection:
            inserted = connection.execute(
                insert(Quotation)
                .values(
                    id=quotation_id,
                    tenant_id=tenant_id,
                    enquiry_id=enquiry_id,
                    status=(
                        QuotationStatus.PENDING_APPROVAL
                        if priced
                        else QuotationStatus.PRICING_FAILED
                    ).value,
                    current_version=1 if priced else None,
                    pricing_issues=None if pricing_issues is None else list(pricing_issues),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "enquiry_id"])
            ).rowcount
            if inserted and content is not None and content_sha256 is not None:
                connection.execute(
                    insert(QuotationVersion).values(
                        **version_values(
                            tenant_id,
                            quotation_id,
                            1,
                            content,
                            content_sha256,
                            created_by,
                            None,
                            None,
                        )
                    )
                )
            row = connection.execute(
                select(Quotation.id, Quotation.current_version).where(
                    Quotation.tenant_id == tenant_id, Quotation.enquiry_id == enquiry_id
                )
            ).one()
        return row.id, row.current_version

    def current_version(self, tenant_id: UUID, quotation_id: UUID) -> int | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            value: int | None = connection.execute(
                select(Quotation.current_version).where(
                    Quotation.tenant_id == tenant_id, Quotation.id == quotation_id
                )
            ).scalar_one_or_none()
        return value

    def request_approval(self, tenant_id: UUID, quotation_id: UUID, version: int) -> UUID:
        with tenant_transaction(self._engine, tenant_id) as connection:
            stored = connection.execute(
                select(QuotationVersion.id, QuotationVersion.content_sha256).where(
                    QuotationVersion.tenant_id == tenant_id,
                    QuotationVersion.quotation_id == quotation_id,
                    QuotationVersion.version == version,
                )
            ).one()
            task_id = uuid4()
            created = connection.execute(
                insert(ApprovalTask)
                .values(
                    id=task_id,
                    tenant_id=tenant_id,
                    quotation_id=quotation_id,
                    version_id=stored.id,
                    version=version,
                    content_sha256=stored.content_sha256,
                    status=TaskStatus.PENDING.value,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "version_id"])
            ).rowcount
            if not created:
                existing: UUID = connection.execute(
                    select(ApprovalTask.id).where(
                        ApprovalTask.tenant_id == tenant_id, ApprovalTask.version_id == stored.id
                    )
                ).scalar_one()
                return existing
            connection.execute(
                update(Quotation)
                .where(Quotation.tenant_id == tenant_id, Quotation.id == quotation_id)
                .values(status=QuotationStatus.PENDING_APPROVAL.value, updated_at=func.now())
            )
            link = enquiry_link(connection, tenant_id, quotation_id)
            queue_job(
                connection,
                tenant_id,
                JobKind.NOTIFY_APPROVERS,
                {"task_id": str(task_id), "enquiry_id": str(link.enquiry_id)},
                link.correlation_id,
            )
        return task_id

    def decision(self, tenant_id: UUID, decision_id: UUID) -> RecordedDecision | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_DECISION_COLUMNS).where(
                    ApprovalDecision.tenant_id == tenant_id, ApprovalDecision.id == decision_id
                )
            ).one_or_none()
        return None if row is None else _decision(row)

    def queue_delivery(self, tenant_id: UUID, quotation_id: UUID, version: int) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return queue_delivery(connection, tenant_id, quotation_id, version)

    def finish(self, tenant_id: UUID, run_id: UUID, status: str) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                update(WorkflowRun)
                .where(WorkflowRun.tenant_id == tenant_id, WorkflowRun.id == run_id)
                .values(status=status, updated_at=func.now())
            )


class SqlDecisionUnit:
    def __init__(
        self, connection: Connection, tenant_id: UUID, task: TaskState, link: Row[Any]
    ) -> None:
        self._connection = connection
        self._tenant_id = tenant_id
        self._task = task
        self._link = link

    @property
    def task(self) -> TaskState:
        return self._task

    def record(
        self,
        decision: RecordedDecision,
        command: DecisionCommand,
        consume_reference: UUID | None,
    ) -> None:
        connection, tenant_id = self._connection, self._tenant_id
        connection.execute(
            insert(ApprovalDecision).values(
                id=decision.decision_id,
                tenant_id=tenant_id,
                task_id=decision.task_id,
                command_id=decision.command_id,
                quotation_id=decision.quotation_id,
                version=decision.version,
                content_sha256=command.content_sha256,
                action=decision.action.value,
                actor_subject=decision.actor_subject,
                reason=decision.reason,
                transport=decision.transport,
                evidence_ref=command.evidence_ref,
                received_at=command.received_at,
                decided_at=decision.decided_at,
            )
        )
        closed = connection.execute(
            update(ApprovalTask)
            .where(
                ApprovalTask.tenant_id == tenant_id,
                ApprovalTask.id == decision.task_id,
                ApprovalTask.status == TaskStatus.PENDING.value,
            )
            .values(status=TASK_STATUS_AFTER[decision.action].value, decided_at=func.now())
        ).rowcount
        if closed != 1:
            raise RuntimeError("Approval task was not pending under the lock")
        connection.execute(
            update(Quotation)
            .where(Quotation.tenant_id == tenant_id, Quotation.id == decision.quotation_id)
            .values(status=QUOTATION_STATUS_AFTER[decision.action].value, updated_at=func.now())
        )
        if consume_reference is not None:
            connection.execute(
                update(ApprovalActionRef)
                .where(
                    ApprovalActionRef.tenant_id == tenant_id,
                    ApprovalActionRef.id == consume_reference,
                    ApprovalActionRef.consumed_at.is_(None),
                )
                .values(consumed_at=func.now())
            )
        queue_job(
            connection,
            tenant_id,
            JobKind.RESUME_QUOTATION,
            {
                "enquiry_id": str(self._link.enquiry_id),
                "waiting_for": "decision",
                "task_id": str(decision.task_id),
                "decision_id": str(decision.decision_id),
            },
            self._link.correlation_id,
        )


class SqlApprovalStore:
    """API-role decision storage."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_by_command(self, tenant_id: UUID, command_id: UUID) -> RecordedDecision | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_DECISION_COLUMNS).where(
                    ApprovalDecision.tenant_id == tenant_id,
                    ApprovalDecision.command_id == command_id,
                )
            ).one_or_none()
        return None if row is None else _decision(row)

    @contextmanager
    def locked_task(self, tenant_id: UUID, task_id: UUID) -> Iterator[SqlDecisionUnit | None]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            # FOR UPDATE: a concurrent decision on this task waits here until we commit.
            row = connection.execute(
                select(
                    ApprovalTask.id,
                    ApprovalTask.quotation_id,
                    ApprovalTask.version,
                    ApprovalTask.content_sha256,
                    ApprovalTask.status,
                    ApprovalTask.version_id,
                )
                .where(ApprovalTask.tenant_id == tenant_id, ApprovalTask.id == task_id)
                .with_for_update()
            ).one_or_none()
            if row is None:
                yield None
                return
            facts = connection.execute(
                select(Quotation.current_version, QuotationVersion.created_by)
                .join(
                    QuotationVersion,
                    (QuotationVersion.tenant_id == Quotation.tenant_id)
                    & (QuotationVersion.id == row.version_id),
                )
                .where(Quotation.tenant_id == tenant_id, Quotation.id == row.quotation_id)
            ).one()
            decided = connection.execute(
                select(*_DECISION_COLUMNS).where(
                    ApprovalDecision.tenant_id == tenant_id, ApprovalDecision.task_id == task_id
                )
            ).one_or_none()
            task = TaskState(
                task_id=row.id,
                quotation_id=row.quotation_id,
                version=row.version,
                content_sha256=row.content_sha256,
                status=TaskStatus(row.status),
                current_version=facts.current_version,
                version_created_by=facts.created_by,
                decision=None if decided is None else _decision(decided),
            )
            link = enquiry_link(connection, tenant_id, row.quotation_id)
            yield SqlDecisionUnit(connection, tenant_id, task, link)


class SqlQuotationReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list(
        self, tenant_id: UUID, statuses: frozenset[QuotationStatus] | None, limit: int
    ) -> list[QuotationSummary]:
        query = (
            select(
                Quotation.id,
                Quotation.enquiry_id,
                Quotation.status,
                Quotation.current_version,
                Quotation.updated_at,
                Enquiry.customer_reference,
                QuotationVersion.currency,
                _TOTAL,
            )
            .join(
                Enquiry,
                (Enquiry.tenant_id == Quotation.tenant_id) & (Enquiry.id == Quotation.enquiry_id),
            )
            .outerjoin(
                QuotationVersion,
                (QuotationVersion.tenant_id == Quotation.tenant_id)
                & (QuotationVersion.quotation_id == Quotation.id)
                & (QuotationVersion.version == Quotation.current_version),
            )
            .where(Quotation.tenant_id == tenant_id)
            .order_by(Quotation.updated_at.desc(), Quotation.id)
            .limit(limit)
        )
        if statuses is not None:
            query = query.where(Quotation.status.in_([status.value for status in statuses]))
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(query).all()
        return [self._summary(row) for row in rows]

    @staticmethod
    def _summary(row: Row[Any]) -> QuotationSummary:
        return QuotationSummary(
            quotation_id=row.id,
            enquiry_id=row.enquiry_id,
            number=quotation_number(row.id),
            status=QuotationStatus(row.status),
            current_version=row.current_version,
            customer_reference=row.customer_reference,
            currency=row.currency,
            total=row.total,
            updated_at=row.updated_at,
        )

    def get(self, tenant_id: UUID, quotation_id: UUID) -> QuotationDetail | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(
                    Quotation.id,
                    Quotation.enquiry_id,
                    Quotation.status,
                    Quotation.current_version,
                    Quotation.updated_at,
                    Quotation.pricing_issues,
                    Enquiry.customer_reference,
                    Enquiry.text,
                    QuotationVersion.currency,
                    _TOTAL,
                )
                .join(
                    Enquiry,
                    (Enquiry.tenant_id == Quotation.tenant_id)
                    & (Enquiry.id == Quotation.enquiry_id),
                )
                .outerjoin(
                    QuotationVersion,
                    (QuotationVersion.tenant_id == Quotation.tenant_id)
                    & (QuotationVersion.quotation_id == Quotation.id)
                    & (QuotationVersion.version == Quotation.current_version),
                )
                .where(Quotation.tenant_id == tenant_id, Quotation.id == quotation_id)
            ).one_or_none()
            if row is None:
                return None
            versions = connection.execute(
                select(
                    QuotationVersion.id,
                    QuotationVersion.version,
                    QuotationVersion.content,
                    QuotationVersion.content_sha256,
                    QuotationVersion.created_by,
                    QuotationVersion.reason,
                    QuotationVersion.created_at,
                )
                .where(
                    QuotationVersion.tenant_id == tenant_id,
                    QuotationVersion.quotation_id == quotation_id,
                )
                .order_by(QuotationVersion.version)
            ).all()
            tasks = connection.execute(
                select(
                    ApprovalTask.id,
                    ApprovalTask.version,
                    ApprovalTask.content_sha256,
                    ApprovalTask.status,
                    ApprovalTask.created_at,
                    ApprovalTask.decided_at,
                )
                .where(
                    ApprovalTask.tenant_id == tenant_id, ApprovalTask.quotation_id == quotation_id
                )
                .order_by(ApprovalTask.version)
            ).all()
            decisions = connection.execute(
                select(*_DECISION_COLUMNS)
                .where(
                    ApprovalDecision.tenant_id == tenant_id,
                    ApprovalDecision.quotation_id == quotation_id,
                )
                .order_by(ApprovalDecision.decided_at)
            ).all()
        return QuotationDetail(
            summary=self._summary(row),
            enquiry_text=row.text,
            pricing_issues=list(row.pricing_issues or []),
            versions=tuple(
                VersionView(
                    version_id=item.id,
                    version=item.version,
                    content=item.content,
                    content_sha256=item.content_sha256,
                    created_by=item.created_by,
                    reason=item.reason,
                    created_at=item.created_at,
                )
                for item in versions
            ),
            tasks=tuple(
                TaskView(
                    task_id=item.id,
                    version=item.version,
                    content_sha256=item.content_sha256,
                    status=TaskStatus(item.status),
                    created_at=item.created_at,
                    decided_at=item.decided_at,
                )
                for item in tasks
            ),
            decisions=tuple(_decision(item) for item in decisions),
        )


class SqlRevisionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_revision(self, tenant_id: UUID, command_id: UUID) -> tuple[UUID, int] | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(QuotationVersion.quotation_id, QuotationVersion.version).where(
                    QuotationVersion.tenant_id == tenant_id,
                    QuotationVersion.command_id == command_id,
                )
            ).one_or_none()
        return None if row is None else (row.quotation_id, row.version)

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
        version = (base_version or 0) + 1
        with tenant_transaction(self._engine, tenant_id) as connection:
            # Optimistic check: only one revision can move the quotation from base_version.
            moved = connection.execute(
                update(Quotation)
                .where(
                    Quotation.tenant_id == tenant_id,
                    Quotation.id == quotation_id,
                    Quotation.current_version.is_not_distinct_from(base_version),
                    Quotation.status.in_([status.value for status in REVISABLE]),
                )
                .values(
                    current_version=version,
                    status=QuotationStatus.REVISION_SUBMITTED.value,
                    updated_at=func.now(),
                )
            ).rowcount
            if moved != 1:
                raise RevisionConflict("The quotation changed; reload it before revising")
            connection.execute(
                insert(QuotationVersion).values(
                    **version_values(
                        tenant_id,
                        quotation_id,
                        version,
                        content,
                        content_sha256,
                        created_by,
                        reason,
                        command_id,
                    )
                )
            )
            link = enquiry_link(connection, tenant_id, quotation_id)
            queue_job(
                connection,
                tenant_id,
                JobKind.RESUME_QUOTATION,
                {
                    "enquiry_id": str(link.enquiry_id),
                    "waiting_for": "revision",
                    "version": str(version),
                },
                link.correlation_id,
            )
        return version
