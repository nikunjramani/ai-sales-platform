from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from sales_platform.domain.catalogue_admin import (
    CATALOGUE_UNITS,
    EntrySource,
    InvalidCatalogueEntry,
    PriceEntry,
    PriceState,
    ProductFields,
    StockEntry,
    check_category_name,
    check_price,
    check_product_fields,
    check_sku,
    check_stock,
    check_unit_change,
    current_stock,
    effective_price,
    field_changes,
    normalise_price,
    price_state,
    scheduled_price,
    stock_is_stale,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _field(error: pytest.ExceptionInfo[InvalidCatalogueEntry]) -> str:
    return error.value.field


def test_units_are_the_ones_the_reader_understands() -> None:
    assert CATALOGUE_UNITS == {"each", "metre", "kg", "box"}


@pytest.mark.parametrize("sku", ["IND-FAN-450", "a", "X1_2.3/4", "9" * 64])
def test_valid_skus(sku: str) -> None:
    assert check_sku(sku) == sku


@pytest.mark.parametrize("sku", ["", "-FAN", " FAN", "FAN 450", "FAN#1", "9" * 65, "ÄBC"])
def test_invalid_skus(sku: str) -> None:
    with pytest.raises(InvalidCatalogueEntry) as error:
        check_sku(sku)
    assert _field(error) == "sku"


def test_product_fields_are_cleaned_and_default_to_one() -> None:
    fields = check_product_fields(ProductFields(name="  Fan 450 ", unit="each", description=" "))
    assert fields == ProductFields(name="Fan 450", unit="each")
    assert fields.minimum_quantity == 1 and fields.quantity_step == 1


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": " "}, "name"),
        ({"name": "x" * 201}, "name"),
        ({"unit": "litre"}, "unit"),
        ({"description": "d" * 2001}, "description"),
        ({"minimum_quantity": Decimal(0)}, "minimum_quantity"),
        ({"quantity_step": Decimal(-1)}, "quantity_step"),
        ({"quantity_step": Decimal("0.0001")}, "quantity_step"),
        ({"minimum_quantity": Decimal(10) ** 9}, "minimum_quantity"),
        ({"minimum_quantity": Decimal(75), "quantity_step": Decimal(50)}, "minimum_quantity"),
        ({"minimum_quantity": Decimal("NaN")}, "minimum_quantity"),
    ],
)
def test_product_field_rules(changes: dict[str, object], field: str) -> None:
    values: dict[str, object] = {"name": "Bolt", "unit": "each", **changes}
    with pytest.raises(InvalidCatalogueEntry) as error:
        check_product_fields(ProductFields(**values))  # type: ignore[arg-type]
    assert _field(error) == field


def test_minimum_multiple_of_step_and_trailing_zeros_dropped() -> None:
    fields = check_product_fields(
        ProductFields(
            "Cable", "metre", minimum_quantity=Decimal("1.5000"), quantity_step=Decimal("0.5")
        )
    )
    assert str(fields.minimum_quantity) == "1.500"


def test_unit_is_locked_once_priced_or_counted() -> None:
    before = ProductFields("Bolt", "each")
    after = ProductFields("Bolt", "box")
    check_unit_change(before, after, has_entries=False)
    with pytest.raises(InvalidCatalogueEntry) as error:
        check_unit_change(before, after, has_entries=True)
    assert _field(error) == "unit"
    check_unit_change(before, ProductFields("Bolt 2", "each"), has_entries=True)


def test_category_name() -> None:
    assert check_category_name(" Fans ") == "Fans"
    with pytest.raises(InvalidCatalogueEntry):
        check_category_name("x" * 101)


@pytest.mark.parametrize(
    ("amount", "minor_units", "expected"),
    [
        (Decimal(4250), 2, "4250.00"),
        (Decimal("2.375"), 2, "2.375"),
        (Decimal("4250.5"), 0, "4250.5"),
        (Decimal(7), None, "7"),
    ],
)
def test_price_keeps_at_least_the_currency_decimals(
    amount: Decimal, minor_units: int | None, expected: str
) -> None:
    assert str(normalise_price(amount, minor_units)) == expected


def test_price_entry_defaults_to_now_and_allows_future() -> None:
    assert check_price(Decimal(10), None, now=NOW, minor_units=2) == (Decimal("10.00"), NOW)
    later = NOW + timedelta(days=3)
    assert check_price(Decimal(10), later, now=NOW, minor_units=2)[1] == later
    # within the clock tolerance
    check_price(Decimal(10), NOW - timedelta(minutes=4), now=NOW, minor_units=2)


