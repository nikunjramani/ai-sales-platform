"""Read quotations and submit revisions (new immutable versions) for one tenant.

Revisions are re-priced by the M1 calculator from live ERP facts. A person can change lines
and quantities, never prices: prices always come from the ERP and the tenant's policy.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sales_platform.application.pricing import QuotationPricingService
from sales_platform.domain.pricing import PricingRejected, RequestedLine
from sales_platform.domain.quotations import (
    REVISABLE,
    QuotationNotFound,
    QuotationStatus,
    RevisionConflict,
    RevisionNotAllowed,
    clean_reason,
    content_sha256,
    quotation_content,
)
from sales_platform.domain.tenancy import Permission, TenantContext
from sales_platform.ports.quotations import (
    QuotationDetail,
    QuotationReader,
    QuotationRevisionStore,
    QuotationSummary,
)

MAX_LIST = 100


@dataclass(frozen=True)
class RevisionResult:
    quotation_id: UUID
    version: int
    replayed: bool


class QuotationService:
    def __init__(
        self,
        reader: QuotationReader,
        revisions: QuotationRevisionStore,
        pricing: QuotationPricingService,
    ) -> None:
        self._reader = reader
        self._revisions = revisions
        self._pricing = pricing

    def list(
        self, context: TenantContext, statuses: frozenset[QuotationStatus] | None = None
    ) -> list[QuotationSummary]:
        context.require(Permission.VIEW_QUOTATIONS)
        return self._reader.list(context.tenant_id, statuses, MAX_LIST)

    def get(self, context: TenantContext, quotation_id: UUID) -> QuotationDetail:
        context.require(Permission.VIEW_QUOTATIONS)
        detail = self._reader.get(context.tenant_id, quotation_id)
        if detail is None:
            raise QuotationNotFound("Quotation not found")
        return detail

    def revise(
        self,
        context: TenantContext,
        quotation_id: UUID,
        command_id: UUID,
        base_version: int | None,
        lines: Sequence[RequestedLine],
        reason: str | None,
    ) -> RevisionResult | PricingRejected:
        """Price the edited lines and store them as the next version.

        Returns PricingRejected (nothing stored) if the ERP facts cannot price the lines.
        Raises RevisionNotAllowed unless the quotation is waiting for a revision, and
        RevisionConflict if base_version is no longer the current version.
        """
        context.require(Permission.DRAFT_QUOTATIONS)
        if not lines:
            raise ValueError("At least one line is required")
        reason = clean_reason(reason)
        if reason is None:
            raise ValueError("Describe what changed in this revision")
        earlier = self._revisions.find_revision(context.tenant_id, command_id)
        if earlier is not None:
            if earlier[0] != quotation_id:
                raise RevisionConflict("This command ID was used for another quotation")
            return RevisionResult(earlier[0], earlier[1], replayed=True)
        detail = self.get(context, quotation_id)
        if detail.summary.status not in REVISABLE:
            raise RevisionNotAllowed("This quotation is not waiting for a revision")
        if base_version != detail.summary.current_version:
            raise RevisionConflict("The quotation changed; reload it before revising")
        outcome = self._pricing.price(context, lines)
        if isinstance(outcome, PricingRejected):
            return outcome
        content = quotation_content(outcome)
        version = self._revisions.add_revision(
            context.tenant_id,
            quotation_id,
            base_version,
            content,
            content_sha256(content),
            context.subject,
            reason,
            command_id,
        )
        return RevisionResult(quotation_id, version, replayed=False)
