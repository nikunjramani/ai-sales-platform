"""Customer answers, staff withdrawal and expiry of issued quotations (M6, ADR 0016).

- respond: the customer's system accepts or declines the exact version it holds (API key).
  Accepting re-prices the lines from the ERP; any price difference refuses it and stores a
  revised version that goes through manager approval (the workflow resumes). ERP errors
  propagate, so nothing is recorded and the caller can retry.
- withdraw: a sales operator or approver closes an issued, unanswered quotation with a reason.
- expire: the job queued for "valid until" closes a quotation nobody answered.
- ResponseNotificationHandler tells managers' linked channels about accepted/declined answers.

Every answer is recorded once (idempotency key, append-only); a replay returns the first result.
"""

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sales_platform.conversations.ports.conversations import CustomerNotifier
from sales_platform.enquiries.domain.intake import validate_idempotency_key
from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.integrations.application.messaging import OutboundService
from sales_platform.integrations.domain.messages import TemplateKey, format_valid_until
from sales_platform.integrations.ports.connections import ConnectionStore
from sales_platform.integrations.ports.messages import MessageLinks
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.domain.tenancy import Permission, PermissionDenied, Role, TenantContext
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.quotations.application.pricing import QuotationPricingService
from sales_platform.quotations.domain.acceptance import (
    SYSTEM_SUBJECT,
    CustomerStatus,
    NotCurrentOffer,
    Offer,
    OfferNotOpen,
    PricesChanged,
    RecordedResponse,
    ResponseAction,
    ResponseConflict,
    ResponseOutcome,
    ResponseRequest,
    price_differences,
)
from sales_platform.quotations.domain.documents import DocumentNotAvailable
from sales_platform.quotations.domain.message_texts import render_response_notice
from sales_platform.quotations.domain.pricing import PricingRejected
from sales_platform.quotations.domain.quotations import (
    QuotationNotFound,
    QuotationStatus,
    clean_reason,
    content_sha256,
    pricing_issues_content,
    quotation_content,
    quotation_number,
)
from sales_platform.quotations.ports.acceptance import NewResponse, ResponseStore, RevisedContent
from sales_platform.quotations.ports.channels import NotificationStore
from sales_platform.quotations.ports.customer_actions import CustomerActionRef

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


# User decision (ADR 0016): sales operators and approvers withdraw; administrators do not.
WITHDRAWING_ROLES = frozenset({Role.SALES_OPERATOR, Role.APPROVER})


class NotIssued(Exception):
    """The quotation has not been delivered to the customer, so there is nothing to withdraw."""


class NotDueYet(Exception):
    """An expiry job ran before valid_until (e.g. clock differences). Retried later."""


