"""Acceptance rules: customer status, validity, price comparison and answer fingerprints."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sales_platform.domain.acceptance import (
    DEFAULT_QUOTE_VALIDITY_DAYS,
    CustomerStatus,
    ResponseAction,
    ResponseRequest,
    customer_status,
    price_differences,
    valid_until,
)
from sales_platform.domain.quotations import QuotationStatus

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=7)


def content(
    unit_price: str = "4250.00", total: str = "10030.00", stock: str = "in_stock"
) -> dict[str, Any]:
    return {
        "currency": "INR",
        "lines": [
            {
                "sku": "IND-FAN-450",
                "quantity": "2",
                "unit": "each",
                "unit_price": unit_price,
                "line_total": "8500.00",
                "stock_status": stock,
                "price_list_version": "3",
            }
        ],
        "subtotal": "8500.00",
        "tax": "1530.00",
        "total": total,
    }


def test_default_validity_is_seven_days_from_preparation() -> None:
    assert DEFAULT_QUOTE_VALIDITY_DAYS == 7
    assert valid_until(NOW, 7) == LATER
    with pytest.raises(ValueError):
        valid_until(NOW, 0)


@pytest.mark.parametrize(
    ("status", "current", "now", "expected"),
    [
        (QuotationStatus.APPROVED, 1, NOW, CustomerStatus.OPEN),
        (QuotationStatus.APPROVED, 1, LATER, CustomerStatus.EXPIRED),  # before the job ran
        (QuotationStatus.APPROVED, 2, NOW, CustomerStatus.BEING_REVISED),  # v2 not delivered yet
        (QuotationStatus.REVISION_SUBMITTED, 2, NOW, CustomerStatus.BEING_REVISED),
        (QuotationStatus.PENDING_APPROVAL, 2, NOW, CustomerStatus.BEING_REVISED),
        (QuotationStatus.PRICING_FAILED, 1, NOW, CustomerStatus.BEING_REVISED),
        (QuotationStatus.ACCEPTED, 1, NOW, CustomerStatus.ACCEPTED),
        (QuotationStatus.DECLINED, 1, NOW, CustomerStatus.DECLINED),
        (QuotationStatus.EXPIRED, 1, NOW, CustomerStatus.EXPIRED),
        (QuotationStatus.WITHDRAWN, 1, NOW, CustomerStatus.WITHDRAWN),
        (QuotationStatus.REJECTED, 2, NOW, CustomerStatus.CLOSED),  # revised version rejected
    ],
)
def test_customer_status(
    status: QuotationStatus, current: int, now: datetime, expected: CustomerStatus
) -> None:
    assert customer_status(status, 1, current, LATER, now) is expected


def test_identical_prices_have_no_differences_and_stock_is_ignored() -> None:
    assert price_differences(content(), content(stock="insufficient")) == ()
    assert price_differences(content(), {**content(), "calculated_at": "later"}) == ()


def test_every_amount_change_is_reported() -> None:
    changed = price_differences(content(), content(unit_price="4300.00", total="10148.00"))
    assert changed == ("IND-FAN-450 unit_price 4250.00 -> 4300.00", "total 10030.00 -> 10148.00")
    fewer = {**content(), "lines": []}
    assert "line count 1 -> 0" in price_differences(content(), fewer)
    assert price_differences(content(), {**content(), "currency": "EUR"}) == (
        "currency INR -> EUR",
    )


def test_answers_are_fingerprinted_and_reasons_only_come_with_declines() -> None:
    accept = ResponseRequest(ResponseAction.ACCEPT, 1, "a" * 64, None)
    decline = ResponseRequest(ResponseAction.DECLINE, 1, "a" * 64, "  too expensive ")
    assert decline.reason == "too expensive"
    assert accept.fingerprint() != decline.fingerprint()
    assert (
        accept.fingerprint()
        == ResponseRequest(ResponseAction.ACCEPT, 1, "a" * 64, None).fingerprint()
    )
    with pytest.raises(ValueError):
        ResponseRequest(ResponseAction.ACCEPT, 1, "a" * 64, "but cheaper")
