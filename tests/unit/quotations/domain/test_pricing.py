"""Business-rule tests for the deterministic quotation calculator (M1 exit gate)."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from sales_platform.catalogue.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.platform.domain.money import Money, RoundingMode
from sales_platform.quotations.domain.pricing import (
    IssueCode,
    PricedQuotation,
    PricingOutcome,
    PricingPolicy,
    PricingRejected,
    StockStatus,
    calculate_quotation,
)

# A small catalogue owned by these tests, chosen to exercise each rule.
PRODUCTS = {
    "FAN": Product("FAN", "Exhaust fan", "each", Decimal(1), Decimal(1)),
    "BOLT": Product("BOLT", "M8 bolt", "each", Decimal(100), Decimal(50)),
    "CABLE": Product("CABLE", "Cable", "metre", Decimal(1), Decimal("0.5")),
    "WASHER": Product("WASHER", "Washer", "each", Decimal(1), Decimal(1)),
}
UNIT_PRICES = {"FAN": "4250.00", "BOLT": "2.375", "CABLE": "38.40", "WASHER": "0.125"}


class Quoting:
    """Builds ERP facts relative to a fixed 'now' and runs the calculator."""

    def __init__(self, now: datetime, policy: PricingPolicy) -> None:
        self.now = now
        self.policy = policy

    def price(self, sku: str, **changes: object) -> PriceQuote:
        quote = PriceQuote(
            sku=sku,
            unit_price=Money(Decimal(UNIT_PRICES[sku]), "INR"),
            unit=PRODUCTS[sku].unit,
            price_list_id="STD",
            price_list_version="1",
            effective_from=self.now - timedelta(days=30),
            effective_to=None,
            fetched_at=self.now - timedelta(seconds=10),
            source="test-erp",
        )
        return replace(quote, **changes)  # type: ignore[arg-type]

    def stock(
        self, sku: str, available: str | None, age: timedelta = timedelta(seconds=10)
    ) -> StockLevel:
        return StockLevel(
            sku=sku,
            unit=PRODUCTS[sku].unit,
            available_quantity=None if available is None else Decimal(available),
            warehouse="MAIN",
            fetched_at=self.now - age,
            source="test-erp",
        )

    def calculate(
        self,
        *lines: RequestedLine,
        prices: dict[str, PriceQuote] | None = None,
        levels: dict[str, StockLevel] | None = None,
        policy: PricingPolicy | None = None,
    ) -> PricingOutcome:
        return calculate_quotation(
            policy=self.policy if policy is None else policy,
            lines=lines,
            products=PRODUCTS,
            prices={sku: self.price(sku) for sku in PRODUCTS} if prices is None else prices,
            stock={sku: self.stock(sku, "100000") for sku in PRODUCTS}
            if levels is None
            else levels,
            now=self.now,
        )


@pytest.fixture
def quoting(
    now: datetime,
    make_policy: Callable[[UUID, str], PricingPolicy],
    industrial_tenant: UUID,
) -> Quoting:
    return Quoting(now, make_policy(industrial_tenant, "INR"))


def line(sku: str, quantity: str, unit: str = "each") -> RequestedLine:
    return RequestedLine(sku, Decimal(quantity), unit)


def priced(outcome: PricingOutcome) -> PricedQuotation:
    assert isinstance(outcome, PricedQuotation), outcome
    return outcome


def issue_codes(outcome: PricingOutcome) -> list[IssueCode]:
    assert isinstance(outcome, PricingRejected), outcome
    return [issue.code for issue in outcome.issues]


def test_totals_are_exact_and_tax_is_rounded_once_on_subtotal(quoting: Quoting) -> None:
    quote = priced(
        quoting.calculate(line("FAN", "2"), line("BOLT", "100"), line("CABLE", "2.5", "metre"))
    )
    assert [item.line_total.amount for item in quote.lines] == [
        Decimal("8500.00"),
        Decimal("237.50"),
        Decimal("96.00"),
    ]
    assert quote.subtotal == Money(Decimal("8833.50"), "INR")
    assert quote.tax == Money(Decimal("1590.03"), "INR")  # 1590.030 exactly
    assert quote.total == Money(Decimal("10423.53"), "INR")
    assert quote.policy is quoting.policy
    assert quote.lines[0].price_evidence.source == "test-erp"


@pytest.mark.parametrize(
    ("mode", "expected"), [(RoundingMode.HALF_UP, "0.13"), (RoundingMode.HALF_EVEN, "0.12")]
)
def test_line_total_uses_tenant_rounding_mode(
    quoting: Quoting, mode: RoundingMode, expected: str
) -> None:
    policy = replace(quoting.policy, rounding=mode)
    quote = priced(quoting.calculate(line("WASHER", "1"), policy=policy))
    assert quote.lines[0].unrounded_total == Decimal("0.125")
    assert quote.lines[0].line_total.amount == Decimal(expected)


def test_absent_price_is_rejected_never_zero(quoting: Quoting) -> None:
    prices = {sku: quoting.price(sku) for sku in PRODUCTS if sku != "FAN"}
    outcome = quoting.calculate(line("FAN", "1"), line("BOLT", "100"), prices=prices)
    assert issue_codes(outcome) == [IssueCode.PRICE_MISSING]
    assert isinstance(outcome, PricingRejected) and outcome.issues[0].line_index == 0


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (line("FAN", "0"), [IssueCode.QUANTITY_NOT_POSITIVE]),
        (line("FAN", "-3"), [IssueCode.QUANTITY_NOT_POSITIVE]),
        (line("FAN", "1.5"), [IssueCode.QUANTITY_NOT_MULTIPLE]),
        (line("FAN", "2", "box"), [IssueCode.UNIT_MISMATCH]),
        (line("BOLT", "50"), [IssueCode.QUANTITY_BELOW_MINIMUM]),
        (line("BOLT", "120"), [IssueCode.QUANTITY_NOT_MULTIPLE]),
        (line("BOLT", "30"), [IssueCode.QUANTITY_BELOW_MINIMUM, IssueCode.QUANTITY_NOT_MULTIPLE]),
        (line("CABLE", "2.3", "metre"), [IssueCode.QUANTITY_NOT_MULTIPLE]),
        (line("MISSING", "1"), [IssueCode.PRODUCT_NOT_FOUND]),
    ],
)
def test_quantity_unit_and_product_rules(
    quoting: Quoting, requested: RequestedLine, expected: list[IssueCode]
) -> None:
    assert issue_codes(quoting.calculate(requested)) == expected


def test_quantity_must_be_decimal() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        RequestedLine("FAN", 2, "each")  # type: ignore[arg-type]


# Each case returns the PriceQuote fields to change, relative to the fixed "now".
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            lambda now: {"unit_price": Money(Decimal("50.00"), "USD")},
            IssueCode.PRICE_CURRENCY_MISMATCH,
        ),
        (lambda now: {"unit": "box"}, IssueCode.PRICE_UNIT_MISMATCH),
        (
            lambda now: {"effective_from": now + timedelta(days=1)},
            IssueCode.PRICE_NOT_EFFECTIVE,
        ),
        (lambda now: {"effective_to": now}, IssueCode.PRICE_NOT_EFFECTIVE),
        (lambda now: {"fetched_at": now - timedelta(minutes=16)}, IssueCode.PRICE_STALE),
    ],
)
def test_untrustworthy_prices_block_the_quote(
    quoting: Quoting,
    changes: Callable[[datetime], dict[str, object]],
    expected: IssueCode,
) -> None:
    prices = {"FAN": quoting.price("FAN", **changes(quoting.now))}
    assert issue_codes(quoting.calculate(line("FAN", "1"), prices=prices)) == [expected]


@pytest.mark.parametrize(
    ("available", "age_minutes", "unit", "expected"),
    [
        ("10", 0, "each", StockStatus.AVAILABLE),
        ("3", 0, "each", StockStatus.INSUFFICIENT),
        ("0", 0, "each", StockStatus.UNAVAILABLE),
        (None, 0, "each", StockStatus.UNKNOWN),
        ("10", 0, "box", StockStatus.UNKNOWN),
        ("10", 6, "each", StockStatus.STALE),
    ],
)
def test_stock_results_stay_distinct(
    quoting: Quoting,
    available: str | None,
    age_minutes: int,
    unit: str,
    expected: StockStatus,
) -> None:
    level = replace(quoting.stock("FAN", available, timedelta(minutes=age_minutes)), unit=unit)
    quote = priced(quoting.calculate(line("FAN", "5"), levels={"FAN": level}))
    assert quote.lines[0].stock.status is expected
    assert quote.stock_review_required is (expected is not StockStatus.AVAILABLE)


def test_missing_stock_result_is_unknown(quoting: Quoting) -> None:
    quote = priced(quoting.calculate(line("FAN", "5"), levels={}))
    assert quote.lines[0].stock.status is StockStatus.UNKNOWN


def test_all_issues_are_reported_together(quoting: Quoting) -> None:
    outcome = quoting.calculate(
        line("MISSING", "1"), line("FAN", "0"), line("BOLT", "100"), prices={}
    )
    assert issue_codes(outcome) == [
        IssueCode.PRODUCT_NOT_FOUND,
        IssueCode.QUANTITY_NOT_POSITIVE,
        IssueCode.PRICE_MISSING,
        IssueCode.PRICE_MISSING,
    ]


def test_explanation_shows_formula_and_evidence(quoting: Quoting) -> None:
    steps = priced(quoting.calculate(line("BOLT", "100"))).explain()
    assert "BOLT: 100 each x 2.375 INR = 237.500 -> 237.50 INR" in steps[1]
    assert steps[-1] == "Subtotal 237.50 INR; tax 42.75 INR; total 280.25 INR"


def test_invalid_inputs_fail_loudly(quoting: Quoting) -> None:
    with pytest.raises(ValueError, match="timezone"):
        calculate_quotation(
            policy=quoting.policy,
            lines=[line("FAN", "1")],
            products={},
            prices={},
            stock={},
            now=datetime(2026, 10, 22),
        )
    with pytest.raises(ValueError, match="At least one"):
        quoting.calculate()
    with pytest.raises(ValueError, match="tax_rate"):
        replace(quoting.policy, tax_rate=Decimal(18))
