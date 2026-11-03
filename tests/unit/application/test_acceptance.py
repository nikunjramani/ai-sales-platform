"""Customer answers (M6, ADR 0016): accept, decline, price re-check with automatic revision,
expiry, withdrawal, notifications, replays and tenant boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.adapters.erp.simulated import SimulatedPrice
from sales_platform.application.acceptance import NotDueYet, NotIssued
from sales_platform.application.channel_actions import ChannelEvent, ChannelEventKind
from sales_platform.domain.acceptance import (
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
)
from sales_platform.domain.documents import DocumentNotAvailable
from sales_platform.domain.jobs import JobKind
from sales_platform.domain.messages import TemplateKey, render_template
from sales_platform.domain.quotations import ApprovalAction, QuotationStatus
from sales_platform.domain.tenancy import PermissionDenied, Role, TenantContext
from sales_platform.ports.erp import ErpError, ErpErrorCode

FAN = ("IND-FAN-450", "2", "each")
MANAGER = "manager@industrial.example"
SALES = "sales@industrial.example"
VIEWER = "viewer@industrial.example"


def customer(harness: Any, tenant_id: UUID | None = None) -> TenantContext:
    return TenantContext(
        tenant_id or harness.tenant_id,
        "api-key:00000000-0000-4000-8000-0000000000c1",
        frozenset({Role.INTAKE_SERVICE}),
    )


def issue(harness: Any) -> tuple[UUID, Offer]:
    """Draft, approve and deliver a quotation; return the enquiry and the customer's offer."""
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()
    harness.approvals.decide(MANAGER, harness.command(harness.pending_task(enquiry)))
    harness.run_jobs()
    return enquiry, harness.responses().offer(customer(harness), enquiry, "api")


def answer(
    harness: Any,
    enquiry: UUID,
    offer: Offer,
    action: ResponseAction = ResponseAction.ACCEPT,
    reason: str | None = None,
    key: str = "answer-0001",
) -> Any:
    request = ResponseRequest(action, offer.version, offer.content_sha256, reason)
    return harness.responses().respond(customer(harness), enquiry, "api", key, request)


def link_manager_phone(harness: Any) -> None:
    code = harness.channels.start_enrollment(harness.context(MANAGER), "simulated").code
    harness.channels.handle(
        ChannelEvent(
            channel="simulated",
            connection_id="simulated-local",
            tenant_id=harness.tenant_id,
            event_id="enroll-phone",
            external_user_id="manager-phone",
            kind=ChannelEventKind.ENROLL,
            code=code,
        )
    )


def set_fan_price(harness: Any, price: str | None) -> None:
    catalogue = harness.erp.catalogue(harness.tenant_id)
    if price is None:
        catalogue.prices.pop("IND-FAN-450")
        return
    [current] = catalogue.prices["IND-FAN-450"][-1:]
    catalogue.prices["IND-FAN-450"] = (
        SimulatedPrice(Decimal(price), "INR", current.price_list_id, "99", current.effective_from),
    )


def test_customer_accepts_unchanged_prices_and_managers_are_told(harness: Any) -> None:
    link_manager_phone(harness)
    enquiry, offer = issue(harness)
    assert harness.responses().status_of(offer) is CustomerStatus.OPEN
    assert offer.valid_until == harness.clock() + timedelta(days=7)

    recorded, replayed = answer(harness, enquiry, offer)
    assert recorded.outcome is ResponseOutcome.ACCEPTED and not replayed
    assert recorded.actor == "api-key:00000000-0000-4000-8000-0000000000c1"
    assert harness.quotation(enquiry).status is QuotationStatus.ACCEPTED
    harness.run_jobs()  # resume the run, notify managers
    run = harness.db.runs[(harness.tenant_id, enquiry, "quotation-approval-v1")]
    assert harness.db.run_status[run] == "accepted"
    [(recipient, notice_id, text)] = harness.notifier.notices
    assert recipient == "manager-phone" and notice_id == recorded.response_id
    assert "the customer accepted quotation Q-" in text and "version 1" in text

    again, replayed = answer(harness, enquiry, offer)  # network retry: same key, same answer
    assert replayed and again == recorded
    with pytest.raises(ResponseConflict):
        answer(harness, enquiry, offer, ResponseAction.DECLINE)  # same key, different answer
    with pytest.raises(OfferNotOpen) as closed:
        answer(harness, enquiry, offer, key="answer-0002")
    assert closed.value.status is CustomerStatus.ACCEPTED


def test_customer_declines_with_a_reason(harness: Any) -> None:
    link_manager_phone(harness)
    enquiry, offer = issue(harness)
    recorded, _ = answer(harness, enquiry, offer, ResponseAction.DECLINE, "Found a cheaper offer")
    assert recorded.outcome is ResponseOutcome.DECLINED
    assert harness.quotation(enquiry).status is QuotationStatus.DECLINED
    harness.run_jobs()
    assert "declined" in harness.notifier.notices[0][2]
    assert "Reason: Found a cheaper offer" in harness.notifier.notices[0][2]


