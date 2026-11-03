"""Customer answers to an issued quotation (M6; decisions in ADR 0016).

An issued quotation is an approved version whose PDF reached the customer. Until its validity
ends, the customer may accept or decline exactly that version (named by version number and
content hash). Accepting re-prices the lines from the ERP first: any price difference refuses the
acceptance and the system drafts a revised version for manager approval. Staff may withdraw an
unanswered quotation. After "valid until" it expires and stays closed.

M6 records acceptance only; it creates no ERP order and reserves no stock.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sales_platform.platform.domain.validation import require_aware
from sales_platform.quotations.domain.quotations import QuotationStatus, clean_reason

DEFAULT_QUOTE_VALIDITY_DAYS = 7  # user decision 2026-10-23; each business can change it
SYSTEM_SUBJECT = "system:quotation-workflow"


class ResponseAction(StrEnum):
    """What the customer asks for."""

    ACCEPT = "accept"
    DECLINE = "decline"


class ResponseOutcome(StrEnum):
    """What was recorded. Every outcome except refused_changed closes the quotation."""

    ACCEPTED = "accepted"
    DECLINED = "declined"
    REFUSED_CHANGED = "refused_changed"  # prices changed: a revised version goes to approval
    WITHDRAWN = "withdrawn"  # by staff
    EXPIRED = "expired"  # validity ended without an answer


QUOTATION_STATUS_FOR: Mapping[ResponseOutcome, QuotationStatus] = {
    ResponseOutcome.ACCEPTED: QuotationStatus.ACCEPTED,
    ResponseOutcome.DECLINED: QuotationStatus.DECLINED,
    ResponseOutcome.REFUSED_CHANGED: QuotationStatus.REVISION_SUBMITTED,
    ResponseOutcome.WITHDRAWN: QuotationStatus.WITHDRAWN,
    ResponseOutcome.EXPIRED: QuotationStatus.EXPIRED,
}


class CustomerStatus(StrEnum):
    """What the customer's system sees for its enquiry's issued quotation."""

    OPEN = "open"  # can be accepted or declined until valid_until
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    BEING_REVISED = "being_revised"  # prices changed; a new version is being approved
    CLOSED = "closed"  # e.g. the manager rejected the revised version


_REVISING = frozenset(
    {
        QuotationStatus.REVISION_SUBMITTED,
        QuotationStatus.PENDING_APPROVAL,
        QuotationStatus.REVISION_REQUESTED,
        QuotationStatus.PRICING_FAILED,
    }
)


def customer_status(
    status: QuotationStatus,
    offered_version: int,
    current_version: int | None,
    valid_until: datetime,
    now: datetime,
) -> CustomerStatus:
    """Status of the offer the customer holds (the latest delivered version)."""
    if status is QuotationStatus.APPROVED and current_version == offered_version:
        return CustomerStatus.EXPIRED if now >= valid_until else CustomerStatus.OPEN
    if status in _REVISING:
        return CustomerStatus.BEING_REVISED
    named = {
        QuotationStatus.ACCEPTED: CustomerStatus.ACCEPTED,
        QuotationStatus.DECLINED: CustomerStatus.DECLINED,
        QuotationStatus.EXPIRED: CustomerStatus.EXPIRED,
        QuotationStatus.WITHDRAWN: CustomerStatus.WITHDRAWN,
    }
    # A newer version approved but not yet delivered is still "being revised" for the customer.
    if status is QuotationStatus.APPROVED:
        return CustomerStatus.BEING_REVISED
    return named.get(status, CustomerStatus.CLOSED)


def valid_until(prepared_at: datetime, days: int) -> datetime:
    """End of validity: the moment the PDF is prepared for delivery plus the tenant's days."""
    require_aware(prepared_at, "prepared_at")
    if days < 1:
        raise ValueError("Quotation validity must be at least one day")
    return prepared_at + timedelta(days=days)


class OfferNotOpen(Exception):
    """The quotation cannot be answered any more (answered, expired, withdrawn, revising...)."""

    def __init__(self, status: CustomerStatus) -> None:
        super().__init__(f"This quotation is {status.value.replace('_', ' ')}")
        self.status = status


class NotCurrentOffer(Exception):
    """The answer names a version or content hash that is not the offer the customer holds."""


class ResponseConflict(Exception):
    """The idempotency key was already used for a different answer."""


class PricesChanged(Exception):
    """Acceptance refused: current ERP prices differ. A revised version goes to approval."""

    def __init__(self, differences: tuple[str, ...], revised_version: int | None) -> None:
        super().__init__("Prices changed since this quotation was issued; it is being revised")
        self.differences = differences
        self.revised_version = revised_version


def _lines(content: Mapping[str, object]) -> list[Mapping[str, object]]:
    lines = content.get("lines")
    assert isinstance(lines, list)
    return lines


def price_differences(
    quoted: Mapping[str, object], current: Mapping[str, object]
) -> tuple[str, ...]:
    """Every commercial difference between the quoted and the re-priced content.

    Compares exactly the amounts the customer sees (as stored strings, no rounding): currency,
    each line's SKU, quantity, unit, unit price and line total, then subtotal, tax and total.
    Stock is deliberately not compared (user decision: stock is ignored for acceptance).
    """
    differences: list[str] = []
    if quoted["currency"] != current["currency"]:
        differences.append(f"currency {quoted['currency']} -> {current['currency']}")
    old_lines, new_lines = _lines(quoted), _lines(current)
    if len(old_lines) != len(new_lines):
        differences.append(f"line count {len(old_lines)} -> {len(new_lines)}")
    for old, new in zip(old_lines, new_lines, strict=False):
        for field in ("sku", "quantity", "unit", "unit_price", "line_total"):
            if old[field] != new[field]:
                differences.append(f"{old['sku']} {field} {old[field]} -> {new[field]}")
    for field in ("subtotal", "tax", "total"):
        if quoted[field] != current[field]:
            differences.append(f"{field} {quoted[field]} -> {current[field]}")
    return tuple(differences)


@dataclass(frozen=True)
class ResponseRequest:
    """A customer's answer, exactly as sent."""

    action: ResponseAction
    version: int
    content_sha256: str
    reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", clean_reason(self.reason))
        if self.action is ResponseAction.ACCEPT and self.reason is not None:
            raise ValueError("A reason can only be given when declining")

    def fingerprint(self) -> str:
        """A replay must match this; a different answer under the same key conflicts."""
        canonical = json.dumps(
            [self.action.value, self.version, self.content_sha256, self.reason],
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecordedResponse:
    response_id: UUID
    quotation_id: UUID
    version: int
    content_sha256: str
    outcome: ResponseOutcome
    actor: str
    reason: str | None
    differences: tuple[str, ...]
    revised_version: int | None
    created_at: datetime


@dataclass(frozen=True)
class Offer:
    """The latest issued version the customer holds, with the quotation's current state."""

    tenant_id: UUID
    tenant_name: str
    quotation_id: UUID
    enquiry_id: UUID
    status: QuotationStatus
    current_version: int | None
    version: int
    content: Mapping[str, object]
    content_sha256: str
    valid_until: datetime
    issued_at: datetime | None

    def customer_status(self, now: datetime) -> CustomerStatus:
        return customer_status(
            self.status, self.version, self.current_version, self.valid_until, now
        )
