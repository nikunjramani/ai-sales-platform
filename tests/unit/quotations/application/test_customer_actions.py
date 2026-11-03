"""Answering by reference (chat buttons, secure links): same core as the API answer."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.quotations.application.customer_actions import (
    CustomerActionRejected,
    CustomerActionService,
)
from sales_platform.quotations.domain.acceptance import (
    NotCurrentOffer,
    Offer,
    OfferNotOpen,
    PricesChanged,
    ResponseAction,
    ResponseOutcome,
)
from sales_platform.quotations.ports.customer_actions import CustomerActionRef
from tests.unit.quotations.application.test_acceptance import issue, set_fan_price

IDENTITY = uuid4()


class Refs:
    def __init__(self, ref: CustomerActionRef) -> None:
        self.ref = ref

    def get(self, tenant_id: UUID, ref_id: UUID) -> CustomerActionRef | None:
        same = (tenant_id, ref_id) == (self.ref.tenant_id, self.ref.ref_id)
        return self.ref if same else None


def reference(offer: Offer, **changes: Any) -> CustomerActionRef:
    values: dict[str, Any] = {
        "ref_id": uuid4(),
        "tenant_id": offer.tenant_id,
        "quotation_id": offer.quotation_id,
        "version": offer.version,
        "content_sha256": offer.content_sha256,
        "identity_id": IDENTITY,
        "conversation_id": uuid4(),
        "expires_at": offer.valid_until,
        "consumed_at": None,
    }
    values.update(changes)
    return CustomerActionRef(**values)


def service(harness: Any, ref: CustomerActionRef) -> CustomerActionService:
    return CustomerActionService(Refs(ref), harness.responses())  # type: ignore[arg-type]


def test_a_button_press_accepts_and_a_second_press_replays(harness: Any) -> None:
    _, offer = issue(harness)
    ref = reference(offer)
    actions = service(harness, ref)
    found = actions.by_button(offer.tenant_id, ref.ref_id, IDENTITY)
    recorded, replayed = actions.act(found, ResponseAction.ACCEPT, None)
    assert recorded.outcome is ResponseOutcome.ACCEPTED and not replayed
    assert recorded.actor == f"customer:{IDENTITY}"
    again, replayed = actions.act(found, ResponseAction.ACCEPT, None)
    assert again.response_id == recorded.response_id and replayed
    with pytest.raises(OfferNotOpen):
        actions.act(found, ResponseAction.DECLINE, None)  # the other button after answering


def test_someone_else_pressing_the_button_is_refused(harness: Any) -> None:
    _, offer = issue(harness)
    ref = reference(offer)
    actions = service(harness, ref)
    with pytest.raises(CustomerActionRejected):
        actions.by_button(offer.tenant_id, ref.ref_id, uuid4())
    with pytest.raises(CustomerActionRejected):
        actions.by_button(uuid4(), ref.ref_id, IDENTITY)  # another business


def test_a_reference_for_an_older_version_is_refused(harness: Any) -> None:
    _, offer = issue(harness)
    stale = reference(offer, content_sha256="0" * 64)
    with pytest.raises(NotCurrentOffer):
        service(harness, stale).act(stale, ResponseAction.ACCEPT, None)


def test_changed_prices_refuse_and_revise_like_the_api(harness: Any) -> None:
    _, offer = issue(harness)
    set_fan_price(harness, "4500.00")
    ref = reference(offer)
    with pytest.raises(PricesChanged):
        service(harness, ref).act(ref, ResponseAction.ACCEPT, None)


def test_decline_keeps_the_reason(harness: Any) -> None:
    _, offer = issue(harness)
    ref = reference(offer)
    recorded, _ = service(harness, ref).act(ref, ResponseAction.DECLINE, "Too expensive")
    assert recorded.outcome is ResponseOutcome.DECLINED and recorded.reason == "Too expensive"