def test_only_the_exact_offered_version_and_hash_can_be_answered(harness: Any) -> None:
    enquiry, offer = issue(harness)
    wrong = ResponseRequest(ResponseAction.ACCEPT, offer.version, "f" * 64, None)
    with pytest.raises(NotCurrentOffer):
        harness.responses().respond(customer(harness), enquiry, "api", "answer-0001", wrong)
    old = ResponseRequest(ResponseAction.ACCEPT, offer.version + 1, offer.content_sha256, None)
    with pytest.raises(NotCurrentOffer):
        harness.responses().respond(customer(harness), enquiry, "api", "answer-0002", old)
    assert harness.db.response_rows == {}


def test_a_price_change_refuses_and_a_revised_version_goes_through_approval(harness: Any) -> None:
    enquiry, offer = issue(harness)
    set_fan_price(harness, "4500.00")
    with pytest.raises(PricesChanged) as refused:
        answer(harness, enquiry, offer)
    assert refused.value.revised_version == 2
    assert any("unit_price 4250.00 -> 4500.00" in item for item in refused.value.differences)
    quotation = harness.quotation(enquiry)
    assert quotation.status is QuotationStatus.REVISION_SUBMITTED and quotation.current_version == 2
    revised = harness.db.versions[-1].view
    assert revised.created_by == "system:quotation-workflow"
    assert revised.content["lines"][0]["unit_price"] == "4500.00"
    with pytest.raises(PricesChanged):  # the replay returns the same refusal
        answer(harness, enquiry, offer)
    assert (
        harness.responses().status_of(harness.responses().offer(customer(harness), enquiry, "api"))
        is CustomerStatus.BEING_REVISED
    )

    harness.run_jobs()  # resume: request approval for version 2
    task = harness.pending_task(enquiry)
    assert task.version == 2
    harness.approvals.decide(MANAGER, harness.command(task))  # a manager may approve it
    harness.run_jobs()  # deliver version 2
    new_offer = harness.responses().offer(customer(harness), enquiry, "api")
    assert new_offer.version == 2 and new_offer.content["total"] != offer.content["total"]
    with pytest.raises(NotCurrentOffer):
        answer(harness, enquiry, offer, key="answer-0002")  # the old version is gone
    recorded, _ = answer(harness, enquiry, new_offer, key="answer-0003")
    assert recorded.outcome is ResponseOutcome.ACCEPTED and recorded.version == 2


def test_a_rejected_revision_closes_the_quotation_for_the_customer(harness: Any) -> None:
    enquiry, offer = issue(harness)
    set_fan_price(harness, "4500.00")
    with pytest.raises(PricesChanged):
        answer(harness, enquiry, offer)
    harness.run_jobs()
    task = harness.pending_task(enquiry)
    harness.approvals.decide(
        MANAGER, harness.command(task, ApprovalAction.REJECT, "We cannot hold this price")
    )
    harness.run_jobs()
    held = harness.responses().offer(customer(harness), enquiry, "api")
    assert harness.responses().status_of(held) is CustomerStatus.CLOSED


def test_lines_that_cannot_be_priced_go_to_a_sales_revision(harness: Any) -> None:
    enquiry, offer = issue(harness)
    set_fan_price(harness, None)
    with pytest.raises(PricesChanged) as refused:
        answer(harness, enquiry, offer)
    assert refused.value.revised_version is None
    quotation = harness.quotation(enquiry)
    assert quotation.status is QuotationStatus.PRICING_FAILED and quotation.pricing_issues
    harness.run_jobs()  # the run now waits for a sales revision (as in M4)
    assert quotation.current_version == 1


