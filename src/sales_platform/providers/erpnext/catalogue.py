"""ERPNext as a business's product source: a copy for searching, live checks for prices and
stock (phase 6, user answer "hybrid", ADR 0023).

- search_products, list_categories and get_products read our synced copy (LocalCatalogue over
  the catalogue tables), so matching uses our own search and still works if ERPNext is slow.
- get_prices and get_availability ask ERPNext itself for the final lines, so a quotation never
  uses a copied price or count. If ERPNext cannot be reached the job retries, as with any ERP.
- build_feed reads the whole catalogue for the sync job.

ERPNext facts used (Frappe doctypes): Item (item_code, item_name, item_group, stock_uom,
is_stock_item, is_sales_item, disabled, has_variants), UOM (must_be_whole_number), Price List
(currency, selling, enabled), Item Price (price_list_rate, currency, uom, valid_from,
valid_upto, customer; general prices have no customer), Bin (actual_qty, reserved_qty per
warehouse). Item groups are a tree, so a chosen group includes its sub-groups.
"""

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sales_platform.catalogue.adapters.catalogue.local import LocalCatalogue
from sales_platform.catalogue.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.catalogue.domain.catalogue_admin import InvalidCatalogueEntry
from sales_platform.catalogue.domain.catalogue_sync import (
    CatalogueFeed,
    FeedIssue,
    FeedPrice,
    FeedProduct,
    FeedStock,
    UnitMap,
    quantity_step,
    summed_stock,
)
from sales_platform.catalogue.ports.erp import ErpError, ErpErrorCode
from sales_platform.platform.domain.money import Money
from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.platform.domain.validation import require_aware
from sales_platform.providers.erpnext.client import FrappeClient
from sales_platform.providers.erpnext.config import ErpnextSettings, StockMeaning

