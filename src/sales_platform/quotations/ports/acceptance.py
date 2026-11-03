"""Storage contract for customer answers, withdrawal and expiry (M6, ADR 0016)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.quotations.domain.acceptance import Offer, RecordedResponse, ResponseOutcome


@dataclass(frozen=True)
class RevisedContent:
    """What replaces the offer when prices changed: a priced version, or the pricing issues
    when the lines can no longer be priced (then a sales operator revises, as in M4)."""

    content: Mapping[str, object] | None
    content_sha256: str | None
    pricing_issues: Sequence[Mapping[str, object]] | None


@dataclass(frozen=True)
class NewResponse:
    outcome: ResponseOutcome
    actor: str  # API key subject, staff subject or the system
    reason: str | None
    idempotency_key: str  # unique per tenant; replays return the first result
    fingerprint: str
    differences: tuple[str, ...] = ()
    revised: RevisedContent | None = None


class ResponseStore(Protocol):
    def find_response(
        self, tenant_id: UUID, idempotency_key: str
    ) -> tuple[RecordedResponse, str] | None:
        """The response stored under this key, with the fingerprint it was stored with."""
        ...

    def offer_for_enquiry(self, tenant_id: UUID, enquiry_id: UUID, channel: str) -> Offer | None:
        """The latest version delivered (available/sent) to the customer on this channel."""
        ...

    def offer_for_quotation(self, tenant_id: UUID, quotation_id: UUID) -> Offer | None:
        """The latest delivered version of a quotation (for staff withdrawal and expiry)."""
        ...

    def record(
        self,
        tenant_id: UUID,
        offer: Offer,
        response: NewResponse,
        now: datetime,
        consume_ref: UUID | None = None,
    ) -> RecordedResponse:
        """Record an answer atomically, re-checking under a row lock that the offer is still
        open (approved, still the current version, and before valid_until; for EXPIRED: at or
        after it). Raises OfferNotOpen otherwise.

        In the same transaction: update the quotation status, store the revised version (or
        pricing issues) for REFUSED_CHANGED, queue the workflow resume and the notification
        job, and mark the customer action reference (phase 4) as used.
        """
        ...

    def response(self, tenant_id: UUID, response_id: UUID) -> RecordedResponse | None: ...

    def responses(self, tenant_id: UUID, quotation_id: UUID) -> list[RecordedResponse]:
        """All answers for a quotation, oldest first (portal history)."""
        ...
