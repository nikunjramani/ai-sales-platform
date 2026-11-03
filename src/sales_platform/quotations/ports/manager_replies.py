"""Storage for approval by message (D10, phase 5): which request a reply is about, the
questions we asked the manager (prompts) and the audit of every written reply."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sales_platform.quotations.domain.quotations import ApprovalAction, TaskStatus
from sales_platform.quotations.ports.channels import ChannelBinding


class PromptKind(StrEnum):
    REASON = "reason"  # "reply with the reason" after a Reject/Change button
    CONFIRM = "confirm"  # "You want to ...? YES / NO" after the AI read a reply (P5-13)


@dataclass(frozen=True)
class RepliedMessage:
    """One of our messages a manager replied to, found by the tool's ID for it."""

    outbound_message_id: UUID
    reference_id: UUID | None  # the approval request it belongs to
    binding_id: UUID | None


@dataclass(frozen=True)
class ApprovalRequestState:
    """An approval request (action reference) as it is now, for checks and wording."""

    reference_id: UUID
    task_id: UUID
    binding: ChannelBinding
    expires_at: datetime
    consumed: bool
    task_status: TaskStatus
    version: int
    current_version: int
    quotation_id: UUID
    number: str
    total: str
    currency: str
    business_name: str
    decided: ApprovalAction | None


@dataclass(frozen=True)
class ReplyPrompt:
    prompt_id: UUID
    reference_id: UUID
    kind: PromptKind
    action: ApprovalAction
    reason: str | None
    expires_at: datetime
    closed: bool
    outbound_message_id: UUID | None


class ReplyStore(Protocol):
    def binding_on(
        self, tenant_id: UUID, connection_id: UUID, external_user_id: str
    ) -> ChannelBinding | None:
        """The active binding of that account on that connection."""
        ...

    def replied_message(
        self, tenant_id: UUID, connection_id: UUID, provider_message_id: str
    ) -> RepliedMessage | None: ...

    def request(self, tenant_id: UUID, reference_id: UUID) -> ApprovalRequestState | None: ...

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
        """Ask a new question about a request; an earlier open one is closed ("replaced").
        The same inbox event gets the same prompt again (a retried job)."""
        ...

    def prompt(self, tenant_id: UUID, prompt_id: UUID) -> ReplyPrompt | None: ...

    def prompt_for_message(
        self, tenant_id: UUID, outbound_message_id: UUID
    ) -> ReplyPrompt | None: ...

    def prompt_by_token(self, tenant_id: UUID, token_sha256: str) -> ReplyPrompt | None: ...

    def bind_prompt_message(
        self,
        tenant_id: UUID,
        prompt_id: UUID,
        outbound_message_id: UUID,
        token_sha256: str | None,
    ) -> bool:
        """At send time: the message carrying the question (and its button token). False if
        the prompt was closed meanwhile."""
        ...

    def close_prompt(self, tenant_id: UUID, prompt_id: UUID, outcome: str) -> bool:
        """Compare-and-set: False if it was already closed (a second answer does nothing)."""
        ...

    def classified(self, tenant_id: UUID, inbox_event_id: UUID) -> bool: ...

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
        """Append-only audit (the reply text is kept, user answer 2026-10-29)."""
        ...

    def queue_classification(
        self, tenant_id: UUID, inbox_event_id: UUID, reference_id: UUID, correlation_id: UUID
    ) -> None:
        """A classify_manager_reply job (the reader is slow and may need retries)."""
        ...


class ApprovalLinkLookup(Protocol):
    """The signed link knows only its token: this reveals exactly the request with that hash
    (tenant and reference), before the business is known. API role."""

    def find(self, token_sha256: str) -> tuple[UUID, UUID] | None: ...
