"""Which enquiry a new message continues (agreed follow-up rules + phase 4 Q3)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sales_platform.domain.conversations import FollowUp, OpenEnquiry, follow_up_target
from sales_platform.domain.intake import EnquiryStatus

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def enquiry(
    status: EnquiryStatus,
    asked_ago: timedelta | None = timedelta(hours=1),
    received_ago: timedelta = timedelta(hours=2),
    quoted: bool = False,
    enquiry_id: UUID | None = None,
) -> OpenEnquiry:
    return OpenEnquiry(
        enquiry_id or uuid4(),
        status,
        NOW - received_ago,
        None if asked_ago is None else NOW - asked_ago,
        quoted,
    )


def test_a_waiting_enquiry_is_reopened() -> None:
    waiting = enquiry(EnquiryStatus.NEEDS_CLARIFICATION)
    assert follow_up_target([waiting], NOW) == FollowUp(waiting.enquiry_id, reopen=True)


@pytest.mark.parametrize("status", [EnquiryStatus.RECEIVED, EnquiryStatus.READY_FOR_EXTRACTION])
def test_an_enquiry_still_being_read_is_joined_without_reopening(status: EnquiryStatus) -> None:
    reading = enquiry(status, asked_ago=None)
    assert follow_up_target([reading], NOW) == FollowUp(reading.enquiry_id, reopen=False)


@pytest.mark.parametrize("status", [EnquiryStatus.NEEDS_REVIEW, EnquiryStatus.READY_FOR_QUOTATION])
def test_review_and_quotation_cases_are_never_reopened(status: EnquiryStatus) -> None:
    assert follow_up_target([enquiry(status)], NOW) is None


def test_an_enquiry_with_a_quotation_is_never_joined() -> None:
    quoted = enquiry(EnquiryStatus.NEEDS_CLARIFICATION, quoted=True)
    assert follow_up_target([quoted], NOW) is None


@pytest.mark.parametrize(
    ("asked_ago", "joined"),
    [
        (timedelta(days=6, hours=23, minutes=59), True),
        (timedelta(days=7), False),
        (timedelta(days=8), False),
    ],
)
def test_the_seven_day_window(asked_ago: timedelta, joined: bool) -> None:
    waiting = enquiry(EnquiryStatus.NEEDS_CLARIFICATION, asked_ago=asked_ago)
    assert (follow_up_target([waiting], NOW) is not None) is joined


def test_no_question_recorded_means_no_follow_up() -> None:
    unasked = enquiry(EnquiryStatus.NEEDS_CLARIFICATION, asked_ago=None)
    assert follow_up_target([unasked], NOW) is None


def test_the_newest_of_two_candidates_wins() -> None:
    older = enquiry(EnquiryStatus.NEEDS_CLARIFICATION, received_ago=timedelta(days=2))
    newer = enquiry(EnquiryStatus.NEEDS_CLARIFICATION, received_ago=timedelta(hours=3))
    target = follow_up_target([older, newer], NOW)
    assert target is not None and target.enquiry_id == newer.enquiry_id


def test_nothing_open_starts_a_new_enquiry() -> None:
    assert follow_up_target([], NOW) is None
