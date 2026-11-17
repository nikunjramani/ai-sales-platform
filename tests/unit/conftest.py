"""Unit-test fakes for M4/M5: one in-memory "database" implementing every quotation, approval,
document and delivery port.

It mirrors the SQL rules that matter for behaviour (one task per version, one decision per
task, unique command IDs, optimistic revision checks, single-use action references) so the
services, graph and routes can be tested without PostgreSQL. tests/integration proves the
real SQL implementations follow the same rules.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.integrations.application.inbound import ProcessInboundHandler, routes_for
from sales_platform.integrations.application.messaging import OutboundService, SendMessageHandler
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import (
    Capabilities,
    Connection,
    ConnectionStatus,
    Purpose,
)
from sales_platform.integrations.domain.events import InboundEvent, InboundEventType, event_type
from sales_platform.integrations.domain.messages import MessageStatus, OutboundMessage, TemplateKey
from sales_platform.integrations.ports.inbound import RecordedEvent, StoredEvent
from sales_platform.integrations.ports.messages import NewMessage
from sales_platform.integrations.ports.providers import (
    ProviderDefaults,
    ReceiptKind,
    RenderedMessage,
    SendReceipt,
)
from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.domain.tenancy import Role, TenantContext, TenantStatus
from sales_platform.platform.ports.identity import SessionRecord, VerifiedIdentity
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.platform.ports.storage import ObjectStorage
from sales_platform.providers.api import ApiProvider
from sales_platform.quotations.adapters.documents.pdf import ReportLabQuotationRenderer
from sales_platform.quotations.application.acceptance import (
    QuotationExpiryHandler,
    QuotationResponseService,
    ResponseNotificationHandler,
)
from sales_platform.quotations.application.approvals import ApprovalService
from sales_platform.quotations.application.channel_actions import ChannelActionService
from sales_platform.quotations.application.delivery import (
    QuotationDeliveryHandler,
    QuotationDocumentHook,
)
from sales_platform.quotations.application.documents import QuotationDocumentService
from sales_platform.quotations.application.inbound_routes import ManagerActionRoute
from sales_platform.quotations.application.notifications import (
    ApprovalNotificationHandler,
    ApprovalRequestHook,
)
from sales_platform.quotations.application.pricing import QuotationPricingService
from sales_platform.quotations.application.quotations import QuotationService
from sales_platform.quotations.domain.acceptance import (
    DEFAULT_QUOTE_VALIDITY_DAYS,
    QUOTATION_STATUS_FOR,
    SYSTEM_SUBJECT,
    Offer,
    OfferNotOpen,
    RecordedResponse,
    ResponseOutcome,
    customer_status,
)
from sales_platform.quotations.domain.documents import (
    OFFERED_STATUSES,
    DeliveryRecord,
    DeliveryStatus,
    DocumentSource,
    StoredDocument,
)
from sales_platform.quotations.domain.quotations import (
    QUOTATION_STATUS_AFTER,
    REVISABLE,
    TASK_STATUS_AFTER,
    ApprovalAction,
    DecisionCommand,
    QuotationStatus,
    RecordedDecision,
    ReviewPacket,
    RevisionConflict,
    TaskStatus,
    quotation_number,
)
from sales_platform.quotations.ports.acceptance import NewResponse
from sales_platform.quotations.ports.approvals import TaskState
from sales_platform.quotations.ports.channels import ActionReference, ChannelBinding
from sales_platform.quotations.ports.documents import DeliveryWork
from sales_platform.quotations.ports.quotations import (
    QuotationDetail,
    QuotationSummary,
    TaskView,
    VersionView,
)
from sales_platform.quotations.workflows.quotation import (
    QuotationJobHandler,
    QuotationWorkflowDeps,
    build_quotation_graph,
)
from tests.support.demo_catalogue import DemoCatalogue


@dataclass
class QuotationRow:
    quotation_id: UUID
    tenant_id: UUID
    enquiry_id: UUID
    status: QuotationStatus
    current_version: int | None
    pricing_issues: list[Mapping[str, object]]
    updated_at: datetime


@dataclass
class VersionRow:
    view: VersionView
    tenant_id: UUID
    quotation_id: UUID
    command_id: UUID | None


@dataclass
class TaskRow:
    task_id: UUID
    tenant_id: UUID
    quotation_id: UUID
    version: int
    content_sha256: str
    status: TaskStatus
    created_at: datetime
    decided_at: datetime | None = None


@dataclass
class ReferenceRow:
    reference_id: UUID
    tenant_id: UUID
    task_id: UUID
    binding_id: UUID
    token_sha256: str
    expires_at: datetime
    sent_at: datetime | None = None
    consumed_at: datetime | None = None


@dataclass
class EnrollmentRow:
    enrollment_id: UUID
    tenant_id: UUID
    subject: str
    channel: str
    code_sha256: str
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass
class Job:
    tenant_id: UUID
    kind: JobKind
    payload: dict[str, str]
    run_at: datetime | None = None  # delayed jobs (expiry) wait until the harness clock reaches it


class _RunLock:
    def __init__(self, db: "MemoryQuotations", run_id: UUID) -> None:
        self._db, self._run_id, self._held = db, run_id, False

    def __enter__(self) -> bool:
        self._held = self._run_id not in self._db.locked_runs
        if self._held:
            self._db.locked_runs.add(self._run_id)
        return self._held

    def __exit__(self, *exc: object) -> None:
        if self._held:
            self._db.locked_runs.discard(self._run_id)


class _DecisionUnit:
    def __init__(self, db: "MemoryQuotations", tenant_id: UUID, task: TaskState) -> None:
        self._db, self._tenant_id, self._task = db, tenant_id, task

    @property
    def task(self) -> TaskState:
        return self._task

    def record(
        self, decision: RecordedDecision, command: DecisionCommand, consume_reference: UUID | None
    ) -> None:
        db = self._db
        db.decisions[decision.decision_id] = (self._tenant_id, decision)
        row = db.tasks[decision.task_id]
        row.status = TASK_STATUS_AFTER[decision.action]
        row.decided_at = decision.decided_at
        quotation = db.quotations[decision.quotation_id]
        quotation.status = QUOTATION_STATUS_AFTER[decision.action]
        if consume_reference is not None:
            db.references[consume_reference].consumed_at = db.clock()
        db.jobs.append(
            Job(
                self._tenant_id,
                JobKind.RESUME_QUOTATION,
                {
                    "enquiry_id": str(quotation.enquiry_id),
                    "waiting_for": "decision",
                    "task_id": str(decision.task_id),
                    "decision_id": str(decision.decision_id),
                },
            )
        )


def memory_connection_id(tenant_id: UUID, provider_key: str, account_ref: str) -> UUID:
    """Deterministic connection IDs, so the memory stores and MemoryConnections agree."""
    return uuid5(NAMESPACE_URL, f"memory-connection:{tenant_id}:{provider_key}:{account_ref}")


def sender_account(channel: str, recipient: str) -> str:
    """The connection a memory enquiry arrived on: an API key, or the business's shop account."""
    return recipient if channel == "api" else f"shop-{channel}"


