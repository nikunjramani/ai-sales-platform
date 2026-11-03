"""Which enquiry a customer's new message belongs to (phase 4).

Agreed rules (conversation-follow-ups plan, user 2026-10-23) and the phase 4 answers
(2026-10-28):
- Only enquiries of the same conversation (same customer on the same tool, Q4) are candidates.
- An enquiry waiting for an answer to our question (needs_clarification) is joined if that
  question is less than 7 days old. Exactly 7 days is too old.
- An enquiry that is still being read (received or ready_for_extraction) is also joined (Q3):
  chat customers send several short messages, and the reading is redone with all of them.
- Enquiries in review, ready for quotation or with a quotation are never reopened.
- If several qualify, the newest enquiry wins. Otherwise the message starts a new enquiry.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sales_platform.domain.intake import EnquiryStatus

FOLLOW_UP_WINDOW = timedelta(days=7)
BEING_READ = frozenset({EnquiryStatus.RECEIVED, EnquiryStatus.READY_FOR_EXTRACTION})


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Author(StrEnum):
    CUSTOMER = "customer"
    SYSTEM = "system"
    STAFF = "staff"  # staff replies come later (D16)


class Intent(StrEnum):
    FOLLOW_UP = "follow_up"
    NEW_ENQUIRY = "new_enquiry"


@dataclass(frozen=True)
class OpenEnquiry:
    """An enquiry of the conversation, as far as routing needs to know it."""

    enquiry_id: UUID
    status: EnquiryStatus
    received_at: datetime
    last_question_at: datetime | None  # when the latest round asked the customer something
    has_quotation: bool


@dataclass(frozen=True)
class FollowUp:
    enquiry_id: UUID
    reopen: bool  # True: it was waiting for an answer, so it goes back to received


def follow_up_target(candidates: Sequence[OpenEnquiry], now: datetime) -> FollowUp | None:
    """The enquiry this message continues, or None for a new enquiry."""
    joinable: list[tuple[OpenEnquiry, bool]] = []
    for enquiry in candidates:
        if enquiry.has_quotation:
            continue
        if enquiry.status in BEING_READ:
            joinable.append((enquiry, False))
        elif (
            enquiry.status is EnquiryStatus.NEEDS_CLARIFICATION
            and enquiry.last_question_at is not None
            and now - enquiry.last_question_at < FOLLOW_UP_WINDOW
        ):
            joinable.append((enquiry, True))
    if not joinable:
        return None
    newest, reopen = max(joinable, key=lambda pair: (pair[0].received_at, pair[0].enquiry_id))
    return FollowUp(newest.enquiry_id, reopen)
