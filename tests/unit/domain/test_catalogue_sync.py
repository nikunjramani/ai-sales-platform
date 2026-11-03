from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sales_platform.domain.catalogue_admin import MAX_ACTIVE_PRODUCTS, InvalidCatalogueEntry
from sales_platform.domain.catalogue_sync import (
    DEFAULT_SYNC_MINUTES,
    SYNC_MINUTES_KEY,
    CatalogueFeed,
    FeedPrice,
    FeedProduct,
    FeedStock,
    UnitMap,
    quantity_step,
    summed_stock,
    sync_minutes,
)

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def _product(sku: str = "PUMP-100", unit: str = "each") -> FeedProduct:
    return FeedProduct(sku=sku, name="Pump", unit=unit, external_id=sku)


def test_units_are_renamed_exactly_never_guessed() -> None:
    units = UnitMap.parse("Nos=each, Kgs = kg\nMtr=metre")
    assert units.unit_for("NOS") == "each"
    assert units.unit_for(" kgs ") == "kg"
    # Without a rename, only the unit words the enquiry reader already knows are used.
    assert units.unit_for("Meter") == "metre"
    assert units.unit_for("Box") == "box"
    assert units.unit_for("Litre") is None  # reported as an issue, never invented


def test_a_unit_map_refuses_unknown_targets_and_malformed_parts() -> None:
    for text in ("Nos=pieces", "Nos", "=each", "Nos=each, Ltr="):
        with pytest.raises(ValueError, match="CODE=unit"):
            UnitMap.parse(text)
    assert UnitMap.parse("").renames == {}


def test_stock_is_summed_over_the_chosen_warehouses() -> None:
    balances = [(Decimal("12"), Decimal("3")), (Decimal("5.5"), Decimal("0"))]
    assert summed_stock(balances, available=True) == Decimal("14.5")
    assert summed_stock(balances, available=False) == Decimal("17.5")
    assert str(summed_stock([(Decimal("10.000"), Decimal(0))], available=True)) == "10"


def test_negative_erp_stock_means_none_available() -> None:
    assert summed_stock([(Decimal("-4"), Decimal("0"))], available=False) == 0
    assert summed_stock([(Decimal("2"), Decimal("5"))], available=True) == 0
    assert summed_stock([], available=True) == 0


def test_feed_products_follow_the_catalogue_rules() -> None:
    assert _product().quantity_step == 1
    with pytest.raises(InvalidCatalogueEntry):
        _product(sku="PUMP 100")  # spaces are not allowed in our SKUs
    with pytest.raises(InvalidCatalogueEntry):
        _product(unit="litre")
    long = FeedProduct("A-1", "x" * 300, "each", "A-1", category="  Pumps   and  cables ")
    assert len(long.name) == 200 and long.category == "Pumps and cables"


def test_feed_prices_and_stock_refuse_impossible_values() -> None:
    with pytest.raises(InvalidCatalogueEntry):
        FeedPrice("A-1", Decimal("0"), "INR", "each", NOW)
    with pytest.raises(InvalidCatalogueEntry):
        FeedStock("A-1", Decimal("-1"), "each", NOW)
    assert FeedStock("A-1", None, "each", NOW).quantity is None  # not tracked


def test_a_feed_has_each_sku_once_and_stays_within_the_product_limit() -> None:
    with pytest.raises(ValueError, match="same SKU"):
        CatalogueFeed((_product("A-1"), _product("a-1")))
    too_many = tuple(_product(f"P-{index}") for index in range(MAX_ACTIVE_PRODUCTS + 1))
    with pytest.raises(ValueError, match="at most"):
        CatalogueFeed(too_many)


def test_whole_number_units_step_by_one() -> None:
    assert quantity_step(True) == 1
    assert quantity_step(False) == Decimal("0.001")


def test_the_sync_interval_comes_from_the_generic_config_key() -> None:
    assert sync_minutes({SYNC_MINUTES_KEY: 30}) == 30
    for value in (None, 0, 2000, True, "15"):
        assert sync_minutes({SYNC_MINUTES_KEY: value}) == DEFAULT_SYNC_MINUTES
    assert sync_minutes({}) == DEFAULT_SYNC_MINUTES
