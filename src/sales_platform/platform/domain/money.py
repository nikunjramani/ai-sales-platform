"""Money and rounding. Amounts are Decimal and every rounding step names its policy."""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, Inexact, localcontext
from enum import StrEnum

from sales_platform.platform.domain.validation import require_decimal

_CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def validate_currency(code: str) -> str:
    """Check the ISO 4217 shape only; which currencies a tenant may use is policy."""
    if not isinstance(code, str) or not _CURRENCY_CODE.fullmatch(code):
        raise ValueError("Currency must be a three-letter uppercase ISO 4217 code")
    return code


class RoundingMode(StrEnum):
    HALF_UP = "half_up"
    HALF_EVEN = "half_even"

    @property
    def decimal_rounding(self) -> str:
        return ROUND_HALF_UP if self is RoundingMode.HALF_UP else ROUND_HALF_EVEN


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        require_decimal(self.amount, "amount")
        validate_currency(self.currency)

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


def exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    """Multiply without silent precision loss.

    Raises decimal.Inexact if the exact product needs more than 28 significant digits,
    rather than quietly rounding before the policy's own rounding step.
    """
    with localcontext() as context:
        context.prec = 28
        context.traps[Inexact] = True
        return left * right


def round_to_minor_units(amount: Decimal, minor_units: int, mode: RoundingMode) -> Decimal:
    """Round to the currency's smallest unit, e.g. 2 decimals for INR or EUR."""
    if not 0 <= minor_units <= 4:
        raise ValueError("minor_units must be between 0 and 4")
    return amount.quantize(Decimal(1).scaleb(-minor_units), rounding=mode.decimal_rounding)
