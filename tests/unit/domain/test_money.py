from decimal import Decimal, Inexact

import pytest

from sales_platform.domain.money import Money, RoundingMode, exact_multiply, round_to_minor_units


def test_money_rejects_float_and_invalid_currency() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        Money(0.1, "INR")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(Decimal("NaN"), "INR")
    for code in ("inr", "RUPEE", "IN"):
        with pytest.raises(ValueError, match="ISO 4217"):
            Money(Decimal(1), code)


@pytest.mark.parametrize(
    ("amount", "mode", "minor_units", "expected"),
    [
        ("0.125", RoundingMode.HALF_UP, 2, "0.13"),
        ("0.125", RoundingMode.HALF_EVEN, 2, "0.12"),
        ("0.135", RoundingMode.HALF_EVEN, 2, "0.14"),
        ("-0.125", RoundingMode.HALF_UP, 2, "-0.13"),
        ("12.5", RoundingMode.HALF_EVEN, 0, "12"),
        ("237.5", RoundingMode.HALF_UP, 2, "237.50"),
    ],
)
def test_rounding_follows_named_policy(
    amount: str, mode: RoundingMode, minor_units: int, expected: str
) -> None:
    result = round_to_minor_units(Decimal(amount), minor_units, mode)
    assert result == Decimal(expected)
    assert str(result) == expected  # scale is fixed, e.g. 237.50 not 237.5


def test_multiplication_refuses_silent_precision_loss() -> None:
    assert exact_multiply(Decimal("2.375"), Decimal(100)) == Decimal("237.500")
    with pytest.raises(Inexact):
        exact_multiply(Decimal("1." + "1" * 20), Decimal("1." + "1" * 20))