def test_erp_failure_records_nothing(harness: Any) -> None:
    enquiry, offer = issue(harness)
    harness.erp.failures["get_prices"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    with pytest.raises(ErpError):
        answer(harness, enquiry, offer)
    assert harness.db.response_rows == {}
    assert harness.quotation(enquiry).status is QuotationStatus.APPROVED


def test_the_quotation_expires_at_valid_until(harness: Any) -> None:
    enquiry, offer = issue(harness)
    [expiry] = [job for job in harness.db.jobs if job.kind is JobKind.EXPIRE_QUOTATION]
    assert expiry.run_at == offer.valid_until
    with pytest.raises(NotDueYet):
        harness.responses().expire(harness.tenant_id, offer.quotation_id, offer.version)
    harness.advance(timedelta(days=7))
    harness.run_jobs()
    assert harness.quotation(enquiry).status is QuotationStatus.EXPIRED
    run = harness.db.runs[(harness.tenant_id, enquiry, "quotation-approval-v1")]
    assert harness.db.run_status[run] == "expired"
    with pytest.raises(OfferNotOpen) as closed:
        answer(harness, enquiry, offer)
    assert closed.value.status is CustomerStatus.EXPIRED


def test_an_answer_after_valid_until_expires_it_even_before_the_job_runs(harness: Any) -> None:
    enquiry, offer = issue(harness)
    harness.advance(timedelta(days=7, seconds=1))
    with pytest.raises(OfferNotOpen) as closed:
        answer(harness, enquiry, offer)
    assert closed.value.status is CustomerStatus.EXPIRED
    assert harness.quotation(enquiry).status is QuotationStatus.EXPIRED
    assert harness.responses().expire(harness.tenant_id, offer.quotation_id, 1) is None


def test_staff_withdraw_an_issued_quotation_with_a_reason(harness: Any) -> None:
    enquiry, offer = issue(harness)
    service = harness.responses()
    harness.db.add_member(harness.tenant_id, "admin@industrial.example", Role.ADMINISTRATOR)
    for person in (VIEWER, "admin@industrial.example"):  # only sales operators and approvers
        with pytest.raises(PermissionDenied):
            service.withdraw(harness.context(person), offer.quotation_id, uuid4(), "mistake")
    with pytest.raises(ValueError):
        service.withdraw(harness.context(SALES), offer.quotation_id, uuid4(), "  ")
    command = uuid4()
    recorded, replayed = service.withdraw(
        harness.context(SALES), offer.quotation_id, command, "Wrong product listed"
    )
    assert recorded.outcome is ResponseOutcome.WITHDRAWN and recorded.actor == SALES
    assert service.withdraw(
        harness.context(SALES), offer.quotation_id, command, "Wrong product listed"
    ) == (recorded, True)
    assert harness.quotation(enquiry).status is QuotationStatus.WITHDRAWN
    with pytest.raises(OfferNotOpen) as closed:
        answer(harness, enquiry, offer)
    assert closed.value.status is CustomerStatus.WITHDRAWN
    with pytest.raises(OfferNotOpen):  # an approver cannot withdraw it twice either
        service.withdraw(harness.context(MANAGER), offer.quotation_id, uuid4(), "again")


def test_nothing_issued_means_nothing_to_answer_or_withdraw(harness: Any) -> None:
    enquiry = harness.enquiry(FAN)
    harness.run_jobs()  # drafted, waiting for approval
    quotation = harness.quotation(enquiry).quotation_id
    with pytest.raises(DocumentNotAvailable):
        harness.responses().offer(customer(harness), enquiry, "api")
    with pytest.raises(NotIssued):
        harness.responses().withdraw(harness.context(SALES), quotation, uuid4(), "mistake")


def test_only_customer_systems_of_the_same_business_can_answer(
    harness: Any, office_tenant: UUID
) -> None:
    enquiry, offer = issue(harness)
    request = ResponseRequest(ResponseAction.ACCEPT, offer.version, offer.content_sha256, None)
    with pytest.raises(PermissionDenied):  # staff cannot accept on the customer's behalf
        harness.responses().respond(harness.context(MANAGER), enquiry, "api", "k-000001", request)
    with pytest.raises(DocumentNotAvailable):
        harness.responses().respond(
            customer(harness, office_tenant), enquiry, "api", "k-000002", request
        )


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        (
            ResponseOutcome.ACCEPTED,
            None,
            "Demo Industrial: the customer accepted quotation Q-1A2B3C4D version 2"
            " (total 10596.40 INR).",
        ),
        (
            ResponseOutcome.DECLINED,
            "Found a cheaper offer",
            "Demo Industrial: the customer declined quotation Q-1A2B3C4D version 2"
            " (total 10596.40 INR). Reason: Found a cheaper offer",
        ),
    ],
)
def test_notice_golden_wording(outcome: ResponseOutcome, reason: str | None, expected: str) -> None:
    """Pins the exact manager notice wording before the phase 1 outbox moves it."""
    quotation_id = UUID("1a2b3c4d-0000-4000-8000-000000000000")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    offer = Offer(
        uuid4(), "Demo Industrial", quotation_id, uuid4(), QuotationStatus.ACCEPTED, 2, 2,
        {"total": "10596.40", "currency": "INR"}, "a" * 64, now, now,
    )  # fmt: skip
    response = RecordedResponse(
        uuid4(), quotation_id, 2, "a" * 64, outcome, "api-key:x", reason, (), None, now
    )
    rendered = render_template(TemplateKey.RESPONSE_NOTICE, {"offer": offer, "response": response})
    assert rendered.text == expected
    assert rendered.metadata == {"notice_id": str(response.response_id)}