class QuotationResponseService:
    def __init__(
        self,
        store: ResponseStore,
        pricing: QuotationPricingService,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._pricing = pricing
        self._clock = clock

    # --- customer ---------------------------------------------------------------------
    def offer(self, context: TenantContext, enquiry_id: UUID, channel: str) -> Offer:
        """The offer the customer holds for its enquiry (any status)."""
        context.require(Permission.VIEW_ENQUIRIES)
        offer = self._store.offer_for_enquiry(context.tenant_id, enquiry_id, channel)
        if offer is None:
            raise DocumentNotAvailable("No quotation has been issued for this enquiry yet")
        return offer

    def respond(
        self,
        context: TenantContext,
        enquiry_id: UUID,
        channel: str,
        idempotency_key: str,
        request: ResponseRequest,
    ) -> tuple[RecordedResponse, bool]:
        """Record an accept/decline; returns (response, replayed).

        Raises DocumentNotAvailable (nothing issued), NotCurrentOffer (wrong version or hash),
        OfferNotOpen (answered, expired, withdrawn, being revised), ResponseConflict (key reused
        for a different answer), PricesChanged (acceptance refused; recorded and revised) and
        ErpError (nothing recorded; retry).
        """
        context.require(Permission.RESPOND_QUOTATIONS)
        key = validate_idempotency_key(idempotency_key)
        fingerprint = request.fingerprint()
        earlier = self._replay(context.tenant_id, key, fingerprint)
        if earlier is not None:
            return earlier, True
        offer = self.offer(context, enquiry_id, channel)
        if request.version != offer.version or request.content_sha256 != offer.content_sha256:
            raise NotCurrentOffer("This is not the quotation version currently offered")
        now = self._clock()
        self._require_open(offer, now)
        if request.action is ResponseAction.DECLINE:
            response = NewResponse(
                ResponseOutcome.DECLINED, context.subject, request.reason, key, fingerprint
            )
        else:
            response = self._acceptance(offer, context.subject, key, fingerprint)
        recorded = self._store.record(context.tenant_id, offer, response, now)
        self._raise_if_refused(recorded)
        return recorded, False

    def respond_by_reference(
        self, ref: CustomerActionRef, action: ResponseAction, reason: str | None
    ) -> tuple[RecordedResponse, bool]:
        """Accept/decline from a chat button or a secure link (phase 4).

        The reference is bound to one version and content hash and is answered once: pressing
        the same action again returns the first answer (replayed); the other action after
        that raises OfferNotOpen (accepted, declined...). Same checks and re-pricing as respond().
        """
        key = f"ref:{ref.ref_id}"
        fingerprint = action.value
        found = self._store.find_response(ref.tenant_id, key)
        if found is not None and found[1] == fingerprint:
            self._raise_if_refused(found[0])
            return found[0], True
        # The other button after an answer: the offer is no longer open (checked below).
        offer = self._store.offer_for_quotation(ref.tenant_id, ref.quotation_id)
        if offer is None:
            raise DocumentNotAvailable("No quotation has been issued for this enquiry yet")
        if offer.version != ref.version or offer.content_sha256 != ref.content_sha256:
            raise NotCurrentOffer("This is not the quotation version currently offered")
        now = self._clock()
        self._require_open(offer, now)
        actor = f"customer:{ref.identity_id}"
        if action is ResponseAction.DECLINE:
            response = NewResponse(ResponseOutcome.DECLINED, actor, reason, key, fingerprint)
        else:
            response = self._acceptance(offer, actor, key, fingerprint)
        recorded = self._store.record(ref.tenant_id, offer, response, now, consume_ref=ref.ref_id)
        self._raise_if_refused(recorded)
        return recorded, False

    def status_of(self, offer: Offer) -> CustomerStatus:
        return offer.customer_status(self._clock())

    # --- staff ------------------------------------------------------------------------
    def history(
        self, context: TenantContext, quotation_id: UUID
    ) -> tuple[Offer | None, list[RecordedResponse]]:
        """The issued offer (if any) and every answer, for the portal."""
        context.require(Permission.VIEW_QUOTATIONS)
        return (
            self._store.offer_for_quotation(context.tenant_id, quotation_id),
            self._store.responses(context.tenant_id, quotation_id),
        )

    def withdraw(
        self, context: TenantContext, quotation_id: UUID, command_id: UUID, reason: str | None
    ) -> tuple[RecordedResponse, bool]:
        if not context.roles & WITHDRAWING_ROLES:
            raise PermissionDenied("Only sales operators and approvers can withdraw quotations")
        reason = clean_reason(reason)
        if reason is None:
            raise ValueError("A reason is required to withdraw a quotation")
        key = f"withdraw:{command_id}"
        fingerprint = ResponseRequest(ResponseAction.DECLINE, 0, "0" * 64, reason).fingerprint()
        earlier = self._replay(context.tenant_id, key, fingerprint)
        if earlier is not None:
            if earlier.quotation_id != quotation_id:
                raise ResponseConflict("This command ID was used for another quotation")
            return earlier, True
        offer = self._store.offer_for_quotation(context.tenant_id, quotation_id)
        if offer is None:
            raise NotIssued(
                "Only a quotation that has been issued to the customer can be withdrawn"
            )
        now = self._clock()
        self._require_open(offer, now)
        response = NewResponse(ResponseOutcome.WITHDRAWN, context.subject, reason, key, fingerprint)
        return self._store.record(context.tenant_id, offer, response, now), False

    # --- expiry job -------------------------------------------------------------------
    def expire(self, tenant_id: UUID, quotation_id: UUID, version: int) -> RecordedResponse | None:
        """Close the version if it is still unanswered at valid_until; otherwise do nothing."""
        offer = self._store.offer_for_quotation(tenant_id, quotation_id)
        if (
            offer is None
            or offer.version != version
            or offer.status is not QuotationStatus.APPROVED
            or offer.current_version != version
        ):
            return None  # answered, withdrawn, revised or already expired
        now = self._clock()
        if now < offer.valid_until:
            raise NotDueYet("The quotation is still valid")
        return self._record_expiry(offer, now)

    # --- helpers ----------------------------------------------------------------------
    def _replay(self, tenant_id: UUID, key: str, fingerprint: str) -> RecordedResponse | None:
        found = self._store.find_response(tenant_id, key)
        if found is None:
            return None
        recorded, stored_fingerprint = found
        if stored_fingerprint != fingerprint:
            raise ResponseConflict("This Idempotency-Key was already used for a different answer")
        self._raise_if_refused(recorded)
        return recorded

    def _require_open(self, offer: Offer, now: datetime) -> None:
        status = offer.customer_status(now)
        if status is CustomerStatus.OPEN:
            return
        if status is CustomerStatus.EXPIRED and offer.status is QuotationStatus.APPROVED:
            self._record_expiry(offer, now)  # validity ended before the expiry job ran
        raise OfferNotOpen(status)

    def _record_expiry(self, offer: Offer, now: datetime) -> RecordedResponse | None:
        key = f"expire:{offer.quotation_id}:{offer.version}"
        response = NewResponse(ResponseOutcome.EXPIRED, SYSTEM_SUBJECT, None, key, key)
        try:
            return self._store.record(offer.tenant_id, offer, response, now)
        except OfferNotOpen:
            return None  # answered or closed meanwhile

    def _acceptance(self, offer: Offer, actor: str, key: str, fingerprint: str) -> NewResponse:
        lines = offer.content["lines"]
        assert isinstance(lines, list)
        requested = [
            RequestedLine(str(line["sku"]), Decimal(str(line["quantity"])), str(line["unit"]))
            for line in lines
        ]
        # Re-pricing runs as the workflow's system identity: the customer never drafts.
        system = TenantContext(offer.tenant_id, SYSTEM_SUBJECT, frozenset({Role.SALES_OPERATOR}))
        outcome = self._pricing.price(system, requested)  # ErpError propagates: retry later
        if isinstance(outcome, PricingRejected):
            issues = pricing_issues_content(outcome.issues)
            return NewResponse(
                ResponseOutcome.REFUSED_CHANGED,
                actor,
                None,
                key,
                fingerprint,
                tuple(f"{issue.sku}: {issue.message}" for issue in outcome.issues),
                RevisedContent(None, None, issues),
            )
        current = quotation_content(outcome)
        differences = price_differences(offer.content, current)
        if not differences:
            return NewResponse(ResponseOutcome.ACCEPTED, actor, None, key, fingerprint)
        return NewResponse(
            ResponseOutcome.REFUSED_CHANGED,
            actor,
            None,
            key,
            fingerprint,
            differences,
            RevisedContent(current, content_sha256(current), None),
        )

    @staticmethod
    def _raise_if_refused(recorded: RecordedResponse) -> None:
        if recorded.outcome is ResponseOutcome.REFUSED_CHANGED:
            raise PricesChanged(recorded.differences, recorded.revised_version)


class QuotationExpiryHandler:
    """Job handler for expire_quotation (queued for the moment validity ends)."""

    def __init__(self, service: QuotationResponseService) -> None:
        self._service = service

    def __call__(self, job: ClaimedJob) -> None:
        try:
            quotation_id = UUID(job.payload["quotation_id"])
            version = int(job.payload["version"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid quotation_id/version") from None
        self._service.expire(job.require_tenant(), quotation_id, version)


# What the customer is told about each recorded answer (wording confirmed, Q7 and Q10).
CUSTOMER_TEMPLATE_FOR: Mapping[ResponseOutcome, TemplateKey] = {
    ResponseOutcome.ACCEPTED: TemplateKey.ANSWER_ACCEPTED,
    ResponseOutcome.DECLINED: TemplateKey.ANSWER_DECLINED,
    ResponseOutcome.REFUSED_CHANGED: TemplateKey.PRICES_CHANGED,
    ResponseOutcome.WITHDRAWN: TemplateKey.QUOTATION_WITHDRAWN,
    ResponseOutcome.EXPIRED: TemplateKey.QUOTATION_EXPIRED,
}
_MANAGER_NOTICES = frozenset({ResponseOutcome.ACCEPTED, ResponseOutcome.DECLINED})


class ResponseNotificationHandler:
    """Job handler for notify_response: announce a recorded answer.

    - Managers: one response_notice per approver binding for accept/decline (idempotency key
      response-notice:<response>:<binding>), so a retried job never repeats a notice.
    - The customer (phase 4): the matching fixed message on their own tool (key
      answer:<response>). The staff withdrawal reason is never included (Q10).
    """

    def __init__(
        self,
        responses: ResponseStore,
        bindings: NotificationStore,
        connections: ConnectionStore,
        outbound: OutboundService,
        customer: CustomerNotifier | None = None,
    ) -> None:
        self._responses = responses
        self._bindings = bindings
        self._connections = connections
        self._outbound = outbound
        self._customer = customer

    def __call__(self, job: ClaimedJob) -> None:
        try:
            response_id = UUID(job.payload["response_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid response_id") from None
        response = self._responses.response(job.require_tenant(), response_id)
        if response is None:
            raise PermanentJobError("Response not found")
        offer = self._responses.offer_for_quotation(job.require_tenant(), response.quotation_id)
        if offer is None:
            raise QuotationNotFound("Quotation not found")
        if self._customer is not None:
            self._customer.send(
                job.require_tenant(),
                offer.enquiry_id,
                CUSTOMER_TEMPLATE_FOR[response.outcome],
                {
                    "number": quotation_number(response.quotation_id),
                    "date": format_valid_until(offer.valid_until),
                },
                f"answer:{response.response_id}",
                job.correlation_id,
            )
        if response.outcome not in _MANAGER_NOTICES:
            return
        for binding in self._bindings.approver_bindings(job.require_tenant()):
            connection = self._connections.get(job.require_tenant(), binding.connection_ref)
            if connection is None or not connection.active:
                logger.warning("Binding %s has no active connection", binding.binding_id)
                continue
            self._outbound.queue(
                connection,
                template=TemplateKey.RESPONSE_NOTICE,
                render=render_response_notice(offer, response),
                params={
                    "response_id": str(response.response_id),
                    "binding_id": str(binding.binding_id),
                },
                recipient=binding.external_user_id,
                idempotency_key=f"response-notice:{response.response_id}:{binding.binding_id}",
                links=MessageLinks(
                    quotation_id=response.quotation_id,
                    version=response.version,
                    binding_id=binding.binding_id,
                    enquiry_id=offer.enquiry_id,
                    response_id=response.response_id,
                ),
                correlation_id=job.correlation_id,
            )
