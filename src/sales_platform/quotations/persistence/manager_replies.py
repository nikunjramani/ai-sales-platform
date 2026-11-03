"""Approval by message on PostgreSQL (phase 5): request state, prompts, reply audit.

Both roles use it (button presses and replies are routed in the worker; the deprecated
simulated alias still runs in the API). Every query is scoped to one business.
"""

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, Row, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.integrations.persistence.tables import OutboundMessageRow
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.persistence.jobs import queue_job
from sales_platform.platform.persistence.models import Tenant
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.quotations.domain.quotations import ApprovalAction, TaskStatus, quotation_number
from sales_platform.quotations.persistence.channels import _BINDING_COLUMNS, _binding
from sales_platform.quotations.persistence.tables import (
    ApprovalActionRef,
    ApprovalDecision,
    ApprovalReplyPrompt,
    ApprovalTask,
    ChannelBindingRow,
    ManagerReplyClassification,
    Quotation,
    QuotationVersion,
)
from sales_platform.quotations.ports.channels import ChannelBinding
from sales_platform.quotations.ports.manager_replies import (
    ApprovalRequestState,
    PromptKind,
    RepliedMessage,
    ReplyPrompt,
)

_PROMPT_COLUMNS = (
    ApprovalReplyPrompt.id,
    ApprovalReplyPrompt.reference_id,
    ApprovalReplyPrompt.kind,
    ApprovalReplyPrompt.action,
    ApprovalReplyPrompt.reason,
    ApprovalReplyPrompt.expires_at,
    ApprovalReplyPrompt.closed_at,
    ApprovalReplyPrompt.outbound_message_id,
)


def _prompt(row: Row[Any]) -> ReplyPrompt:
    return ReplyPrompt(
        prompt_id=row.id,
        reference_id=row.reference_id,
        kind=PromptKind(row.kind),
        action=ApprovalAction(row.action),
        reason=row.reason,
        expires_at=row.expires_at,
        closed=row.closed_at is not None,
        outbound_message_id=row.outbound_message_id,
    )


class SqlReplyStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def binding_on(
        self, tenant_id: UUID, connection_id: UUID, external_user_id: str
    ) -> ChannelBinding | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_BINDING_COLUMNS).where(
                    ChannelBindingRow.tenant_id == tenant_id,
                    ChannelBindingRow.connection_ref == connection_id,
                    ChannelBindingRow.external_user_id == external_user_id,
                    ChannelBindingRow.revoked_at.is_(None),
                )
            ).one_or_none()
        return None if row is None else _binding(row)

    def replied_message(
        self, tenant_id: UUID, connection_id: UUID, provider_message_id: str
    ) -> RepliedMessage | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(
                    OutboundMessageRow.id,
                    OutboundMessageRow.action_ref_id,
                    OutboundMessageRow.binding_id,
                ).where(
                    OutboundMessageRow.tenant_id == tenant_id,
                    OutboundMessageRow.connection_ref == connection_id,
                    OutboundMessageRow.provider_message_id == provider_message_id,
                )
            ).one_or_none()
        return None if row is None else RepliedMessage(row.id, row.action_ref_id, row.binding_id)

    def request(self, tenant_id: UUID, reference_id: UUID) -> ApprovalRequestState | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(
                    ApprovalActionRef.id.label("reference_id"),
                    ApprovalActionRef.expires_at,
                    ApprovalActionRef.consumed_at,
                    ApprovalTask.id.label("task_id"),
                    ApprovalTask.status.label("task_status"),
                    ApprovalTask.version,
                    ApprovalTask.quotation_id,
                    Quotation.current_version,
                    QuotationVersion.content,
                    Tenant.name.label("business_name"),
                    *_BINDING_COLUMNS,
                )
                .join(ApprovalTask, ApprovalTask.id == ApprovalActionRef.task_id)
                .join(ChannelBindingRow, ChannelBindingRow.id == ApprovalActionRef.binding_id)
                .join(Quotation, Quotation.id == ApprovalTask.quotation_id)
                .join(QuotationVersion, QuotationVersion.id == ApprovalTask.version_id)
                .join(Tenant, Tenant.id == ApprovalActionRef.tenant_id)
                .where(
                    ApprovalActionRef.tenant_id == tenant_id,
                    ApprovalActionRef.id == reference_id,
                )
            ).one_or_none()
            if row is None:
                return None
            decided = connection.execute(
                select(ApprovalDecision.action).where(
                    ApprovalDecision.tenant_id == tenant_id,
                    ApprovalDecision.task_id == row.task_id,
                )
            ).scalar_one_or_none()
        content = dict(row.content)
        return ApprovalRequestState(
            reference_id=row.reference_id,
            task_id=row.task_id,
            binding=_binding(row),
            expires_at=row.expires_at,
            consumed=row.consumed_at is not None,
            task_status=TaskStatus(row.task_status),
            version=row.version,
            current_version=row.current_version or row.version,
            quotation_id=row.quotation_id,
            number=quotation_number(row.quotation_id),
            total=str(content.get("total", "")),
            currency=str(content.get("currency", "")),
            business_name=row.business_name,
            decided=None if decided is None else ApprovalAction(decided),
        )

    def open_prompt(
        self,
        tenant_id: UUID,
        reference_id: UUID,
        kind: PromptKind,
        action: ApprovalAction,
        reason: str | None,
        expires_at: datetime,
        inbox_event_id: UUID | None,
    ) -> ReplyPrompt:
        with tenant_transaction(self._engine, tenant_id) as connection:
            if inbox_event_id is not None:
                earlier = connection.execute(
                    select(*_PROMPT_COLUMNS).where(
                        ApprovalReplyPrompt.tenant_id == tenant_id,
                        ApprovalReplyPrompt.inbox_event_id == inbox_event_id,
                    )
                ).one_or_none()
                if earlier is not None:
                    return _prompt(earlier)
            connection.execute(
                update(ApprovalReplyPrompt)
                .where(
                    ApprovalReplyPrompt.tenant_id == tenant_id,
                    ApprovalReplyPrompt.reference_id == reference_id,
                    ApprovalReplyPrompt.closed_at.is_(None),
                )
                .values(closed_at=func.now(), outcome="replaced")
            )
            row = connection.execute(
                insert(ApprovalReplyPrompt)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    reference_id=reference_id,
                    kind=kind.value,
                    action=action.value,
                    reason=None if reason is None else reason[:1000],
                    expires_at=expires_at,
                    inbox_event_id=inbox_event_id,
                )
                .returning(*_PROMPT_COLUMNS)
            ).one()
        return _prompt(row)

    def prompt(self, tenant_id: UUID, prompt_id: UUID) -> ReplyPrompt | None:
        return self._prompt(tenant_id, ApprovalReplyPrompt.id == prompt_id)

    def prompt_for_message(self, tenant_id: UUID, outbound_message_id: UUID) -> ReplyPrompt | None:
        return self._prompt(
            tenant_id, ApprovalReplyPrompt.outbound_message_id == outbound_message_id
        )

    def prompt_by_token(self, tenant_id: UUID, token_sha256: str) -> ReplyPrompt | None:
        return self._prompt(tenant_id, ApprovalReplyPrompt.token_sha256 == token_sha256)

    def bind_prompt_message(
        self,
        tenant_id: UUID,
        prompt_id: UUID,
        outbound_message_id: UUID,
        token_sha256: str | None,
    ) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            changed = connection.execute(
                update(ApprovalReplyPrompt)
                .where(
                    ApprovalReplyPrompt.tenant_id == tenant_id,
                    ApprovalReplyPrompt.id == prompt_id,
                    ApprovalReplyPrompt.closed_at.is_(None),
                )
                .values(outbound_message_id=outbound_message_id, token_sha256=token_sha256)
            ).rowcount
        return bool(changed)

    def close_prompt(self, tenant_id: UUID, prompt_id: UUID, outcome: str) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            changed = connection.execute(
                update(ApprovalReplyPrompt)
                .where(
                    ApprovalReplyPrompt.tenant_id == tenant_id,
                    ApprovalReplyPrompt.id == prompt_id,
                    ApprovalReplyPrompt.closed_at.is_(None),
                )
                .values(closed_at=func.now(), outcome=outcome[:32])
            ).rowcount
        return bool(changed)

    def classified(self, tenant_id: UUID, inbox_event_id: UUID) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            found = connection.execute(
                select(ManagerReplyClassification.id).where(
                    ManagerReplyClassification.tenant_id == tenant_id,
                    ManagerReplyClassification.inbox_event_id == inbox_event_id,
                )
            ).one_or_none()
        return found is not None

    def record_classification(
        self,
        tenant_id: UUID,
        inbox_event_id: UUID,
        reference_id: UUID | None,
        text: str,
        intent: str,
        model: str,
        guard: str | None,
        outcome: str,
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                insert(ManagerReplyClassification)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    inbox_event_id=inbox_event_id,
                    reference_id=reference_id,
                    text=text[:4000],
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    intent=intent,
                    model=model[:200],
                    guard=guard,
                    outcome=outcome[:64],
                )
                .on_conflict_do_nothing(index_elements=["inbox_event_id"])
            )

    def queue_classification(
        self, tenant_id: UUID, inbox_event_id: UUID, reference_id: UUID, correlation_id: UUID
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            queue_job(
                connection,
                tenant_id,
                JobKind.CLASSIFY_MANAGER_REPLY,
                {"inbox_event_id": str(inbox_event_id), "reference_id": str(reference_id)},
                correlation_id,
            )

    def _prompt(self, tenant_id: UUID, condition: Any) -> ReplyPrompt | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_PROMPT_COLUMNS).where(
                    ApprovalReplyPrompt.tenant_id == tenant_id, condition
                )
            ).one_or_none()
        return None if row is None else _prompt(row)


class SqlApprovalLinks:
    """The approval_link_lookup policy reveals one action reference by its token's hash."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find(self, token_sha256: str) -> tuple[UUID, UUID] | None:
        with self._engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.approval_token_sha256', :h, true)"),
                {"h": token_sha256},
            )
            row = connection.execute(
                select(ApprovalActionRef.tenant_id, ApprovalActionRef.id).where(
                    ApprovalActionRef.token_sha256 == token_sha256,
                    ApprovalActionRef.sent_at.is_not(None),
                )
            ).one_or_none()
        return None if row is None else (row.tenant_id, row.id)
