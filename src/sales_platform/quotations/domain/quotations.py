"""Quotation versions, approval decisions and review packets.

A quotation version is an immutable snapshot of exactly what a manager reviews: lines, prices,
evidence, stock status and totals. Its SHA-256 content hash binds an approval to that exact
content, so a decision made on an old or changed version can never apply to a new one.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sales_platform.platform.domain.tenancy import PermissionDenied
from sales_platform.platform.domain.validation import require_aware, require_text
from sales_platform.quotations.domain.pricing import PricedQuotation, PricingIssue

CONTENT_SCHEMA = 1
MAX_REASON_CHARACTERS = 1000
_SHA256 = re.compile(r"[0-9a-f]{64}")


class QuotationStatus(StrEnum):
    PRICING_FAILED = "pricing_failed"  # ERP facts could not price the request; needs a revision
    PENDING_APPROVAL = "pending_approval"
    REVISION_REQUESTED = "revision_requested"
    REVISION_SUBMITTED = "revision_submitted"  # new version stored; approval request follows
    APPROVED = "approved"  # by a manager; issued to the customer once delivered (M5)
    REJECTED = "rejected"
    # Customer answers and closure (M6, ADR 0016):
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


# A sales operator may submit a new version only in these states.
REVISABLE = frozenset({QuotationStatus.PRICING_FAILED, QuotationStatus.REVISION_REQUESTED})


class ApprovalAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REVISION = "request_revision"


class TaskStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"


TASK_STATUS_AFTER: Mapping[ApprovalAction, TaskStatus] = {
    ApprovalAction.APPROVE: TaskStatus.APPROVED,
    ApprovalAction.REJECT: TaskStatus.REJECTED,
    ApprovalAction.REQUEST_REVISION: TaskStatus.REVISION_REQUESTED,
}
QUOTATION_STATUS_AFTER: Mapping[ApprovalAction, QuotationStatus] = {
    ApprovalAction.APPROVE: QuotationStatus.APPROVED,
    ApprovalAction.REJECT: QuotationStatus.REJECTED,
    ApprovalAction.REQUEST_REVISION: QuotationStatus.REVISION_REQUESTED,
}


PORTAL_TRANSPORT = "portal"


class QuotationNotFound(Exception):
    """Unknown quotation or one belonging to another tenant (same message for both)."""


class TaskNotFound(Exception):
    """Unknown approval task or one belonging to another tenant (same message for both)."""


class StaleVersion(Exception):
    """The decision was made on content that is no longer the current version."""


class SelfApprovalNotAllowed(PermissionDenied):
    """The person who created a version cannot approve their own version."""


class RevisionNotAllowed(Exception):
    """The quotation is not waiting for a revision (e.g. it is pending approval or closed)."""


class RevisionConflict(Exception):
    """Someone else revised the quotation first; reload and try again."""


@dataclass(frozen=True)
class RecordedDecision:
    decision_id: UUID
    command_id: UUID
    task_id: UUID
    quotation_id: UUID
    version: int
    action: ApprovalAction
    actor_subject: str
    reason: str | None
    transport: str
    decided_at: datetime


class DecisionConflict(Exception):
    """The task already has a different decision, or the command ID was reused differently."""

    def __init__(self, recorded: RecordedDecision | None) -> None:
        super().__init__("This approval task already has a different decision")
        self.recorded = recorded


def clean_reason(reason: str | None) -> str | None:
    if reason is None or not reason.strip():
        return None
    reason = reason.strip()
    if len(reason) > MAX_REASON_CHARACTERS:
        raise ValueError(f"Reason must be at most {MAX_REASON_CHARACTERS} characters")
    return reason


@dataclass(frozen=True)
class DecisionCommand:
    """One manager decision, the same shape for every entry point (portal or channel).

    The tenant comes from the server (session URL membership or channel binding), never from
    message text. content_sha256 is the hash the manager saw; a mismatch means stale content.
    """

    command_id: UUID
    tenant_id: UUID
    task_id: UUID
    content_sha256: str
    action: ApprovalAction
    reason: str | None
    transport: str
    evidence_ref: str | None
    received_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.received_at, "received_at")
        require_text(self.transport, "transport", 32)
        if not _SHA256.fullmatch(self.content_sha256):
            raise ValueError("content_sha256 must be 64 lowercase hex characters")
        object.__setattr__(self, "reason", clean_reason(self.reason))
        if self.action is not ApprovalAction.APPROVE and self.reason is None:
            raise ValueError("A reason is required to reject or request a revision")
        if self.evidence_ref is not None:
            require_text(self.evidence_ref, "evidence_ref", 300)


def quotation_content(priced: PricedQuotation) -> dict[str, object]:
    """Serialise a priced draft into the stored, hashed version content.

    Decimals become strings so no precision is lost; timestamps are ISO 8601 in UTC offset form.
    """
    policy = priced.policy
    return {
        "schema": CONTENT_SCHEMA,
        "currency": policy.currency,
        "policy": {
            "version": policy.version,
            "minor_units": policy.minor_units,
            "rounding": policy.rounding.value,
            "tax_rate": str(policy.tax_rate),
        },
        "lines": [
            {
                "line_index": line.line_index,
                "sku": line.sku,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit": line.unit,
                "unit_price": str(line.unit_price.amount),
                "line_total": str(line.line_total.amount),
                "price_list_id": line.price_evidence.price_list_id,
                "price_list_version": line.price_evidence.price_list_version,
                "price_source": line.price_evidence.source,
                "price_fetched_at": line.price_evidence.fetched_at.isoformat(),
                "stock_status": line.stock.status.value,
                "stock_available": None
                if line.stock.available_quantity is None
                else str(line.stock.available_quantity),
                "stock_note": line.stock.note,
                "stock_fetched_at": None
                if line.stock.evidence is None
                else line.stock.evidence.fetched_at.isoformat(),
            }
            for line in priced.lines
        ],
        "subtotal": str(priced.subtotal.amount),
        "tax": str(priced.tax.amount),
        "total": str(priced.total.amount),
        "stock_review_required": priced.stock_review_required,
        "calculated_at": priced.calculated_at.isoformat(),
        "explanation": list(priced.explain()),
    }


def content_sha256(content: Mapping[str, object]) -> str:
    """Hash of canonical JSON (sorted keys, no spaces), stable across processes and restarts."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pricing_issues_content(issues: tuple[PricingIssue, ...]) -> list[dict[str, object]]:
    return [
        {
            "line_index": issue.line_index,
            "sku": issue.sku,
            "code": issue.code.value,
            "message": issue.message,
        }
        for issue in issues
    ]


def quotation_number(quotation_id: UUID) -> str:
    """Short human reference derived from the ID, e.g. Q-1A2B3C4D."""
    return f"Q-{quotation_id.hex[:8].upper()}"


@dataclass(frozen=True)
class ReviewPacket:
    """Everything a manager needs to decide, independent of how it is delivered."""

    tenant_id: UUID
    tenant_name: str
    task_id: UUID
    quotation_id: UUID
    version: int
    content_sha256: str
    customer_reference: str | None
    content: Mapping[str, object]

    @property
    def number(self) -> str:
        return quotation_number(self.quotation_id)
