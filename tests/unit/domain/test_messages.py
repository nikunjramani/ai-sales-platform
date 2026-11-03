"""Outbox statuses and templates (the golden wording lives in test_quotations/test_acceptance)."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from sales_platform.domain.accounts import AccountEmailPurpose
from sales_platform.domain.messages import (
    ACCOUNT_TEMPLATES,
    CUSTOMER_WORDING,
    FINAL_MESSAGE_STATUSES,
    MessageStatus,
    TemplateKey,
    format_valid_until,
    render_template,
)


def test_only_pending_and_sending_are_unfinished() -> None:
    assert set(MessageStatus) - FINAL_MESSAGE_STATUSES == {
        MessageStatus.PENDING,
        MessageStatus.SENDING,
    }


def test_quotation_document_has_no_invented_covering_text() -> None:
    quotation_id = UUID("1a2b3c4d-0000-4000-8000-000000000000")
    rendered = render_template(
        TemplateKey.QUOTATION_DOCUMENT, {"quotation_id": quotation_id, "version": 3}
    )
    assert rendered.text == ""
    assert rendered.metadata == {"quotation": "Q-1A2B3C4D", "version": "3"}


def test_customer_messages_use_the_confirmed_wording() -> None:
    """Q7 / Q10 / D15 texts, exactly as the user confirmed them on 2026-10-28."""
    params = {
        "number": "Q-1A2B3C4D",
        "version": "2",
        "total": "10450.38",
        "currency": "INR",
        "date": "2026-11-04 10:00 UTC",
        "business": "Acme Industrial",
    }
    expected = {
        TemplateKey.ENQUIRY_RECEIVED: "Thanks, we are preparing your quotation.",
        TemplateKey.QUOTATION_READY: "Your quotation Q-1A2B3C4D (version 2) is ready: total "
        "10450.38 INR, valid until 2026-11-04 10:00 UTC.",
        TemplateKey.ANSWER_ACCEPTED: "Thank you, we received your acceptance of Q-1A2B3C4D.",
        TemplateKey.ANSWER_DECLINED: "Thank you, we recorded that you declined Q-1A2B3C4D.",
        TemplateKey.PRICES_CHANGED: "Prices have changed since quotation Q-1A2B3C4D. We are "
        "preparing an updated quotation for you.",
        TemplateKey.QUOTATION_WITHDRAWN: "Quotation Q-1A2B3C4D has been withdrawn. Please "
        "contact us if you still need it.",
        TemplateKey.QUOTATION_EXPIRED: "Quotation Q-1A2B3C4D has expired (it was valid until "
        "2026-11-04 10:00 UTC). Send us a message if you would like an updated quotation.",
        TemplateKey.OPTED_OUT: "You will not receive more messages from Acme Industrial. Reply "
        "START to receive messages again.",
        TemplateKey.OPTED_IN: "You will receive messages from Acme Industrial again.",
    }
    texts = {key: render_template(key, params).text for key in expected}
    assert texts == expected
    assert set(CUSTOMER_WORDING) - set(expected) == ORDER_TEMPLATES  # phase 7, tested below


ORDER_TEMPLATES = {
    TemplateKey.ORDER_CONFIRMED,
    TemplateKey.ORDER_SHIPPED,
    TemplateKey.ORDER_DELIVERED,
    TemplateKey.ORDER_CANCELLED,
    TemplateKey.ORDER_STATUS,
    TemplateKey.ORDER_NONE,
}


def test_order_messages_have_fixed_wording() -> None:
    params = {"order": "O-1A2B3C4D", "number": "Q-9F8E7D6C", "business": "Acme Industrial"}
    texts = {
        key: render_template(key, params).text
        for key in ORDER_TEMPLATES - {TemplateKey.ORDER_STATUS}
    }
    assert texts == {
        TemplateKey.ORDER_CONFIRMED: "Your order O-1A2B3C4D (quotation Q-9F8E7D6C) is confirmed.",
        TemplateKey.ORDER_SHIPPED: "Your order O-1A2B3C4D has been shipped.",
        TemplateKey.ORDER_DELIVERED: "Your order O-1A2B3C4D has been delivered. Thank you!",
        TemplateKey.ORDER_CANCELLED: "Your order O-1A2B3C4D has been cancelled. Please contact "
        "us if you have questions.",
        TemplateKey.ORDER_NONE: "We could not find an order of yours with Acme Industrial.",
    }


def test_order_status_lists_each_order_on_its_own_line() -> None:
    rendered = render_template(
        TemplateKey.ORDER_STATUS,
        {"business": "Acme\nIndustrial", "orders": ["O-1 (quotation Q-1): shipped", "O-2\nx"]},
    )
    assert rendered.text == (
        "Your orders with Acme Industrial:\n- O-1 (quotation Q-1): shipped\n- O-2 x"
    )


def test_clarification_questions_list_each_question_on_its_own_line() -> None:
    rendered = render_template(
        TemplateKey.CLARIFICATION_QUESTIONS,
        {"questions": ["How many metre of 2-core power cable do you need?", "Which fan?\nX"]},
    )
    assert rendered.text == (
        "We need a bit more information:\n"
        "- How many metre of 2-core power cable do you need?\n"
        "- Which fan? X"
    )


def test_a_missing_customer_parameter_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        render_template(TemplateKey.ANSWER_ACCEPTED, {})


def test_valid_until_matches_the_pdf_format() -> None:
    moment = datetime(2026, 11, 4, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert format_valid_until(moment) == "2026-11-04 10:00 UTC"


def test_missing_parameters_are_a_programming_error() -> None:
    with pytest.raises(KeyError):
        render_template(TemplateKey.APPROVAL_REQUEST, {})


@pytest.mark.parametrize("purpose", list(AccountEmailPurpose))
def test_every_account_email_has_a_subject_and_the_link(purpose: AccountEmailPurpose) -> None:
    rendered = render_template(
        ACCOUNT_TEMPLATES[purpose],
        {"link": "http://portal.example/#/x?token=t", "business": "Acme", "roles": "approver"},
    )
    assert rendered.metadata["subject"]
    assert "http://portal.example/#/x?token=t" in rendered.text


def test_account_email_values_cannot_add_lines_or_headers() -> None:
    rendered = render_template(
        TemplateKey.ACCOUNT_INVITATION,
        {"link": "L", "business": "Acme\r\nBcc: evil@example.test", "roles": "viewer"},
    )
    assert rendered.metadata["subject"] == "You are invited to Acme Bcc: evil@example.test"
    assert "\r" not in rendered.text
    assert rendered.text.startswith(
        "You are invited to join Acme Bcc: evil@example.test as viewer."
    )
    assert "within 7 days" in rendered.text