SOURCE = "erpnext"
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)  # an Item Price without "valid from"
_ITEM_FIELDS = (
    "item_code",
    "item_name",
    "item_group",
    "stock_uom",
    "is_stock_item",
)
_PRICE_FIELDS = (
    "name",
    "item_code",
    "price_list_rate",
    "currency",
    "uom",
    "valid_from",
    "valid_upto",
    "modified",
)
_BIN_FIELDS = ("item_code", "warehouse", "actual_qty", "reserved_qty")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _decimal(value: object) -> Decimal | None:
    """ERPNext sends floats; str() keeps the digits it shows (12.5 -> 12.5, not 12.4999...)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _day(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _start(day: date | None) -> datetime:
    # Item Price dates have no time zone; the day starts at 00:00 UTC (ADR 0023).
    return _EARLIEST if day is None else datetime.combine(day, time(), tzinfo=UTC)


class _PriceRow:
    """One general selling price of the chosen price list."""

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.sku = str(row.get("item_code") or "")
        self.rate = _decimal(row.get("price_list_rate"))
        self.currency = str(row.get("currency") or "")
        self.uom = str(row.get("uom") or "")
        self.valid_from = _day(row.get("valid_from"))
        self.valid_upto = _day(row.get("valid_upto"))
        self.version = str(row.get("modified") or row.get("name") or "1")[:64]

    def effective_at(self, moment: datetime) -> bool:
        day = moment.astimezone(UTC).date()
        starts = self.valid_from is None or self.valid_from <= day
        return starts and (self.valid_upto is None or day <= self.valid_upto)

    def sort_key(self) -> tuple[date, str]:
        return (self.valid_from or date.min, self.version)


def _current_prices(
    rows: Sequence[Mapping[str, Any]],
    units: UnitMap,
    unit_of: Mapping[str, str],
    stock_uom: Mapping[str, str],
    moment: datetime,
) -> dict[str, _PriceRow]:
    """Per SKU: the price in force at `moment` in the product's own unit (no conversion).

    A row without a unit is in the item's stock unit. Several in force: the latest start wins,
    then the latest change.
    """
    chosen: dict[str, _PriceRow] = {}
    for raw in rows:
        price = _PriceRow(raw)
        unit = unit_of.get(price.sku)
        if unit is None or price.rate is None or price.rate <= 0:
            continue
        code = price.uom or stock_uom.get(price.sku, "")
        if units.unit_for(code) != unit or not price.effective_at(moment):
            continue
        best = chosen.get(price.sku)
        if best is None or price.sort_key() > best.sort_key():
            chosen[price.sku] = price
    return chosen


class ErpnextCatalogue:
    """ProductSource for one ERPNext connection."""

    source = SOURCE

    def __init__(
        self,
        client: FrappeClient,
        settings: ErpnextSettings,
        copy: LocalCatalogue,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._client = client
        self._settings = settings
        self._copy = copy
        self._clock = clock

    # --- from our copy ---------------------------------------------------------------------
    def search_products(
        self, context: TenantContext, query: str, limit: int = 10
    ) -> Sequence[Product]:
        return self._copy.search_products(context, query, limit)

    def list_categories(self, context: TenantContext) -> Sequence[str]:
        return self._copy.list_categories(context)

    def get_products(self, context: TenantContext, skus: Sequence[str]) -> Mapping[str, Product]:
        return self._copy.get_products(context, skus)

    # --- live from ERPNext -----------------------------------------------------------------
    def get_prices(
        self,
        context: TenantContext,
        skus: Sequence[str],
        currency: str,
        effective_at: datetime,
    ) -> Mapping[str, PriceQuote]:
        require_aware(effective_at, "effective_at")
        if self._settings.prices_include_tax:
            return {}  # tax-inclusive prices are never used (user answer, ADR 0010)
        products = self.get_products(context, skus)
        if not products:
            return {}
        price_list = self._client.get_doc("Price List", self._settings.price_list)
        if str(price_list.get("currency") or "") != currency:
            return {}  # another currency: no price, never a converted one
        items = self._client.get_list_in(
            "Item", ("item_code", "stock_uom"), "item_code", [*products]
        )
        stock_uom = {str(item["item_code"]): str(item.get("stock_uom") or "") for item in items}
        rows = self._client.get_list_in(
            "Item Price",
            _PRICE_FIELDS,
            "item_code",
            [*products],
            _general_price_filters(self._settings.price_list),
        )
        fetched_at = self._clock()
        found = _current_prices(
            rows,
            self._settings.units,
            {sku: product.unit for sku, product in products.items()},
            stock_uom,
            effective_at,
        )
        return {
            sku: PriceQuote(
                sku=sku,
                unit_price=Money(price.rate, currency),
                unit=products[sku].unit,
                price_list_id=self._settings.price_list,
                price_list_version=price.version,
                effective_from=_start(price.valid_from),
                effective_to=None
                if price.valid_upto is None
                else _start(price.valid_upto + timedelta(days=1)),
                fetched_at=fetched_at,
                source=SOURCE,
            )
            for sku, price in found.items()
            if price.rate is not None and price.currency == currency
        }

    def get_availability(
        self, context: TenantContext, skus: Sequence[str], warehouse: str | None = None
    ) -> Mapping[str, StockLevel]:
        chosen = self._settings.warehouses
        if warehouse is not None:
            if warehouse not in chosen:
                return {}
            chosen = (warehouse,)
        products = self.get_products(context, skus)
        if not products:
            return {}
        items = self._client.get_list_in(
            "Item", ("item_code", "is_stock_item"), "item_code", [*products]
        )
        tracked = {str(item["item_code"]) for item in items if item.get("is_stock_item")}
        known = {str(item["item_code"]) for item in items}
        bins = self._client.get_list_in(
            "Bin", _BIN_FIELDS, "item_code", sorted(tracked), [["warehouse", "in", list(chosen)]]
        )
        balances = _balances(bins)
        fetched_at = self._clock()
        label = chosen[0] if len(chosen) == 1 else f"{len(chosen)} warehouses"
        available = self._settings.stock_meaning is StockMeaning.AVAILABLE
        return {
            sku: StockLevel(
                sku=sku,
                unit=product.unit,
                # Not counted by ERPNext (a service): unknown. No Bin yet: ERPNext holds none.
                available_quantity=summed_stock(balances.get(sku, []), available=available)
                if sku in tracked
                else None,
                warehouse=label,
                fetched_at=fetched_at,
                source=SOURCE,
            )
            for sku, product in products.items()
            if sku in known
        }


def _general_price_filters(price_list: str) -> list[list[Any]]:
    return [["price_list", "=", price_list], ["selling", "=", 1], ["customer", "is", "not set"]]


def _balances(bins: Sequence[Mapping[str, Any]]) -> dict[str, list[tuple[Decimal, Decimal]]]:
    result: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for row in bins:
        actual = _decimal(row.get("actual_qty")) or Decimal(0)
        reserved = _decimal(row.get("reserved_qty")) or Decimal(0)
        result[str(row.get("item_code") or "")].append((actual, reserved))
    return result


def build_feed(client: FrappeClient, settings: ErpnextSettings, now: datetime) -> CatalogueFeed:
    """The whole sellable catalogue of the connection, in our words, for one sync run."""
    price_list = client.get_doc("Price List", settings.price_list)
    if not price_list.get("enabled", 1) or not price_list.get("selling"):
        raise ErpError(
            ErpErrorCode.INVALID_REQUEST,
            f"The price list '{settings.price_list}' is not an enabled selling price list",
        )
    filters: list[list[Any]] = [
        ["disabled", "=", 0],
        ["is_sales_item", "=", 1],
        ["has_variants", "=", 0],
    ]
    items: dict[str, dict[str, Any]] = {}
    for group_filter in _group_filters(settings.item_groups):
        for row in client.get_list("Item", _ITEM_FIELDS, [*filters, *group_filter]):
            items.setdefault(str(row.get("item_code") or ""), row)
    whole = {
        str(row["name"]).casefold()
        for row in client.get_list("UOM", ("name", "must_be_whole_number"))
        if row.get("must_be_whole_number")
    }
    units = settings.units
    issues: list[FeedIssue] = []
    products: list[FeedProduct] = []
    for code, item in sorted(items.items()):
        stock_uom = str(item.get("stock_uom") or "")
        unit = units.unit_for(stock_uom)
        if unit is None:
            issues.append(
                FeedIssue(
                    "unmapped_unit",
                    f"Unit '{stock_uom}' has no name we know; add it to the unit names",
                    code,
                )
            )
            continue
        try:
            products.append(
                FeedProduct(
                    sku=code,
                    name=str(item.get("item_name") or code),
                    unit=unit,
                    external_id=code,
                    category=str(item.get("item_group") or "") or None,
                    quantity_step=quantity_step(stock_uom.casefold() in whole),
                    minimum_quantity=quantity_step(stock_uom.casefold() in whole),
                )
            )
        except InvalidCatalogueEntry as error:
            issues.append(FeedIssue("invalid_product", str(error), code[:64]))
    by_sku = {product.sku: product for product in products}
    prices = _feed_prices(
        client, settings, by_sku, items, now, issues, str(price_list.get("currency") or "")
    )
    stock = _feed_stock(client, settings, by_sku, items, now)
    # The copy keeps one count per product (the chosen warehouses summed) at our default
    # location, where the product screens read it; quotations check ERPNext live.
    return CatalogueFeed(tuple(products), prices, stock, tuple(issues))


def _group_filters(groups: Sequence[str]) -> list[list[list[Any]]]:
    if not groups:
        return [[]]
    return [[["item_group", "descendants of (inclusive)", group]] for group in groups]


def _feed_prices(
    client: FrappeClient,
    settings: ErpnextSettings,
    products: Mapping[str, FeedProduct],
    items: Mapping[str, Mapping[str, Any]],
    now: datetime,
    issues: list[FeedIssue],
    currency: str,
) -> tuple[FeedPrice, ...]:
    if settings.prices_include_tax:
        issues.append(
            FeedIssue("tax_included", "Prices that include tax are not used for quotations")
        )
        return ()
    rows = client.get_list("Item Price", _PRICE_FIELDS, _general_price_filters(settings.price_list))
    found = _current_prices(
        rows,
        settings.units,
        {sku: product.unit for sku, product in products.items()},
        {sku: str(items[sku].get("stock_uom") or "") for sku in products},
        now,
    )
    return tuple(
        FeedPrice(sku, price.rate, currency, products[sku].unit, _start(price.valid_from))
        for sku, price in sorted(found.items())
        if price.rate is not None
    )


def _feed_stock(
    client: FrappeClient,
    settings: ErpnextSettings,
    products: Mapping[str, FeedProduct],
    items: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> tuple[FeedStock, ...]:
    bins = client.get_list("Bin", _BIN_FIELDS, [["warehouse", "in", list(settings.warehouses)]])
    balances = _balances(bins)
    available = settings.stock_meaning is StockMeaning.AVAILABLE
    return tuple(
        FeedStock(
            sku,
            summed_stock(balances.get(sku, []), available=available)
            if items[sku].get("is_stock_item")
            else None,
            product.unit,
            now,
        )
        for sku, product in sorted(products.items())
    )
