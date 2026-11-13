"""Test double: the demo businesses' product source (replaces the removed simulated ERP).

Our own catalogue logic (LocalCatalogue) over the in-memory store, filled with the seed's demo
products. `failures` makes one operation raise (e.g. the source being down); `store` lets a
test change prices or stock through the normal catalogue writes.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sales_platform.catalogue.adapters.catalogue.local import LocalCatalogue
from sales_platform.catalogue.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.catalogue.domain.catalogue_admin import DEFAULT_LOCATION, LOCAL_PRICE_LIST
from sales_platform.catalogue.ports.sources import ErpError
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.seed import DEMO_CATALOGUES
from tests.support.catalogue_store import InMemoryCatalogueStore


def _now() -> datetime:
    return datetime.now(UTC)


class DemoCatalogue:
    def __init__(self, tenants: Mapping[UUID, str], clock: Callable[[], datetime] = _now) -> None:
        """tenants: business ID -> demo catalogue name ("industrial" or "office")."""
        self.store = InMemoryCatalogueStore.from_demo(
            {tenant: DEMO_CATALOGUES[name] for tenant, name in tenants.items()}, clock
        )
        self._reader = LocalCatalogue(self.store)
        self._clock = clock
        self.failures: dict[str, ErpError] = {}

    def _fail(self, operation: str) -> None:
        if operation in self.failures:
            raise self.failures[operation]

    def search_products(
        self, context: TenantContext, query: str, limit: int = 10
    ) -> Sequence[Product]:
        self._fail("search_products")
        return self._reader.search_products(context, query, limit)

    def list_categories(self, context: TenantContext) -> Sequence[str]:
        self._fail("list_categories")
        return self._reader.list_categories(context)

    def get_products(self, context: TenantContext, skus: Sequence[str]) -> Mapping[str, Product]:
        self._fail("get_products")
        return self._reader.get_products(context, skus)

    def get_prices(
        self, context: TenantContext, skus: Sequence[str], currency: str, effective_at: datetime
    ) -> Mapping[str, PriceQuote]:
        self._fail("get_prices")
        return self._reader.get_prices(context, skus, currency, effective_at)

    def get_availability(
        self, context: TenantContext, skus: Sequence[str], warehouse: str | None = None
    ) -> Mapping[str, StockLevel]:
        self._fail("get_availability")
        return self._reader.get_availability(context, skus, warehouse)

    # --- test helpers ----------------------------------------------------------------------
    def _product_id(self, tenant_id: UUID, sku: str) -> UUID:
        return next(
            product.product_id
            for owner, product in self.store._products.values()
            if owner == tenant_id and product.sku == sku
        )

    def set_price(self, tenant_id: UUID, sku: str, amount: str, currency: str = "INR") -> None:
        """A new price in force from now (as staff would enter it)."""
        context = TenantContext(tenant_id, "test", frozenset({Role.ADMINISTRATOR}))
        self.store.add_price(
            context, self._product_id(tenant_id, sku), uuid4(), Decimal(amount), currency,
            self._clock(), None, LOCAL_PRICE_LIST,
        )  # fmt: skip

    def clear_prices(self, tenant_id: UUID, sku: str) -> None:
        """The product has no price any more (it must then never be quoted)."""
        product_id = self._product_id(tenant_id, sku)
        self.store._prices = [
            item for item in self.store._prices if item[1].product_id != product_id
        ]

    def set_stock(self, tenant_id: UUID, sku: str, quantity: str | None) -> None:
        """A stock count as of now; None means "not tracked"."""
        context = TenantContext(tenant_id, "test", frozenset({Role.ADMINISTRATOR}))
        self.store.add_stock(
            context, self._product_id(tenant_id, sku), uuid4(),
            None if quantity is None else Decimal(quantity), self._clock(), None, DEFAULT_LOCATION,
        )  # fmt: skip
