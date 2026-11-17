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


class ApprovalLinkLookup(Protocol):
    """The signed link knows only its token: this reveals exactly the request with that hash
    (tenant and reference), before the business is known. API role."""

    def find(self, token_sha256: str) -> tuple[UUID, UUID] | None: ...