@dataclass
class MemoryQuotations:
    clock: Callable[[], datetime]
    roles: dict[tuple[UUID, str], frozenset[Role]] = field(default_factory=dict)
    tenant_names: dict[UUID, str] = field(default_factory=dict)
    extraction: dict[tuple[UUID, UUID], list[RequestedLine]] = field(default_factory=dict)
    customer_refs: dict[UUID, str | None] = field(default_factory=dict)
    runs: dict[tuple[UUID, UUID, str], UUID] = field(default_factory=dict)
    run_status: dict[UUID, str] = field(default_factory=dict)
    locked_runs: set[UUID] = field(default_factory=set)
    quotations: dict[UUID, QuotationRow] = field(default_factory=dict)
    versions: list[VersionRow] = field(default_factory=list)
    tasks: dict[UUID, TaskRow] = field(default_factory=dict)
    decisions: dict[UUID, tuple[UUID, RecordedDecision]] = field(default_factory=dict)
    jobs: list[Job] = field(default_factory=list)
    bindings: dict[UUID, ChannelBinding] = field(default_factory=dict)
    enrollments: dict[UUID, EnrollmentRow] = field(default_factory=dict)
    binding_enrollment: dict[UUID, UUID] = field(default_factory=dict)
    references: dict[UUID, ReferenceRow] = field(default_factory=dict)
    message_references: dict[UUID, UUID] = field(default_factory=dict)  # message -> reference
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    # enquiry -> (channel, recipient) of whoever sent it; default: an API client.
    senders: dict[UUID, tuple[str, str]] = field(default_factory=dict)
    delivery_rows: dict[UUID, tuple[UUID, DeliveryRecord]] = field(default_factory=dict)
    documents: dict[UUID, tuple[UUID, StoredDocument]] = field(default_factory=dict)
    validity_days: dict[UUID, int] = field(default_factory=dict)  # tenant -> days (default 7)
    delivery_messages: dict[UUID, UUID] = field(default_factory=dict)  # delivery -> message
    # response ID -> (tenant, response, idempotency key, fingerprint)
    response_rows: dict[UUID, tuple[UUID, RecordedResponse, str, str]] = field(default_factory=dict)
    # Name of a method -> exception raised on its next call (failure injection).
    fail_next: dict[str, Exception] = field(default_factory=dict)

    def _maybe_fail(self, name: str) -> None:
        error = self.fail_next.pop(name, None)
        if error is not None:
            raise error

    # --- test helpers -------------------------------------------------------------------
    def add_member(self, tenant_id: UUID, subject: str, *roles: Role) -> None:
        self.roles[(tenant_id, subject)] = frozenset(roles) | self.roles.get(
            (tenant_id, subject), frozenset()
        )

    def take_jobs(self, kind: JobKind | None = None) -> list[Job]:
        taken = [job for job in self.jobs if kind is None or job.kind is kind]
        self.jobs = [job for job in self.jobs if job not in taken]
        return taken

    def _versions(self, tenant_id: UUID, quotation_id: UUID) -> list[VersionRow]:
        return sorted(
            (
                row
                for row in self.versions
                if row.tenant_id == tenant_id and row.quotation_id == quotation_id
            ),
            key=lambda row: row.view.version,
        )

    def _quotation(self, tenant_id: UUID, quotation_id: UUID) -> QuotationRow | None:
        row = self.quotations.get(quotation_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    def _add_version(
        self,
        tenant_id: UUID,
        quotation_id: UUID,
        version: int,
        content: Mapping[str, object],
        sha: str,
        created_by: str,
        reason: str | None,
        command_id: UUID | None,
    ) -> None:
        if any(
            row.tenant_id == tenant_id and row.command_id == command_id and command_id
            for row in self.versions
        ):
            raise RevisionConflict("duplicate command")
        self.versions.append(
            VersionRow(
                VersionView(uuid4(), version, dict(content), sha, created_by, reason, self.clock()),
                tenant_id,
                quotation_id,
                command_id,
            )
        )

    # --- MembershipReader -----------------------------------------------------------------
    def roles_for(self, tenant_id: UUID, subject: str) -> frozenset[Role]:
        return self.roles.get((tenant_id, subject), frozenset())

    # --- QuotationWorkflowStore -----------------------------------------------------------
    def start_run(self, tenant_id: UUID, enquiry_id: UUID, graph_version: str) -> UUID:
        return self.runs.setdefault((tenant_id, enquiry_id, graph_version), uuid4())

    def lock_run(self, run_id: UUID) -> _RunLock:
        return _RunLock(self, run_id)

    def extraction_lines(self, tenant_id: UUID, enquiry_id: UUID) -> list[RequestedLine] | None:
        return self.extraction.get((tenant_id, enquiry_id))

    def create_quotation(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        content: Mapping[str, object] | None,
        content_sha256: str | None,
        pricing_issues: Sequence[Mapping[str, object]] | None,
        created_by: str,
    ) -> tuple[UUID, int | None]:
        self._maybe_fail("create_quotation")
        for row in self.quotations.values():
            if row.tenant_id == tenant_id and row.enquiry_id == enquiry_id:
                return row.quotation_id, row.current_version
        quotation_id = uuid4()
        priced = content is not None and content_sha256 is not None
        self.quotations[quotation_id] = QuotationRow(
            quotation_id,
            tenant_id,
            enquiry_id,
            QuotationStatus.PENDING_APPROVAL if priced else QuotationStatus.PRICING_FAILED,
            1 if priced else None,
            list(pricing_issues or []),
            self.clock(),
        )
        if content is not None and content_sha256 is not None:
            self._add_version(
                tenant_id, quotation_id, 1, content, content_sha256, created_by, None, None
            )
        return quotation_id, 1 if priced else None

    def current_version(self, tenant_id: UUID, quotation_id: UUID) -> int | None:
        row = self._quotation(tenant_id, quotation_id)
        return None if row is None else row.current_version

    def request_approval(self, tenant_id: UUID, quotation_id: UUID, version: int) -> UUID:
        self._maybe_fail("request_approval")
        stored = next(
            row for row in self._versions(tenant_id, quotation_id) if row.view.version == version
        )
        for task in self.tasks.values():
            if task.tenant_id == tenant_id and task.quotation_id == quotation_id:
                if task.version == version:
                    return task.task_id
        task_id = uuid4()
        self.tasks[task_id] = TaskRow(
            task_id,
            tenant_id,
            quotation_id,
            version,
            stored.view.content_sha256,
            TaskStatus.PENDING,
            self.clock(),
        )
        quotation = self.quotations[quotation_id]
        quotation.status = QuotationStatus.PENDING_APPROVAL
        self.jobs.append(
            Job(
                tenant_id,
                JobKind.NOTIFY_APPROVERS,
                {"task_id": str(task_id), "enquiry_id": str(quotation.enquiry_id)},
            )
        )
        return task_id

    def decision(self, tenant_id: UUID, decision_id: UUID) -> RecordedDecision | None:
        found = self.decisions.get(decision_id)
        return found[1] if found is not None and found[0] == tenant_id else None

    def finish(self, tenant_id: UUID, run_id: UUID, status: str) -> None:
        self.run_status[run_id] = status

    # --- ApprovalStore --------------------------------------------------------------------
    def find_by_command(self, tenant_id: UUID, command_id: UUID) -> RecordedDecision | None:
        return next(
            (
                decision
                for tenant, decision in self.decisions.values()
                if tenant == tenant_id and decision.command_id == command_id
            ),
            None,
        )

    @contextmanager
    def locked_task(self, tenant_id: UUID, task_id: UUID) -> Iterator[_DecisionUnit | None]:
        task = self.tasks.get(task_id)
        if task is None or task.tenant_id != tenant_id:
            yield None
            return
        quotation = self.quotations[task.quotation_id]
        version = next(
            row
            for row in self._versions(tenant_id, task.quotation_id)
            if row.view.version == task.version
        )
        decision = next((d for tenant, d in self.decisions.values() if d.task_id == task_id), None)
        yield _DecisionUnit(
            self,
            tenant_id,
            TaskState(
                task.task_id,
                task.quotation_id,
                task.version,
                task.content_sha256,
                task.status,
                quotation.current_version,
                version.view.created_by,
                decision,
            ),
        )

    # --- QuotationReader / QuotationRevisionStore -----------------------------------------
    def _summary(self, row: QuotationRow) -> QuotationSummary:
        current = next(
            (
                version
                for version in self._versions(row.tenant_id, row.quotation_id)
                if version.view.version == row.current_version
            ),
            None,
        )
        return QuotationSummary(
            row.quotation_id,
            row.enquiry_id,
            quotation_number(row.quotation_id),
            row.status,
            row.current_version,
            self.customer_refs.get(row.enquiry_id),
            None if current is None else str(current.view.content["currency"]),
            None if current is None else str(current.view.content["total"]),
            row.updated_at,
        )

    def find_revision(self, tenant_id: UUID, command_id: UUID) -> tuple[UUID, int] | None:
        return next(
            (
                (row.quotation_id, row.view.version)
                for row in self.versions
                if row.tenant_id == tenant_id and row.command_id == command_id
            ),
            None,
        )

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
        row = self._quotation(tenant_id, quotation_id)
        if row is None or row.current_version != base_version or row.status not in REVISABLE:
            raise RevisionConflict("changed")
        version = (base_version or 0) + 1
        self._add_version(
            tenant_id, quotation_id, version, content, content_sha256, created_by, reason,
            command_id,
        )  # fmt: skip
        row.current_version = version
        row.status = QuotationStatus.REVISION_SUBMITTED
        self.jobs.append(
            Job(
                tenant_id,
                JobKind.RESUME_QUOTATION,
                {
                    "enquiry_id": str(row.enquiry_id),
                    "waiting_for": "revision",
                    "version": str(version),
                },
            )
        )
        return version

    # --- NotificationStore ----------------------------------------------------------------
    def review_packet(self, tenant_id: UUID, task_id: UUID) -> ReviewPacket | None:
        task = self.tasks.get(task_id)
        if task is None or task.tenant_id != tenant_id or task.status is not TaskStatus.PENDING:
            return None
        version = next(
            row
            for row in self._versions(tenant_id, task.quotation_id)
            if row.view.version == task.version
        )
        quotation = self.quotations[task.quotation_id]
        return ReviewPacket(
            tenant_id,
            self.tenant_names.get(tenant_id, "Demo"),
            task_id,
            task.quotation_id,
            task.version,
            task.content_sha256,
            self.customer_refs.get(quotation.enquiry_id),
            version.view.content,
        )

    def approver_bindings(self, tenant_id: UUID) -> list[ChannelBinding]:
        return [
            binding
            for binding in self.bindings.values()
            if binding.tenant_id == tenant_id
            and binding.active
            and Role.APPROVER in self.roles.get((tenant_id, binding.subject), frozenset())
        ]

    def prepare_reference(
        self,
        tenant_id: UUID,
        task_id: UUID,
        binding_id: UUID,
        token_sha256: str,
        expires_at: datetime,
    ) -> UUID | None:
        self._maybe_fail("prepare_reference")
        for row in self.references.values():
            if (row.tenant_id, row.task_id, row.binding_id) == (tenant_id, task_id, binding_id):
                if row.sent_at is not None:
                    return None
                row.token_sha256, row.expires_at = token_sha256, expires_at
                return row.reference_id
        reference_id = uuid4()
        self.references[reference_id] = ReferenceRow(
            reference_id, tenant_id, task_id, binding_id, token_sha256, expires_at
        )
        return reference_id

    def mark_sent(self, tenant_id: UUID, reference_id: UUID) -> None:
        self.references[reference_id].sent_at = self.clock()

    def link_message(self, tenant_id: UUID, message_id: UUID, reference_id: UUID) -> None:
        self.message_references[message_id] = reference_id

    # --- ChannelStore ---------------------------------------------------------------------
    def create_enrollment(
        self,
        tenant_id: UUID,
        subject: str,
        channel: str,
        code_sha256: str,
        expires_at: datetime,
        connection_ref: UUID | None = None,
    ) -> None:
        enrollment_id = uuid4()
        self.enrollments[enrollment_id] = EnrollmentRow(
            enrollment_id, tenant_id, subject, channel, code_sha256, expires_at
        )

    def enroll(
        self,
        tenant_id: UUID,
        channel: str,
        connection_id: str,
        external_user_id: str,
        code_sha256: str,
        now: datetime,
    ) -> ChannelBinding | None:
        row = next(
            (
                item
                for item in self.enrollments.values()
                if item.tenant_id == tenant_id and item.code_sha256 == code_sha256
            ),
            None,
        )
        if row is None or row.channel != channel:
            return None

        def same(binding: ChannelBinding) -> bool:
            return (binding.tenant_id, binding.channel, binding.connection_id,
                    binding.external_user_id) == (tenant_id, channel, connection_id,
                                                  external_user_id)  # fmt: skip

        if row.consumed_at is not None:
            return next(
                (
                    binding
                    for binding_id, binding in self.bindings.items()
                    if same(binding) and self.binding_enrollment[binding_id] == row.enrollment_id
                ),
                None,
            )
        if row.expires_at <= now or any(
            same(binding) and binding.active for binding in self.bindings.values()
        ):
            return None
        binding = ChannelBinding(
            uuid4(), tenant_id, row.subject, channel, connection_id, external_user_id, now, None,
            memory_connection_id(tenant_id, channel, connection_id),
        )  # fmt: skip
        self.bindings[binding.binding_id] = binding
        self.binding_enrollment[binding.binding_id] = row.enrollment_id
        row.consumed_at = now
        return binding

    def find_reference(
        self, tenant_id: UUID, token_sha256: str
    ) -> tuple[ActionReference, ChannelBinding] | None:
        return self._reference(tenant_id, lambda row: row.token_sha256 == token_sha256)

    def get_reference(
        self, tenant_id: UUID, reference_id: UUID
    ) -> tuple[ActionReference, ChannelBinding] | None:
        return self._reference(tenant_id, lambda row: row.reference_id == reference_id)

    def _reference(
        self, tenant_id: UUID, matches: Callable[[ReferenceRow], bool]
    ) -> tuple[ActionReference, ChannelBinding] | None:
        for row in self.references.values():
            if row.tenant_id == tenant_id and matches(row) and row.sent_at:
                task = self.tasks[row.task_id]
                return (
                    ActionReference(
                        row.reference_id,
                        row.task_id,
                        row.binding_id,
                        task.content_sha256,
                        row.expires_at,
                        row.consumed_at,
                    ),
                    self.bindings[row.binding_id],
                )
        return None

    def bindings_for(self, tenant_id: UUID, subject: str | None) -> list[ChannelBinding]:
        return [
            binding
            for binding in self.bindings.values()
            if binding.tenant_id == tenant_id and (subject is None or binding.subject == subject)
        ]

    def revoke(self, tenant_id: UUID, binding_id: UUID, subject: str | None) -> bool:
        binding = self.bindings.get(binding_id)
        if (
            binding is None
            or binding.tenant_id != tenant_id
            or not binding.active
            or (subject is not None and binding.subject != subject)
        ):
            return False
        self.bindings[binding_id] = replace(binding, revoked_at=self.clock())
        return True

    # --- SessionStore ---------------------------------------------------------------------
    def create(
        self,
        session_id: UUID,
        token_sha256: str,
        identity: VerifiedIdentity,
        expires_at: datetime,
    ) -> None:
        self.sessions[token_sha256] = SessionRecord(
            session_id, identity.subject, identity.provider, expires_at, False
        )

    def find(self, token_sha256: str) -> SessionRecord | None:
        return self.sessions.get(token_sha256)

    def revoke_session(self, token_sha256: str) -> None:
        record = self.sessions.get(token_sha256)
        if record is not None:
            self.sessions[token_sha256] = replace(record, revoked=True)

    # --- QuotationWorkflowStore.queue_delivery, DeliveryStore and DocumentReader ---------
    def queue_delivery(self, tenant_id: UUID, quotation_id: UUID, version: int) -> bool:
        row = self._quotation(tenant_id, quotation_id)
        if (
            row is None
            or row.status is not QuotationStatus.APPROVED
            or row.current_version != version
        ):
            return False
        channel, recipient = self.senders.get(
            row.enquiry_id, ("api", "api-key:00000000-0000-4000-8000-0000000000c1")
        )
        target = (tenant_id, quotation_id, version, channel, recipient)
        for tenant, record in self.delivery_rows.values():
            key = (tenant, record.quotation_id, record.version, record.channel, record.recipient)
            if key == target:
                return True
        now = self.clock()
        delivery_id = uuid4()
        self.delivery_rows[delivery_id] = (
            tenant_id,
            DeliveryRecord(
                delivery_id=delivery_id,
                quotation_id=quotation_id,
                version=version,
                channel=channel,
                recipient=recipient,
                status=DeliveryStatus.PENDING,
                document_id=None,
                provider_reference=None,
                last_error=None,
                created_at=now,
                updated_at=now,
                delivered_at=None,
                first_downloaded_at=None,
            ),
        )
        self.jobs.append(
            Job(tenant_id, JobKind.DELIVER_QUOTATION, {"delivery_id": str(delivery_id)})
        )
        return True

    def _document_for(
        self, tenant_id: UUID, quotation_id: UUID, version: int
    ) -> StoredDocument | None:
        return next(
            (
                document
                for tenant, document in self.documents.values()
                if tenant == tenant_id
                and document.quotation_id == quotation_id
                and document.version == version
            ),
            None,
        )

    def delivery(self, tenant_id: UUID, delivery_id: UUID) -> DeliveryWork | None:
        found = self.delivery_rows.get(delivery_id)
        if found is None or found[0] != tenant_id:
            return None
        record = found[1]
        quotation = self.quotations[record.quotation_id]
        return DeliveryWork(
            record,
            self.tenant_names.get(tenant_id, "Demo"),
            self.customer_refs.get(quotation.enquiry_id),
            self._document_for(tenant_id, record.quotation_id, record.version),
            self.validity_days.get(tenant_id, DEFAULT_QUOTE_VALIDITY_DAYS),
            memory_connection_id(
                tenant_id, record.channel, sender_account(record.channel, record.recipient)
            ),
            quotation.enquiry_id,
            uuid4(),
            self.delivery_messages.get(delivery_id),
        )

    def document(self, tenant_id: UUID, document_id: UUID) -> StoredDocument | None:
        found = self.documents.get(document_id)
        return found[1] if found is not None and found[0] == tenant_id else None

    def _source(self, tenant_id: UUID, quotation_id: UUID, version: int) -> DocumentSource | None:
        quotation = self._quotation(tenant_id, quotation_id)
        stored = next(
            (row for row in self._versions(tenant_id, quotation_id) if row.view.version == version),
            None,
        )
        if quotation is None or stored is None:
            return None
        approved_at = next(
            (
                decision.decided_at
                for tenant, decision in self.decisions.values()
                if tenant == tenant_id
                and decision.quotation_id == quotation_id
                and decision.version == version
                and decision.action is ApprovalAction.APPROVE
                and self.tasks[decision.task_id].content_sha256 == stored.view.content_sha256
            ),
            None,
        )
        return DocumentSource(
            tenant_name=self.tenant_names.get(tenant_id, "Demo"),
            quotation_id=quotation_id,
            version=version,
            content=dict(stored.view.content),
            content_sha256=stored.view.content_sha256,
            customer_reference=self.customer_refs.get(quotation.enquiry_id),
            approved_at=approved_at,
        )

    def approved_source(
        self, tenant_id: UUID, quotation_id: UUID, version: int
    ) -> DocumentSource | None:
        source = self._source(tenant_id, quotation_id, version)
        quotation = self._quotation(tenant_id, quotation_id)
        if (
            source is None
            or source.approved_at is None
            or quotation is None
            or quotation.status is not QuotationStatus.APPROVED
            or quotation.current_version != version
        ):
            return None
        return source

    def save_document(self, tenant_id: UUID, document: StoredDocument) -> StoredDocument:
        existing = self._document_for(tenant_id, document.quotation_id, document.version)
        if existing is not None:
            return existing
        self.documents[document.document_id] = (tenant_id, document)
        return document

    def set_status(
        self,
        tenant_id: UUID,
        delivery_id: UUID,
        status: DeliveryStatus,
        *,
        expected: frozenset[DeliveryStatus],
        document_id: UUID | None = None,
        provider_reference: str | None = None,
        error: str | None = None,
    ) -> bool:
        found = self.delivery_rows.get(delivery_id)
        if found is None or found[0] != tenant_id or found[1].status not in expected:
            return False
        record = found[1]
        now = self.clock()
        self.delivery_rows[delivery_id] = (
            tenant_id,
            replace(
                record,
                status=status,
                last_error=error,
                updated_at=now,
                document_id=document_id or record.document_id,
                provider_reference=provider_reference or record.provider_reference,
                delivered_at=now if status in OFFERED_STATUSES else record.delivered_at,
            ),
        )
        if status in OFFERED_STATUSES:
            document = self.documents[document_id or record.document_id or uuid4()][1]
            self.jobs.append(
                Job(
                    tenant_id,
                    JobKind.EXPIRE_QUOTATION,
                    {"quotation_id": str(record.quotation_id), "version": str(record.version)},
                    run_at=document.valid_until,
                )
            )
        return True

    def source(self, tenant_id: UUID, quotation_id: UUID, version: int) -> DocumentSource | None:
        return self._source(tenant_id, quotation_id, version)

    def approved_document(self, tenant_id: UUID, quotation_id: UUID) -> StoredDocument | None:
        found = [
            document
            for tenant, document in self.documents.values()
            if tenant == tenant_id and document.quotation_id == quotation_id
        ]
        return max(found, key=lambda document: document.version, default=None)

    def deliveries(self, tenant_id: UUID, quotation_id: UUID) -> list[DeliveryRecord]:
        return [
            record
            for tenant, record in self.delivery_rows.values()
            if tenant == tenant_id and record.quotation_id == quotation_id
        ]

    def offered(
        self, tenant_id: UUID, enquiry_id: UUID, channel: str
    ) -> tuple[DeliveryRecord, StoredDocument, dict[str, object]] | None:
        candidates = [
            record
            for tenant, record in self.delivery_rows.values()
            if tenant == tenant_id
            and self.quotations[record.quotation_id].enquiry_id == enquiry_id
            and record.channel == channel
            and record.status in OFFERED_STATUSES
            and record.document_id is not None
        ]
        if not candidates:
            return None
        record = max(candidates, key=lambda item: item.version)
        assert record.document_id is not None
        document = self.documents[record.document_id][1]
        source = self._source(tenant_id, record.quotation_id, record.version)
        assert source is not None
        return record, document, source.content

    def record_download(self, tenant_id: UUID, delivery_id: UUID, at: datetime) -> None:
        tenant, record = self.delivery_rows[delivery_id]
        if tenant == tenant_id and record.first_downloaded_at is None:
            self.delivery_rows[delivery_id] = (tenant, replace(record, first_downloaded_at=at))

    # --- ResponseStore (M6) ----------------------------------------------------------------
    def find_response(
        self, tenant_id: UUID, idempotency_key: str
    ) -> tuple[RecordedResponse, str] | None:
        for tenant, response, key, fingerprint in self.response_rows.values():
            if tenant == tenant_id and key == idempotency_key:
                return response, fingerprint
        return None

    def _offer(self, tenant_id: UUID, records: list[DeliveryRecord]) -> Offer | None:
        offered = [r for r in records if r.status in OFFERED_STATUSES and r.document_id]
        if not offered:
            return None
        record = max(offered, key=lambda item: item.version)
        assert record.document_id is not None
        document = self.documents[record.document_id][1]
        quotation = self.quotations[record.quotation_id]
        version = next(
            row.view
            for row in self._versions(tenant_id, record.quotation_id)
            if row.view.version == record.version
        )
        return Offer(
            tenant_id=tenant_id,
            tenant_name=self.tenant_names.get(tenant_id, "Demo"),
            quotation_id=record.quotation_id,
            enquiry_id=quotation.enquiry_id,
            status=quotation.status,
            current_version=quotation.current_version,
            version=record.version,
            content=dict(version.content),
            content_sha256=version.content_sha256,
            valid_until=document.valid_until,
            issued_at=record.delivered_at,
        )

    def offer_for_enquiry(self, tenant_id: UUID, enquiry_id: UUID, channel: str) -> Offer | None:
        return self._offer(
            tenant_id,
            [
                record
                for tenant, record in self.delivery_rows.values()
                if tenant == tenant_id
                and self.quotations[record.quotation_id].enquiry_id == enquiry_id
                and record.channel == channel
            ],
        )

    def offer_for_quotation(self, tenant_id: UUID, quotation_id: UUID) -> Offer | None:
        return self._offer(
            tenant_id,
            [
                record
                for tenant, record in self.delivery_rows.values()
                if tenant == tenant_id and record.quotation_id == quotation_id
            ],
        )

    def record(
        self,
        tenant_id: UUID,
        offer: Offer,
        response: NewResponse,
        now: datetime,
        consume_ref: UUID | None = None,
    ) -> RecordedResponse:
        self._maybe_fail("record")
        quotation = self.quotations[offer.quotation_id]
        expiring = response.outcome is ResponseOutcome.EXPIRED
        still_open = (
            quotation.status is QuotationStatus.APPROVED
            and quotation.current_version == offer.version
            and ((now >= offer.valid_until) if expiring else (now < offer.valid_until))
        )
        if not still_open:
            raise OfferNotOpen(
                customer_status(
                    quotation.status,
                    offer.version,
                    quotation.current_version,
                    offer.valid_until,
                    now,
                )
            )
        if self.find_response(tenant_id, response.idempotency_key) is not None:
            raise RevisionConflict("duplicate idempotency key")  # the SQL unique constraint
        revised_version = None
        quotation.status = QUOTATION_STATUS_FOR[response.outcome]
        revised = response.revised
        if revised is not None and revised.content is not None and revised.content_sha256:
            revised_version = offer.version + 1
            self._add_version(
                tenant_id,
                offer.quotation_id,
                revised_version,
                revised.content,
                revised.content_sha256,
                SYSTEM_SUBJECT,
                "re-priced at acceptance",
                None,
            )
            quotation.current_version = revised_version
        elif revised is not None:
            quotation.status = QuotationStatus.PRICING_FAILED
            quotation.pricing_issues = list(revised.pricing_issues or [])
        recorded = RecordedResponse(
            response_id=uuid4(),
            quotation_id=offer.quotation_id,
            version=offer.version,
            content_sha256=offer.content_sha256,
            outcome=response.outcome,
            actor=response.actor,
            reason=response.reason,
            differences=response.differences,
            revised_version=revised_version,
            created_at=now,
        )
        self.response_rows[recorded.response_id] = (
            tenant_id,
            recorded,
            response.idempotency_key,
            response.fingerprint,
        )
        self.jobs.append(
            Job(
                tenant_id,
                JobKind.RESUME_QUOTATION,
                {
                    "enquiry_id": str(quotation.enquiry_id),
                    "waiting_for": "customer",
                    "version": str(offer.version),
                    "response_id": str(recorded.response_id),
                },
            )
        )
        self.jobs.append(
            Job(tenant_id, JobKind.NOTIFY_RESPONSE, {"response_id": str(recorded.response_id)})
        )
        return recorded

    def response(self, tenant_id: UUID, response_id: UUID) -> RecordedResponse | None:
        found = self.response_rows.get(response_id)
        return found[1] if found is not None and found[0] == tenant_id else None

    def responses(self, tenant_id: UUID, quotation_id: UUID) -> list[RecordedResponse]:
        return [
            response
            for tenant, response, _, _ in self.response_rows.values()
            if tenant == tenant_id and response.quotation_id == quotation_id
        ]

    # --- QuotationReader (last: `list` would shadow the builtin in later annotations) --
    def list(
        self, tenant_id: UUID, statuses: frozenset[QuotationStatus] | None, limit: int
    ) -> list[QuotationSummary]:
        rows = [
            row
            for row in self.quotations.values()
            if row.tenant_id == tenant_id and (statuses is None or row.status in statuses)
        ]
        return [self._summary(row) for row in rows][:limit]

    def get(self, tenant_id: UUID, quotation_id: UUID) -> QuotationDetail | None:
        row = self._quotation(tenant_id, quotation_id)
        if row is None:
            return None
        tasks = sorted(
            (task for task in self.tasks.values() if task.quotation_id == quotation_id),
            key=lambda task: task.version,
        )
        return QuotationDetail(
            summary=self._summary(row),
            enquiry_text="Please quote 2 x IND-FAN-450",
            pricing_issues=list(row.pricing_issues),
            versions=tuple(version.view for version in self._versions(tenant_id, quotation_id)),
            tasks=tuple(
                TaskView(
                    task.task_id,
                    task.version,
                    task.content_sha256,
                    task.status,
                    task.created_at,
                    task.decided_at,
                )
                for task in tasks
            ),
            decisions=tuple(
                decision
                for tenant, decision in self.decisions.values()
                if decision.quotation_id == quotation_id
            ),
        )


class MemorySessions:
    """SessionStore view of MemoryQuotations (its revoke name is taken by bindings)."""

    def __init__(self, db: MemoryQuotations) -> None:
        self._db = db

    def create(
        self, session_id: UUID, token_sha256: str, identity: VerifiedIdentity, expires: datetime
    ) -> None:
        self._db.create(session_id, token_sha256, identity, expires)

    def find(self, token_sha256: str) -> SessionRecord | None:
        return self._db.find(token_sha256)

    def revoke(self, token_sha256: str) -> None:
        self._db.revoke_session(token_sha256)


class MemoryDirectory:
    """MembershipDirectory over MemoryQuotations.roles."""

    def __init__(self, db: MemoryQuotations) -> None:
        self._db = db

    def tenants_for(self, subject: str) -> list[Any]:
        from sales_platform.platform.ports.tenancy import TenantMembership

        return [
            TenantMembership(tenant, self._db.tenant_names.get(tenant, "Demo"), roles)
            for (tenant, member), roles in self._db.roles.items()
            if member == subject
        ]


@pytest.fixture
def quotation_db(now: datetime, industrial_tenant: UUID, office_tenant: UUID) -> MemoryQuotations:
    db = MemoryQuotations(clock=lambda: now)
    db.tenant_names = {industrial_tenant: "Demo Industrial", office_tenant: "Demo Office"}
    db.add_member(industrial_tenant, "manager@industrial.example", Role.APPROVER)
    db.add_member(industrial_tenant, "sales@industrial.example", Role.SALES_OPERATOR)
    db.add_member(industrial_tenant, "viewer@industrial.example", Role.VIEWER)
    db.add_member(office_tenant, "sales@office.example", Role.SALES_OPERATOR)
    return db


class RecordingProvider(ProviderDefaults):
    """A provider fake that records what it is asked to send.

    `outcome` decides the next send: None = accept, or an exception instance to raise once.
    `fail` (the old notifier switch) makes the next send raise OSError.
    """

    dev_only = False

    def __init__(self, key: str, purpose: Purpose, kind: ReceiptKind = ReceiptKind.SENT) -> None:
        self.key = key
        self.display_name = key
        self.purposes = frozenset({purpose})
        self.capabilities = Capabilities()
        self._kind = kind
        self.messages: list[RenderedMessage] = []
        self.outcome: Exception | None = None
        self.fail = False

    def send(self, conn: Connection, secrets: Any, message: RenderedMessage) -> SendReceipt:
        if self.fail:
            self.fail = False
            raise OSError("provider unavailable")
        if self.outcome is not None:
            error, self.outcome = self.outcome, None
            raise error
        self.messages.append(message)
        return SendReceipt(self._kind, f"msg-{len(self.messages)}")

    @property
    def sent(self) -> list[tuple[str, RenderedMessage, str]]:
        """Approval requests: (recipient, message, action reference)."""
        return [
            (m.recipient, m, m.action_reference or "")
            for m in self.messages
            if "task_id" in m.metadata
        ]

    @property
    def notices(self) -> list[tuple[str, UUID, str]]:
        return [
            (m.recipient, UUID(m.metadata["notice_id"]), m.text)
            for m in self.messages
            if "notice_id" in m.metadata
        ]

    @property
    def documents(self) -> list[tuple[RenderedMessage, bytes]]:
        return [(m, m.attachment.data) for m in self.messages if m.attachment is not None]

    def test(self, conn: Connection, secrets: Any) -> Any:
        raise NotImplementedError

    def verify_inbound(self, conn: Connection, secrets: Any, request: Any) -> None:
        raise NotImplementedError

    def parse_inbound(self, conn: Connection, request: Any) -> Any:
        raise NotImplementedError

    def catalogue_reader(self, conn: Connection, secrets: Any) -> Any:
        raise NotImplementedError


class MemoryOutbox:
    """MessageOutbox over a dict; a message carrying a delivery mirrors its status there."""

    def __init__(self, db: "MemoryQuotations") -> None:
        self.db = db
        self.messages: dict[UUID, OutboundMessage] = {}
        self.delivery_of: dict[UUID, UUID] = {}

    def enqueue(self, message: NewMessage) -> UUID:
        for existing in self.messages.values():
            if (existing.tenant_id, existing.idempotency_key) == (
                message.tenant_id,
                message.idempotency_key,
            ):
                return existing.message_id
        now = self.db.clock()
        links = message.links
        stored = OutboundMessage(
            uuid4(), message.tenant_id, message.connection_id, message.provider_key,
            message.purpose, message.template_key, dict(message.params), message.recipient,
            message.idempotency_key, message.body_text, dict(message.metadata),
            MessageStatus.PENDING, links.document_id, links.quotation_id, links.version,
            links.approval_task_id, links.binding_id, links.enquiry_id, links.response_id,
            message.correlation_id, None, None, now, now,
        )  # fmt: skip
        self.messages[stored.message_id] = stored
        self.db.jobs.append(
            Job(message.tenant_id, JobKind.SEND_MESSAGE, {"message_id": str(stored.message_id)})
        )
        if links.delivery_id is not None:
            self.delivery_of[stored.message_id] = links.delivery_id
            self.db.delivery_messages[links.delivery_id] = stored.message_id
            tenant, record = self.db.delivery_rows[links.delivery_id]
            self.db.delivery_rows[links.delivery_id] = (
                tenant,
                replace(record, document_id=links.document_id),
            )
        return stored.message_id

    def get(self, tenant_id: UUID, message_id: UUID) -> OutboundMessage | None:
        found = self.messages.get(message_id)
        return found if found is not None and found.tenant_id == tenant_id else None

    def resend(self, tenant_id: UUID, message_id: UUID, actor: str) -> UUID | None:
        found = self.get(tenant_id, message_id)
        if (
            found is None
            or found.status is not MessageStatus.FAILED
            or found.purpose is not Purpose.CUSTOMER_MESSAGES
        ):
            return None
        key = f"resend:{message_id}"
        for existing in self.messages.values():
            if (existing.tenant_id, existing.idempotency_key) == (tenant_id, key):
                return existing.message_id
        now = self.db.clock()
        copy = replace(
            found,
            message_id=uuid4(),
            params={**found.params, "resent_by": actor},
            idempotency_key=key,
            status=MessageStatus.PENDING,
            provider_message_id=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        self.messages[copy.message_id] = copy
        self.db.jobs.append(
            Job(tenant_id, JobKind.SEND_MESSAGE, {"message_id": str(copy.message_id)})
        )
        return copy.message_id

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
        found = self.get(tenant_id, message_id)
        if found is None or found.status not in expected:
            return False
        self.messages[message_id] = replace(
            found,
            status=status,
            last_error=error,
            provider_message_id=provider_message_id or found.provider_message_id,
            updated_at=self.db.clock(),
        )
        delivery_id = self.delivery_of.get(message_id)
        if delivery_id is not None:
            target = (
                DeliveryStatus.SENT
                if status is MessageStatus.DELIVERED
                else DeliveryStatus(status.value)
            )
            record = self.db.delivery_rows[delivery_id][1]
            self.db.set_status(
                tenant_id,
                delivery_id,
                target,
                expected=frozenset({record.status}),
                provider_reference=provider_message_id,
                error=error,
            )
        return True

    def apply_receipt(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        provider_message_id: str,
        status: MessageStatus,
        detail: str | None,
    ) -> bool:
        raise NotImplementedError


class MemoryConnections:
    """ConnectionStore + HookLookup over a dict; each demo business has its simulated phone."""

    def __init__(self, now: datetime, *tenants: UUID) -> None:
        self.rows: dict[UUID, Connection] = {}
        self.token_hashes: dict[UUID, str | None] = {}
        for tenant_id in tenants:
            for provider, account, purpose in (
                ("api", "api-key:00000000-0000-4000-8000-0000000000c1", Purpose.CUSTOMER_MESSAGES),
                ("email", "shop-email", Purpose.CUSTOMER_MESSAGES),
            ):
                self.add(
                    Connection(
                        memory_connection_id(tenant_id, provider, account),
                        tenant_id,
                        provider,
                        provider,
                        frozenset({purpose}),
                        {},
                        {},
                        account,
                        ConnectionStatus.ACTIVE,
                        None,
                        "test",
                        now,
                        now,
                    )  # fmt: skip
                )
            self.add(
                Connection(
                    memory_connection_id(tenant_id, "simulated", "simulated-local"),
                    tenant_id,
                    "simulated",
                    "Simulated phone",
                    frozenset({Purpose.MANAGER_APPROVALS, Purpose.CATALOGUE}),
                    {"catalogue": "industrial"},
                    {},
                    "simulated-local",
                    ConnectionStatus.ACTIVE,
                    None,
                    "seed",
                    now,
                    now,
                )  # fmt: skip
            )

    def add(self, connection: Connection, token_sha256: str | None = None) -> Connection:
        self.rows[connection.connection_id] = connection
        self.token_hashes[connection.connection_id] = token_sha256
        return connection

    def get(self, tenant_id: UUID, connection_id: UUID) -> Connection | None:
        row = self.rows.get(connection_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    def find_by_account(
        self, tenant_id: UUID, provider_key: str, account_ref: str
    ) -> Connection | None:
        wanted = (tenant_id, provider_key, account_ref)
        return next(
            (
                row
                for row in self.rows.values()
                if (row.tenant_id, row.provider_key, row.account_ref) == wanted
            ),
            None,
        )

    def list_for(self, tenant_id: UUID, purpose: Purpose | None = None) -> list[Connection]:
        return [
            row
            for row in self.rows.values()
            if row.tenant_id == tenant_id and (purpose is None or purpose in row.purposes)
        ]

    def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
        return [row for row in self.list_for(tenant_id, Purpose.CATALOGUE) if row.active]

    def find_for_hook(self, connection_id: UUID) -> tuple[Connection, str | None] | None:
        row = self.rows.get(connection_id)
        return None if row is None else (row, self.token_hashes[connection_id])


@dataclass
class InboxRow:
    inbox_event_id: UUID
    connection_id: UUID
    event: InboundEvent
    processed_at: datetime | None = None
    outcome: str | None = None


class MemoryInbox:
    """InboundStore: dedupe on (connection, provider event ID, type); jobs go to the queue."""

    def __init__(self, connections: MemoryConnections, jobs: list["Job"]) -> None:
        self.connections = connections
        self.jobs = jobs
        self.rows: dict[tuple[UUID, str, InboundEventType], InboxRow] = {}

    def record(
        self,
        connection: Connection,
        events: Sequence[InboundEvent],
        received_at: datetime,
        *,
        queue_jobs: bool,
    ) -> list[RecordedEvent]:
        recorded = []
        for event in events:
            key = (connection.connection_id, event.provider_event_id, event_type(event))
            if key in self.rows:
                recorded.append(RecordedEvent(self.rows[key].inbox_event_id, new=False))
                continue
            row = InboxRow(uuid4(), connection.connection_id, event)
            self.rows[key] = row
            if queue_jobs:
                payload = {"inbox_event_id": str(row.inbox_event_id)}
                self.jobs.append(Job(connection.tenant_id, JobKind.PROCESS_INBOUND, payload))
            recorded.append(RecordedEvent(row.inbox_event_id, new=True))
        return recorded

    def _row(self, inbox_event_id: UUID) -> InboxRow | None:
        return next(
            (row for row in self.rows.values() if row.inbox_event_id == inbox_event_id), None
        )

    def load(self, tenant_id: UUID, inbox_event_id: UUID) -> StoredEvent | None:
        row = self._row(inbox_event_id)
        if row is None:
            return None
        connection = self.connections.get(tenant_id, row.connection_id)
        if connection is None:
            return None
        return StoredEvent(row.inbox_event_id, connection, row.event, row.processed_at, row.outcome)

    def mark_processed(self, tenant_id: UUID, inbox_event_id: UUID, outcome: str) -> None:
        row = self._row(inbox_event_id)
        if row is not None and row.processed_at is None:
            row.processed_at, row.outcome = datetime.now(UTC), outcome


@dataclass
class QuotationHarness:
    """The real M4 services, graph and job handlers over MemoryQuotations."""

    db: MemoryQuotations
    erp: DemoCatalogue
    pricing: QuotationPricingService
    checkpointer: InMemorySaver
    notifier: RecordingProvider  # the managers' simulated phones
    approvals: ApprovalService
    quotations: QuotationService
    channels: ChannelActionService
    tenant_id: UUID
    clock: Callable[[], datetime]
    storage: ObjectStorage
    now: list[datetime]  # mutable "current time" behind `clock`
    connections: MemoryConnections
    inbox: MemoryInbox
    outbox: MemoryOutbox
    email: RecordingProvider  # a push provider for customers
    renderer: ReportLabQuotationRenderer = field(default_factory=ReportLabQuotationRenderer)
    # Business status per tenant (phase 2); unlisted businesses are live.
    business_status: dict[UUID, TenantStatus] = field(default_factory=dict)

    def status(self, tenant_id: UUID) -> TenantStatus:
        return self.business_status.get(tenant_id, TenantStatus.ACTIVE)

    @property
    def registry(self) -> ProviderRegistry:
        return ProviderRegistry([ApiProvider(), self.email, self.notifier])

    def outbound(self) -> OutboundService:
        return OutboundService(self.outbox)

    def deliveries(self) -> QuotationDeliveryHandler:
        return QuotationDeliveryHandler(
            self.db, self.storage, self.renderer, self.connections, self.outbound(),
            clock=self.clock,
        )  # fmt: skip

    def sender(self) -> SendMessageHandler:
        hooks = {
            TemplateKey.APPROVAL_REQUEST: ApprovalRequestHook(self.db, clock=self.clock),
            TemplateKey.QUOTATION_DOCUMENT: QuotationDocumentHook(self.db, self.storage),
        }
        return SendMessageHandler(
            self.outbox,
            self.connections,
            self.registry,
            None,  # type: ignore[arg-type]
            hooks,  # type: ignore[arg-type]
            self,
        )

    def send(self, message_id: UUID, attempt: int = 1) -> None:
        """Run the send_message job for one message (dropping its queued job)."""
        payload = {"message_id": str(message_id)}
        self.db.jobs[:] = [
            job for job in self.db.jobs
            if not (job.kind is JobKind.SEND_MESSAGE and job.payload == payload)
        ]  # fmt: skip
        self.sender()(ClaimedJob(uuid4(), self.tenant_id, JobKind.SEND_MESSAGE, payload, attempt,
                                 5, uuid4(), uuid4()))  # fmt: skip

    def deliver(self, delivery_id: UUID, attempt: int = 1) -> None:
        """deliver_quotation, then the send_message job of the message it queued."""
        payload = {"delivery_id": str(delivery_id)}
        self.deliveries()(ClaimedJob(uuid4(), self.tenant_id, JobKind.DELIVER_QUOTATION, payload,
                                     attempt, 5, uuid4(), uuid4()))  # fmt: skip
        message_id = self.db.delivery_messages.get(delivery_id)
        if message_id is not None:
            self.send(message_id, attempt)

    def documents(self) -> QuotationDocumentService:
        return QuotationDocumentService(self.db, self.storage, self.renderer, clock=self.clock)

    def responses(self) -> QuotationResponseService:
        return QuotationResponseService(self.db, self.pricing, clock=self.clock)

    def advance(self, delta: timedelta) -> None:
        """Move the harness clock (expiry tests). ERP prices keep their own fetch time."""
        self.now[0] += delta

    def handler(self) -> QuotationJobHandler:
        """A fresh graph on the same checkpoints, as after a worker restart."""
        graph = build_quotation_graph(
            QuotationWorkflowDeps(self.db, self.pricing, self.db), self.checkpointer
        )
        return QuotationJobHandler(graph, self.db)

    def notifications(self) -> ApprovalNotificationHandler:
        return ApprovalNotificationHandler(self.db, self.connections, self.outbound())

    def enquiry(self, *lines: tuple[str, str, str], tenant_id: UUID | None = None) -> UUID:
        enquiry_id = uuid4()
        tenant = tenant_id or self.tenant_id
        self.db.extraction[(tenant, enquiry_id)] = [
            RequestedLine(sku, Decimal(quantity), unit) for sku, quantity, unit in lines
        ]
        self.db.jobs.append(Job(tenant, JobKind.DRAFT_QUOTATION, {"enquiry_id": str(enquiry_id)}))
        return enquiry_id

    def run_jobs(self) -> int:
        """Run due jobs (and the jobs they queue) until none are due; delayed ones stay."""
        handled = 0
        while self.run_one():
            handled += 1
        return handled

    def run_one(self) -> bool:
        """Run the first due job; False if none is due."""
        due = [job for job in self.db.jobs if job.run_at is None or job.run_at <= self.clock()]
        if not due:
            return False
        job = due[0]
        self.db.jobs.remove(job)
        claimed = ClaimedJob(uuid4(), job.tenant_id, job.kind, job.payload, 1, 5, uuid4(),
                             uuid4())  # fmt: skip
        if job.kind is JobKind.NOTIFY_APPROVERS:
            self.notifications()(claimed)
        elif job.kind is JobKind.DELIVER_QUOTATION:
            self.deliveries()(claimed)
        elif job.kind is JobKind.EXPIRE_QUOTATION:
            QuotationExpiryHandler(self.responses())(claimed)
        elif job.kind is JobKind.NOTIFY_RESPONSE:
            ResponseNotificationHandler(self.db, self.db, self.connections, self.outbound())(
                claimed
            )
        elif job.kind is JobKind.SEND_MESSAGE:
            self.sender()(claimed)
        elif job.kind is JobKind.PROCESS_INBOUND:
            ProcessInboundHandler(self.inbox, routes_for(ManagerActionRoute(self.channels)))(
                claimed
            )
        else:
            self.handler()(claimed)
        return True

    def quotation(self, enquiry_id: UUID) -> QuotationRow:
        return next(row for row in self.db.quotations.values() if row.enquiry_id == enquiry_id)

    def pending_task(self, enquiry_id: UUID) -> TaskRow:
        quotation = self.quotation(enquiry_id)
        return next(
            task
            for task in self.db.tasks.values()
            if task.quotation_id == quotation.quotation_id and task.status is TaskStatus.PENDING
        )

    def command(
        self,
        task: TaskRow,
        action: ApprovalAction = ApprovalAction.APPROVE,
        reason: str | None = None,
        command_id: UUID | None = None,
        content_sha256: str | None = None,
    ) -> DecisionCommand:
        return DecisionCommand(
            command_id=command_id or uuid4(),
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            content_sha256=content_sha256 or task.content_sha256,
            action=action,
            reason=reason,
            transport="portal",
            evidence_ref=None,
            received_at=self.clock(),
        )

    def context(self, subject: str, tenant_id: UUID | None = None) -> TenantContext:
        return TenantContextResolver(self.db).resolve(subject, tenant_id or self.tenant_id)


@pytest.fixture
def harness(
    quotation_db: MemoryQuotations,
    demo_catalogue: DemoCatalogue,
    policy_reader: Any,
    now: datetime,
    industrial_tenant: UUID,
    office_tenant: UUID,
    memory_storage: ObjectStorage,
) -> QuotationHarness:
    current = [now]

    def clock() -> datetime:
        return current[0]

    quotation_db.clock = clock
    resolver = TenantContextResolver(quotation_db)
    pricing = QuotationPricingService(demo_catalogue, policy_reader, clock=clock)
    approvals = ApprovalService(quotation_db, resolver, clock=clock)
    connections = MemoryConnections(now, industrial_tenant, office_tenant)
    notifier = RecordingProvider("simulated", Purpose.MANAGER_APPROVALS)
    email = RecordingProvider("email", Purpose.CUSTOMER_MESSAGES)
    return QuotationHarness(
        db=quotation_db,
        erp=demo_catalogue,
        pricing=pricing,
        checkpointer=InMemorySaver(),
        notifier=notifier,
        approvals=approvals,
        quotations=QuotationService(quotation_db, quotation_db, pricing),
        channels=ChannelActionService(
            quotation_db,
            approvals,
            connections,
            ProviderRegistry([ApiProvider(), email, notifier]),
            clock=clock,
        ),
        tenant_id=industrial_tenant,
        clock=clock,
        storage=memory_storage,
        now=current,
        connections=connections,
        inbox=MemoryInbox(connections, quotation_db.jobs),
        outbox=MemoryOutbox(quotation_db),
        email=email,
    )


@pytest.fixture
def memory_sessions(quotation_db: MemoryQuotations) -> MemorySessions:
    return MemorySessions(quotation_db)


@pytest.fixture
def memory_directory(quotation_db: MemoryQuotations) -> MemoryDirectory:
    return MemoryDirectory(quotation_db)


class NoPeople:
    """A user store without stored people (the dev-login demo people are subjects only)."""

    def by_subject(self, subject: str) -> None:
        return None


@pytest.fixture
def session_view(memory_directory: MemoryDirectory) -> Any:
    from sales_platform.accounts.api.auth import SessionView
    from sales_platform.accounts.application.accounts import ProfileService

    return SessionView(memory_directory, ProfileService(NoPeople()))  # type: ignore[arg-type]


@pytest.fixture
def make_image() -> Callable[[str, int, int], bytes]:
    """A small synthetic image file (format "PNG" or "JPEG") for logo and PDF tests."""

    def make(image_format: str, width: int, height: int) -> bytes:
        from io import BytesIO

        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (width, height), (20, 90, 160)).save(output, format=image_format)
        return output.getvalue()

    return make
