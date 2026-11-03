"""ProductService: the product screens' use cases (phase 3, ADR 0020).

Who may do what (Q14): every staff role may read the catalogue (catalogue:view); only
administrators and sales operators change it (catalogue:manage). Changes are refused when:
- the business's product source is not editable here (an ERP or the simulated demo): its
  screens are read-only (Q18), decided by the provider's capability, never by its name;
- the business is suspended (read-only, like its other settings).

Prices use the business's quotation currency only (Q7) and are stored with at least its
minor units. Every price or stock entry carries a request ID (command_id), so a repeated
submit returns the stored entry instead of adding a second one.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.catalogue_admin import (
    CATALOGUE_UNITS,
    DEFAULT_LOCATION,
    EDITABLE_FIELDS,
    LOCAL_PRICE_LIST,
    MAX_ACTIVE_PRODUCTS,
    CatalogueProduct,
    Category,
    ChangeRecord,
    EffectivePrice,
    InvalidCatalogueEntry,
    PriceEntry,
    PriceState,
    ProductFields,
    StockEntry,
    check_category_name,
    check_note,
    check_price,
    check_product_fields,
    check_sku,
    check_stock,
    current_stock,
    effective_price,
    price_state,
    scheduled_price,
    stock_is_stale,
)
from sales_platform.domain.tenancy import Permission, TenantContext, allows_changes
from sales_platform.ports.accounts import BusinessStore, NotAllowed, PricingSettings
from sales_platform.ports.catalogue_admin import (
    AddedEntry,
    CatalogueCounts,
    CatalogueReadStore,
    CatalogueStore,
    CategoryNotFound,
    ProductPage,
    ProductQuery,
)
from sales_platform.ports.connections import ConnectionStore


def utc_now() -> datetime:
    return datetime.now(UTC)


class SourceNotEditable(Exception):
    """The business's products come from another source; edit them there (Q18)."""


class CurrencyNotSet(Exception):
    """Prices need the business's quotation currency, which is still "set later"."""


@dataclass(frozen=True)
class CatalogueSource:
    provider_key: str
    display_name: str
    editable: bool


@dataclass(frozen=True)
class CatalogueSummary:
    source: CatalogueSource | None
    counts: CatalogueCounts
    max_active_products: int
    units: tuple[str, ...]
    currency: str | None
    minor_units: int | None
    max_stock_age: timedelta | None
    time_zone: str | None  # a scheduled price starts at midnight here (Q8)


@dataclass(frozen=True)
class StockView:
    entry: StockEntry
    stale: bool


@dataclass(frozen=True)
class ProductDetail:
    product: CatalogueProduct
    current_price: EffectivePrice | None
    scheduled_price: PriceEntry | None
    stock: StockView | None


@dataclass(frozen=True)
class ProductListItem:
    product: CatalogueProduct
    current_price: EffectivePrice | None
    stock: StockView | None


@dataclass(frozen=True)
class ProductListPage:
    items: tuple[ProductListItem, ...]
    next_after: str | None


@dataclass(frozen=True)
class PriceHistoryItem:
    entry: PriceEntry
    state: PriceState


