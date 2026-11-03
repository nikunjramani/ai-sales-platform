"""Deterministic quotation calculation.

Inputs: requested lines plus ERP products, prices and stock, a tenant pricing policy and "now".
Output: a PricedQuotation, or a PricingRejected listing every blocking issue. A missing,
stale or mismatched price never becomes zero; stock problems are reported, not hidden.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sales_platform.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.domain.money import (
    Money,
    RoundingMode,
    exact_multiply,
    round_to_minor_units,
    validate_currency,
)
from sales_platform.domain.validation import require_aware, require_decimal, require_text


@dataclass(frozen=True)
class PricingPolicy:
    """Per-tenant commercial settings. Sample values are synthetic, not tax advice."""

    tenant_id: UUID
    version: int
    currency: str
    minor_units: int
    rounding: RoundingMode
    tax_rate: Decimal
    max_price_age: timedelta
    max_stock_age: timedelta

    def __post_init__(self) -> None:
        validate_currency(self.currency)
        require_decimal(self.tax_rate, "tax_rate")
        if self.version < 1:
            raise ValueError("Policy version must be at least 1")
        if not 0 <= self.minor_units <= 4:
            raise ValueError("minor_units must be between 0 and 4")
        if not Decimal(0) <= self.tax_rate < Decimal(1):
            raise ValueError("tax_rate is a fraction between 0 and 1, e.g. 0.18")
        if self.max_price_age <= timedelta(0) or self.max_stock_age <= timedelta(0):
            raise ValueError("Freshness limits must be positive")

    def round(self, amount: Decimal) -> Decimal:
        return round_to_minor_units(amount, self.minor_units, self.rounding)


@dataclass(frozen=True)
class RequestedLine:
    """What the customer asked for. Quantity problems become issues so they can be clarified."""

    sku: str
    quantity: Decimal
    unit: str

    def __post_init__(self) -> None:
        require_text(self.sku, "sku", 64)
        require_decimal(self.quantity, "quantity")
        require_text(self.unit, "unit", 32)


class IssueCode(StrEnum):
    PRODUCT_NOT_FOUND = "product_not_found"
    QUANTITY_NOT_POSITIVE = "quantity_not_positive"
    UNIT_MISMATCH = "unit_mismatch"
    QUANTITY_BELOW_MINIMUM = "quantity_below_minimum"
    QUANTITY_NOT_MULTIPLE = "quantity_not_multiple"
    PRICE_MISSING = "price_missing"
    PRICE_UNIT_MISMATCH = "price_unit_mismatch"
    PRICE_CURRENCY_MISMATCH = "price_currency_mismatch"
    PRICE_NOT_EFFECTIVE = "price_not_effective"
    PRICE_STALE = "price_stale"


@dataclass(frozen=True)
class PricingIssue:
    line_index: int
    sku: str
    code: IssueCode
    message: str


class StockStatus(StrEnum):
    AVAILABLE = "available"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    STALE = "stale"


@dataclass(frozen=True)
class StockCheck:
    status: StockStatus
    available_quantity: Decimal | None
    evidence: StockLevel | None
    note: str


@dataclass(frozen=True)
class PricedLine:
    line_index: int
    sku: str
    description: str
    quantity: Decimal
    unit: str
    unit_price: Money
    unrounded_total: Decimal
    line_total: Money
    price_evidence: PriceQuote
    stock: StockCheck


@dataclass(frozen=True)
class PricedQuotation:
    """A calculated draft. It is evidence for review, not an approved or sent offer."""

    tenant_id: UUID
    policy: PricingPolicy
    lines: tuple[PricedLine, ...]
    subtotal: Money
    tax: Money
    total: Money
    calculated_at: datetime

    @property
    def stock_review_required(self) -> bool:
        return any(line.stock.status is not StockStatus.AVAILABLE for line in self.lines)

    def explain(self) -> tuple[str, ...]:
        policy = self.policy
        steps = [
            f"Policy v{policy.version}: {policy.currency}, {policy.minor_units} decimals, "
            f"{policy.rounding} rounding, tax rate {policy.tax_rate}"
        ]
        for line in self.lines:
            steps.append(
                f"{line.sku}: {line.quantity} {line.unit} x {line.unit_price} = "
                f"{line.unrounded_total} -> {line.line_total} "
                f"(price list {line.price_evidence.price_list_id} "
                f"v{line.price_evidence.price_list_version}); stock {line.stock.status}"
            )
        steps.append(f"Subtotal {self.subtotal}; tax {self.tax}; total {self.total}")
        return tuple(steps)


@dataclass(frozen=True)
class PricingRejected:
    issues: tuple[PricingIssue, ...]


type PricingOutcome = PricedQuotation | PricingRejected


def calculate_quotation(
    *,
    policy: PricingPolicy,
    lines: Sequence[RequestedLine],
    products: Mapping[str, Product],
    prices: Mapping[str, PriceQuote],
    stock: Mapping[str, StockLevel],
    now: datetime,
) -> PricingOutcome:
    require_aware(now, "now")
    if not lines:
        raise ValueError("At least one requested line is required")
    issues: list[PricingIssue] = []
    priced: list[PricedLine] = []
    for index, line in enumerate(lines):
        product = products.get(line.sku)
        price = prices.get(line.sku)
        line_issues = _line_issues(index, line, product, price, policy, now)
        issues.extend(line_issues)
        # A missing product or price always produces an issue; the None checks narrow types.
        if line_issues or product is None or price is None:
            continue
        unrounded = exact_multiply(price.unit_price.amount, line.quantity)
        priced.append(
            PricedLine(
                line_index=index,
                sku=line.sku,
                description=product.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=price.unit_price,
                unrounded_total=unrounded,
                line_total=Money(policy.round(unrounded), policy.currency),
                price_evidence=price,
                stock=_check_stock(line, stock.get(line.sku), policy, now),
            )
        )
    if issues:
        return PricingRejected(tuple(issues))
    # Line totals are already rounded; tax is calculated once on the subtotal, then rounded.
    subtotal = policy.round(sum((line.line_total.amount for line in priced), Decimal(0)))
    tax = policy.round(exact_multiply(subtotal, policy.tax_rate))
    return PricedQuotation(
        tenant_id=policy.tenant_id,
        policy=policy,
        lines=tuple(priced),
        subtotal=Money(subtotal, policy.currency),
        tax=Money(tax, policy.currency),
        total=Money(subtotal + tax, policy.currency),
        calculated_at=now,
    )


def _line_issues(
    index: int,
    line: RequestedLine,
    product: Product | None,
    price: PriceQuote | None,
    policy: PricingPolicy,
    now: datetime,
) -> list[PricingIssue]:
    def issue(code: IssueCode, message: str) -> PricingIssue:
        return PricingIssue(index, line.sku, code, message)

    if product is None:
        return [issue(IssueCode.PRODUCT_NOT_FOUND, "The ERP has no product with this SKU")]
    found: list[PricingIssue] = []
    if line.quantity <= 0:
        found.append(issue(IssueCode.QUANTITY_NOT_POSITIVE, "Quantity must be greater than 0"))
    elif line.unit != product.unit:
        # No unit conversion is guessed; conversions need an explicit, documented mapping.
        found.append(
            issue(
                IssueCode.UNIT_MISMATCH,
                f"Requested {line.unit}; product is sold per {product.unit}",
            )
        )
    else:
        if line.quantity < product.minimum_quantity:
            found.append(
                issue(
                    IssueCode.QUANTITY_BELOW_MINIMUM,
                    f"Minimum order is {product.minimum_quantity} {product.unit}",
                )
            )
        if line.quantity % product.quantity_step != 0:
            found.append(
                issue(
                    IssueCode.QUANTITY_NOT_MULTIPLE,
                    f"Quantity must be a multiple of {product.quantity_step} {product.unit}",
                )
            )
    if price is None:
        found.append(issue(IssueCode.PRICE_MISSING, "No current price; a price is never assumed"))
        return found
    if price.unit != product.unit:
        found.append(
            issue(
                IssueCode.PRICE_UNIT_MISMATCH,
                f"Price is per {price.unit}, product per {product.unit}",
            )
        )
    if price.unit_price.currency != policy.currency:
        found.append(
            issue(
                IssueCode.PRICE_CURRENCY_MISMATCH,
                f"Price is in {price.unit_price.currency}; quotation currency is {policy.currency}",
            )
        )
    if not price.is_effective_at(now):
        found.append(issue(IssueCode.PRICE_NOT_EFFECTIVE, "Price is not effective now"))
    if now - price.fetched_at > policy.max_price_age:
        found.append(issue(IssueCode.PRICE_STALE, "Price data is older than the policy allows"))
    return found


def _check_stock(
    line: RequestedLine, level: StockLevel | None, policy: PricingPolicy, now: datetime
) -> StockCheck:
    if level is None:
        return StockCheck(StockStatus.UNKNOWN, None, None, "ERP returned no stock result")
    if level.unit != line.unit:
        return StockCheck(
            StockStatus.UNKNOWN, None, level, "Stock unit differs; no conversion is assumed"
        )
    if now - level.fetched_at > policy.max_stock_age:
        return StockCheck(StockStatus.STALE, None, level, "Stock data is older than allowed")
    available = level.available_quantity
    if available is None:
        return StockCheck(StockStatus.UNKNOWN, None, level, "ERP did not state a quantity")
    if available >= line.quantity:
        return StockCheck(StockStatus.AVAILABLE, available, level, "Enough stock reported")
    if available > 0:
        return StockCheck(StockStatus.INSUFFICIENT, available, level, "Only part is in stock")
    return StockCheck(StockStatus.UNAVAILABLE, available, level, "No stock reported")