@pytest.mark.parametrize(
    ("amount", "start", "field"),
    [
        (Decimal(0), None, "unit_price"),
        (Decimal(-1), None, "unit_price"),
        (Decimal("0.0000001"), None, "unit_price"),
        (Decimal(10) ** 12, None, "unit_price"),
        (Decimal(1), NOW - timedelta(minutes=6), "effective_from"),
        (Decimal(1), datetime(2026, 11, 1), "effective_from"),
    ],
)
def test_price_entry_rules(amount: Decimal, start: datetime | None, field: str) -> None:
    with pytest.raises(InvalidCatalogueEntry) as error:
        check_price(amount, start, now=NOW, minor_units=2)
    assert _field(error) == field


def test_stock_entry_rules() -> None:
    assert check_stock(None, None, now=NOW) == (None, NOW)
    assert check_stock(Decimal("0"), NOW - timedelta(days=30), now=NOW)[0] == 0
    for quantity, as_of, field in [
        (Decimal(-1), None, "quantity"),
        (Decimal("0.0001"), None, "quantity"),
        (Decimal(1), NOW + timedelta(minutes=6), "as_of"),
    ]:
        with pytest.raises(InvalidCatalogueEntry) as error:
            check_stock(quantity, as_of, now=NOW)
        assert _field(error) == field


def _price(start: datetime, revision: int, amount: str = "1") -> PriceEntry:
    return PriceEntry(
        uuid4(), uuid4(), "standard", revision, "INR", "each", Decimal(amount), start,
        EntrySource.MANUAL, None, start, "test",
    )  # fmt: skip


def test_effective_price_picks_latest_start_and_reports_next_change() -> None:
    old = _price(NOW - timedelta(days=10), 1)
    current = _price(NOW - timedelta(days=1), 2)
    upcoming = _price(NOW + timedelta(days=5), 3)
    result = effective_price([old, current, upcoming], NOW)
    assert result is not None
    assert result.entry is current and result.effective_to == upcoming.effective_from
    assert effective_price([upcoming], NOW) is None
    assert scheduled_price([old, current, upcoming], NOW) is upcoming
    assert price_state(old, [old, current, upcoming], NOW) is PriceState.PAST
    assert price_state(current, [old, current, upcoming], NOW) is PriceState.CURRENT
    assert price_state(upcoming, [old, current, upcoming], NOW) is PriceState.SCHEDULED


def test_same_start_higher_revision_wins_so_a_scheduled_price_can_be_replaced() -> None:
    start = NOW + timedelta(days=2)
    first, second = _price(start, 1, "5"), _price(start, 2, "6")
    assert scheduled_price([first, second], NOW) is second
    result = effective_price([first, second], start)
    assert result is not None and result.entry is second and result.effective_to is None


def _count(as_of: datetime, recorded_at: datetime, location: str = "MAIN") -> StockEntry:
    return StockEntry(
        uuid4(), uuid4(), location, Decimal(1), "each", as_of, EntrySource.MANUAL, None,
        recorded_at, "test",
    )  # fmt: skip


def test_current_stock_an_older_count_entered_later_does_not_hide_a_newer_one() -> None:
    newer = _count(NOW - timedelta(hours=1), NOW - timedelta(hours=1))
    older_later = _count(NOW - timedelta(days=2), NOW)
    assert current_stock([newer, older_later], "MAIN") is newer
    tie = _count(newer.as_of, NOW)
    assert current_stock([newer, tie], "MAIN") is tie
    assert current_stock([newer], "BERLIN") is None


def test_stock_staleness_matches_the_calculator() -> None:
    week = timedelta(days=7)
    assert not stock_is_stale(NOW - week, week, NOW)
    assert stock_is_stale(NOW - week - timedelta(seconds=1), week, NOW)


def test_field_changes_lists_only_changed_fields_as_text() -> None:
    before = {"name": "Fan", "minimum_quantity": Decimal(1), "description": None}
    after = {"name": "Fan", "minimum_quantity": Decimal(2), "description": "Big"}
    assert field_changes(before, after) == {
        "description": [None, "Big"],
        "minimum_quantity": ["1", "2"],
    }