class ProductService:
    def __init__(
        self,
        store: CatalogueStore,
        reader: CatalogueReadStore,
        businesses: BusinessStore,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._reader = reader
        self._businesses = businesses
        self._connections = connections
        self._registry = registry
        self._clock = clock

    # --- reads -------------------------------------------------------------------------------

    def summary(self, context: TenantContext) -> CatalogueSummary:
        context.require(Permission.VIEW_CATALOGUE)
        pricing = self._pricing(context)
        now = self._clock()
        max_age = _seconds(pricing.max_stock_age_seconds)
        counts = self._store.counts(
            context.tenant_id, pricing.currency, now, now - max_age if max_age else None
        )
        return CatalogueSummary(
            source=self.source(context),
            counts=counts,
            max_active_products=MAX_ACTIVE_PRODUCTS,
            units=tuple(sorted(CATALOGUE_UNITS)),
            currency=pricing.currency,
            minor_units=pricing.minor_units,
            max_stock_age=max_age,
            time_zone=self._businesses.settings(context).time_zone,
        )

    def source(self, context: TenantContext) -> CatalogueSource | None:
        """The business's product source; editable only when its provider says so."""
        sources = self._connections.catalogue_sources(context.tenant_id)
        if not sources:
            return None
        connection = sources[0]
        provider = self._registry.find(connection.provider_key)
        editable = provider is not None and provider.capabilities.catalogue_editable
        return CatalogueSource(connection.provider_key, connection.display_name, editable)

    def has_product_data(self, context: TenantContext) -> bool:
        """The setup checklist's product step (Q19, shown but not blocking Go live): our own
        list needs at least one active product with a price in force; another source is
        enough by being connected (its data lives there)."""
        source = self.source(context)
        if source is None:
            return False
        if not source.editable:
            return True
        currency = self._pricing(context).currency
        if currency is None:
            return False
        return (
            self._store.counts(context.tenant_id, currency, self._clock(), None).priced_products > 0
        )

    def list_products(self, context: TenantContext, query: ProductQuery) -> ProductListPage:
        context.require(Permission.VIEW_CATALOGUE)
        if not 1 <= query.limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        page: ProductPage = self._store.list_products(context.tenant_id, query)
        pricing = self._pricing(context)
        now = self._clock()
        skus = [item.sku for item in page.items if item.active]
        prices: Mapping[str, EffectivePrice] = {}
        if pricing.currency is not None and skus:
            prices = self._reader.effective_prices(
                context.tenant_id, skus, pricing.currency, now, LOCAL_PRICE_LIST
            )
        counts = (
            self._reader.current_stock(context.tenant_id, skus, DEFAULT_LOCATION) if skus else {}
        )
        return ProductListPage(
            tuple(
                ProductListItem(
                    item,
                    prices.get(item.sku),
                    self._stock_view(counts.get(item.sku), pricing, now),
                )
                for item in page.items
            ),
            page.next_after,
        )

    def get_product(self, context: TenantContext, product_id: UUID) -> ProductDetail:
        context.require(Permission.VIEW_CATALOGUE)
        product = self._store.get_product(context.tenant_id, product_id)
        return self._detail(context, product)

    def price_history(self, context: TenantContext, product_id: UUID) -> list[PriceHistoryItem]:
        context.require(Permission.VIEW_CATALOGUE)
        entries = self._store.list_prices(context.tenant_id, product_id)
        now = self._clock()
        result = []
        for entry in entries:
            same_list = [
                other
                for other in entries
                if (other.price_list, other.currency) == (entry.price_list, entry.currency)
            ]
            result.append(PriceHistoryItem(entry, price_state(entry, same_list, now)))
        return result

    def stock_history(self, context: TenantContext, product_id: UUID) -> Sequence[StockEntry]:
        context.require(Permission.VIEW_CATALOGUE)
        return self._store.list_stock(context.tenant_id, product_id)

    def changes(self, context: TenantContext, entity_id: UUID) -> Sequence[ChangeRecord]:
        context.require(Permission.VIEW_CATALOGUE)
        return self._store.list_changes(context.tenant_id, entity_id)

    def list_categories(self, context: TenantContext) -> Sequence[Category]:
        context.require(Permission.VIEW_CATALOGUE)
        return self._store.list_categories(context.tenant_id)

    # --- writes ------------------------------------------------------------------------------

    def create_product(
        self, context: TenantContext, sku: str, fields: ProductFields
    ) -> ProductDetail:
        self._writable(context)
        with _category_field():
            product = self._store.create_product(
                context, check_sku(sku), check_product_fields(fields), MAX_ACTIVE_PRODUCTS
            )
        return self._detail(context, product)

    def update_product(
        self,
        context: TenantContext,
        product_id: UUID,
        changes: Mapping[str, object],
        expected_version: int,
    ) -> ProductDetail:
        """Change only the given fields; the SKU never changes (Q2)."""
        self._writable(context)
        unknown = set(changes) - EDITABLE_FIELDS
        if unknown:
            field = sorted(unknown)[0]
            raise InvalidCatalogueEntry(field, f"{field} cannot be changed")
        current = self._store.get_product(context.tenant_id, product_id)
        fields = check_product_fields(replace(current.fields, **changes))  # type: ignore[arg-type]
        with _category_field():
            product = self._store.update_product(context, product_id, fields, expected_version)
        return self._detail(context, product)

    def archive_product(
        self, context: TenantContext, product_id: UUID, expected_version: int
    ) -> ProductDetail:
        return self._set_archived(context, product_id, True, expected_version)

    def restore_product(
        self, context: TenantContext, product_id: UUID, expected_version: int
    ) -> ProductDetail:
        return self._set_archived(context, product_id, False, expected_version)

    def delete_product(self, context: TenantContext, product_id: UUID) -> None:
        self._writable(context)
        self._store.delete_product(context, product_id)

    def add_price(
        self,
        context: TenantContext,
        product_id: UUID,
        unit_price: Decimal,
        effective_from: datetime | None,
        note: str | None,
        command_id: UUID,
    ) -> AddedEntry[PriceEntry]:
        self._writable(context)
        pricing = self._pricing(context)
        if pricing.currency is None:
            raise CurrencyNotSet("Set the business currency in Pricing before adding prices")
        amount, start = check_price(
            unit_price, effective_from, now=self._clock(), minor_units=pricing.minor_units
        )
        return self._store.add_price(
            context, product_id, command_id, amount, pricing.currency, start, check_note(note),
            LOCAL_PRICE_LIST,
        )  # fmt: skip

    def record_stock(
        self,
        context: TenantContext,
        product_id: UUID,
        quantity: Decimal | None,
        as_of: datetime | None,
        note: str | None,
        command_id: UUID,
    ) -> AddedEntry[StockEntry]:
        self._writable(context)
        count, moment = check_stock(quantity, as_of, now=self._clock())
        return self._store.add_stock(
            context, product_id, command_id, count, moment, check_note(note), DEFAULT_LOCATION
        )

    def create_category(self, context: TenantContext, name: str) -> Category:
        self._writable(context)
        return self._store.create_category(context, check_category_name(name))

    def update_category(
        self,
        context: TenantContext,
        category_id: UUID,
        name: str | None,
        archived: bool | None,
        expected_version: int,
    ) -> Category:
        self._writable(context)
        cleaned = None if name is None else check_category_name(name)
        return self._store.update_category(
            context, category_id, cleaned, archived, expected_version
        )

    # --- helpers -----------------------------------------------------------------------------

    def _writable(self, context: TenantContext) -> None:
        context.require(Permission.MANAGE_CATALOGUE)
        if not allows_changes(self._businesses.status(context.tenant_id)):
            raise NotAllowed("This business is suspended")
        source = self.source(context)
        if source is None or not source.editable:
            name = source.display_name if source else "no product source"
            raise SourceNotEditable(f"Products come from {name}; change them there")

    def _set_archived(
        self, context: TenantContext, product_id: UUID, archived: bool, expected_version: int
    ) -> ProductDetail:
        self._writable(context)
        product = self._store.set_archived(
            context, product_id, archived, expected_version, MAX_ACTIVE_PRODUCTS
        )
        return self._detail(context, product)

    def _pricing(self, context: TenantContext) -> PricingSettings:
        return self._businesses.pricing(context).settings

    def _stock_view(
        self, entry: StockEntry | None, pricing: PricingSettings, now: datetime
    ) -> StockView | None:
        if entry is None:
            return None
        max_age = _seconds(pricing.max_stock_age_seconds)
        stale = (
            entry.quantity is not None
            and max_age is not None
            and stock_is_stale(entry.as_of, max_age, now)
        )
        return StockView(entry, stale)

    def _detail(self, context: TenantContext, product: CatalogueProduct) -> ProductDetail:
        pricing = self._pricing(context)
        now = self._clock()
        entries = [
            entry
            for entry in self._store.list_prices(context.tenant_id, product.product_id)
            if entry.currency == pricing.currency and entry.price_list == LOCAL_PRICE_LIST
        ]
        counts = self._store.list_stock(context.tenant_id, product.product_id)
        return ProductDetail(
            product,
            effective_price(entries, now) if product.active else None,
            scheduled_price(entries, now),
            self._stock_view(current_stock(counts, DEFAULT_LOCATION), pricing, now),
        )


@contextmanager
def _category_field() -> Iterator[None]:
    """In a product form an unknown category is a field problem, not a missing page."""
    try:
        yield
    except CategoryNotFound:
        raise InvalidCatalogueEntry("category_id", "Choose one of your categories") from None


def _seconds(value: int | None) -> timedelta | None:
    return None if value is None else timedelta(seconds=value)
