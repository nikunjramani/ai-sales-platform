"""Routing a customer's message within their conversation (phase 4).

Every tool's customer message goes through the same store method (IntakeStore.record), which
locks the customer's conversation and asks an EnquiryRouter which open enquiry, if any, the
message continues. IntentRouter is that router: an ordered list of handlers, the first one that
claims the message wins. FollowUpHandler applies the agreed follow-up rules; anything it does
not claim starts a new enquiry.

Later intents ("where is my order?", payments, "talk to a person") are more handlers added to
the list in the composition root. The router never names a provider, and AI intent detection,
when it comes, is one more handler (not in v1).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from sales_platform.domain.conversations import FollowUp, OpenEnquiry, follow_up_target


class IntentHandler(Protocol):
    name: str

    def decide(self, open_enquiries: Sequence[OpenEnquiry], now: datetime) -> FollowUp | None:
        """Claim the message by returning the enquiry it continues, or None to pass it on."""
        ...


class FollowUpHandler:
    name = "follow_up"

    def decide(self, open_enquiries: Sequence[OpenEnquiry], now: datetime) -> FollowUp | None:
        return follow_up_target(open_enquiries, now)


class IntentRouter:
    """Callable as an EnquiryRouter. No handler claiming the message means a new enquiry."""

    def __init__(self, handlers: Sequence[IntentHandler]) -> None:
        self._handlers = tuple(handlers)

    def __call__(self, open_enquiries: Sequence[OpenEnquiry], now: datetime) -> FollowUp | None:
        for handler in self._handlers:
            decided = handler.decide(open_enquiries, now)
            if decided is not None:
                return decided
        return None


def default_router() -> IntentRouter:
    return IntentRouter([FollowUpHandler()])
