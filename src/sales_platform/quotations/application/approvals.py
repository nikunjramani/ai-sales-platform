"""The single approval decision service used by the portal and every manager channel.

Inputs: an authenticated subject (from a session or a verified channel binding) and a
DecisionCommand. Output: the recorded decision, or the earlier one for an exact replay.
Every call re-checks current membership and permission, task status, version freshness and
self-approval inside a transaction holding the task lock. Errors: AccessDenied,
PermissionDenied, SelfApprovalNotAllowed, TaskNotFound, StaleVersion, DecisionConflict.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.domain.tenancy import Permission
from sales_platform.quotations.domain.quotations import (
    DecisionCommand,
    DecisionConflict,
    RecordedDecision,
    SelfApprovalNotAllowed,
    StaleVersion,
    TaskNotFound,
    TaskStatus,
)
from sales_platform.quotations.ports.approvals import ApprovalStore


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DecisionResult:
    decision: RecordedDecision
    replayed: bool


def _same_command(recorded: RecordedDecision, command: DecisionCommand, subject: str) -> bool:
    return (
        recorded.command_id == command.command_id
        and recorded.task_id == command.task_id
        and recorded.action is command.action
        and recorded.actor_subject == subject
    )


class ApprovalService:
    def __init__(
        self,
        store: ApprovalStore,
        resolver: TenantContextResolver,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._clock = clock

    def decide(
        self, subject: str, command: DecisionCommand, consume_reference: UUID | None = None
    ) -> DecisionResult:
        # Current membership is checked on every decision, so removed or demoted people and
        # revoked bindings cannot act on old pages, buttons or messages.
        context = self._resolver.resolve(subject, command.tenant_id)
        context.require(Permission.APPROVE_QUOTATIONS)
        earlier = self._store.find_by_command(command.tenant_id, command.command_id)
        if earlier is not None:
            if _same_command(earlier, command, subject):
                return DecisionResult(earlier, replayed=True)
            raise DecisionConflict(earlier)
        with self._store.locked_task(command.tenant_id, command.task_id) as unit:
            if unit is None:
                raise TaskNotFound("Approval task not found")
            task = unit.task
            if task.decision is not None:
                if _same_command(task.decision, command, subject):
                    return DecisionResult(task.decision, replayed=True)
                raise DecisionConflict(task.decision)
            if task.status is not TaskStatus.PENDING:
                raise DecisionConflict(None)
            if command.content_sha256 != task.content_sha256 or (
                task.current_version != task.version
            ):
                raise StaleVersion("The quotation changed; review the current version")
            if task.version_created_by == subject:
                raise SelfApprovalNotAllowed("You cannot approve a version you created")
            decision = RecordedDecision(
                decision_id=uuid4(),
                command_id=command.command_id,
                task_id=task.task_id,
                quotation_id=task.quotation_id,
                version=task.version,
                action=command.action,
                actor_subject=subject,
                reason=command.reason,
                transport=command.transport,
                decided_at=self._clock(),
            )
            unit.record(decision, command, consume_reference)
        return DecisionResult(decision, replayed=False)
