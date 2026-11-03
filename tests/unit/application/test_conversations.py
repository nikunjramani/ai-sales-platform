"""Intent routing: handler order, the follow-up handler and a plugged-in extra handler."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sales_platform.application.conversations import FollowUpHandler, IntentRouter, default_router
from sales_platform.domain.conversations import FollowUp, OpenEnquiry
from sales_platform.domain.intake import EnquiryStatus

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def waiting() -> OpenEnquiry:
    return OpenEnquiry(
        uuid4(), EnquiryStatus.NEEDS_CLARIFICATION, NOW - timedelta(hours=2), NOW, False
    )


class ClaimsEverything:
    """A later intent (e.g. order status) plugs in as one more handler; no core change."""

    name = "always"

    def __init__(self) -> None:
        self.target = FollowUp(uuid4(), reopen=False)
        self.calls = 0

    def decide(self, open_enquiries: Sequence[OpenEnquiry], now: datetime) -> FollowUp | None:
        self.calls += 1
        return self.target


def test_no_handler_claiming_the_message_starts_a_new_enquiry() -> None:
    assert IntentRouter([])([waiting()], NOW) is None
    assert default_router()([], NOW) is None


def test_the_follow_up_handler_continues_a_waiting_enquiry() -> None:
    open_enquiry = waiting()
    assert default_router()([open_enquiry], NOW) == FollowUp(open_enquiry.enquiry_id, True)


def test_handlers_are_asked_in_order_and_the_first_claim_wins() -> None:
    extra = ClaimsEverything()
    open_enquiry = waiting()
    first = IntentRouter([FollowUpHandler(), extra])
    assert first([open_enquiry], NOW) == FollowUp(open_enquiry.enquiry_id, True)
    assert extra.calls == 0
    assert first([], NOW) == extra.target
    assert IntentRouter([extra, FollowUpHandler()])([open_enquiry], NOW) == extra.target
