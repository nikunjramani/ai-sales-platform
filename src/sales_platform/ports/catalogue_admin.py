"""Storage of our own product catalogue (phase 3, ADR 0020).

Two ports over the same tables:
- CatalogueReadStore: the few reads LocalCatalogue needs to act as the business's ERP. The
  adapter layer may not run SQL itself, so it reads through this port.
- CatalogueStore: everything the product screens need (ProductService).

Rules for implementations:
- Every call is scoped to one business; another business's rows are never returned or changed.
- Prices and stock counts are only ever added, never changed or deleted.
- Products and categories carry a row_version; a write with an older version raises
  VersionConflict instead of overwriting someone else's change.
- Every product and category change is recorded in the change log in the same transaction.
- A store that cannot be reached raises CatalogueUnavailable (never an empty result).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sales_platform.domain.catalogue_admin import (
    CatalogueProduct,
    Category,
    ChangeRecord,
    EffectivePrice,
    PriceEntry,
    ProductFields,
    StockEntry,
)
from sales_platform.domain.tenancy import TenantContext


class CatalogueUnavailable(Exception):
    """The catalogue tables could not be read; callers retry later."""


class ProductNotFound(LookupError):
    """No such product in this business (also for another business's ID)."""


class CategoryNotFound(LookupError):
    """No such category in this business."""


class SkuAlreadyExists(Exception):
    """Another product of this business has the same SKU (ignoring case)."""


class CategoryNameTaken(Exception):
    """Another category of this business has the same name (ignoring case)."""


class VersionConflict(Exception):
    """Someone changed the row first, or a request ID was reused with different values."""


class ProductReferenced(Exception):
    """The product has prices, stock counts or quotations, so it can only be archived (Q12)."""


class CatalogueLimitReached(Exception):
    """The business already has the maximum number of active products (Q13)."""


class ProductArchived(Exception):
    """Prices and stock can only be entered for active products."""


class ProductState(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    ALL = "all"


@dataclass(frozen=True)
class ProductQuery:
    text: str | None = None  # part of the SKU or name, any case
    category_id: UUID | None = None
    state: ProductState = ProductState.ACTIVE
    limit: int = 50
    after: str | None = None  # paging cursor: the last SKU (lower case) of the previous page


@dataclass(frozen=True)
class ProductPage:
    items: tuple[CatalogueProduct, ...]
    next_after: str | None


@dataclass(frozen=True)
class CatalogueCounts:
    active_products: int
    priced_products: int  # active, with a price in force in the business currency
    stale_stock: int  # active, current count older than the business's max stock age
    unknown_stock: int  # active, no count or "not tracked"


@dataclass(frozen=True)
class AddedEntry[T]:
    entry: T
    replayed: bool  # the same request ID was already stored; nothing new was added


class CatalogueReadStore(Protocol):
    def search(
        self, tenant_id: UUID, terms: frozenset[str], limit: int
    ) -> Sequence[CatalogueProduct]:
        """Active products sharing the most terms (domain.catalogue_search), in SKU order."""
        ...

    def active_products(self, tenant_id: UUID, skus: Sequence[str]) -> Sequence[CatalogueProduct]:
        """Active products with exactly these SKUs; unknown or archived ones are absent."""
        ...

    def effective_prices(
        self, tenant_id: UUID, skus: Sequence[str], currency: str, at: datetime, price_list: str
    ) -> Mapping[str, EffectivePrice]:
        """The price in force at `at` per active product's SKU; absent when there is none."""
        ...

    def current_stock(
        self, tenant_id: UUID, skus: Sequence[str], location: str
    ) -> Mapping[str, StockEntry]:
        """The latest count per active product's SKU at `location`; absent when none."""
        ...

    def category_names_in_use(self, tenant_id: UUID) -> Sequence[str]:
        """Names of active categories with at least one active product, in code-point order."""
        ...


class CatalogueStore(Protocol):
    # products
    def list_products(self, tenant_id: UUID, query: ProductQuery) -> ProductPage: ...

    def get_product(self, tenant_id: UUID, product_id: UUID) -> CatalogueProduct: ...

    def create_product(
        self, context: TenantContext, sku: str, fields: ProductFields, max_active: int
    ) -> CatalogueProduct:
        """Raises SkuAlreadyExists, CategoryNotFound or CatalogueLimitReached."""
        ...

    def update_product(
        self,
        context: TenantContext,
        product_id: UUID,
        fields: ProductFields,
        expected_version: int,
    ) -> CatalogueProduct:
        """Raises VersionConflict, CategoryNotFound, or InvalidCatalogueEntry when the unit
        would change although a price or stock count exists (checked under the row lock)."""
        ...

    def set_archived(
        self,
        context: TenantContext,
        product_id: UUID,
        archived: bool,
        expected_version: int,
        max_active: int,
    ) -> CatalogueProduct:
        """Archive or restore; restoring checks the active-product limit."""
        ...

    def delete_product(self, context: TenantContext, product_id: UUID) -> None:
        """Only a product never priced, counted or quoted; otherwise ProductReferenced."""
        ...

    # prices and stock (append-only)
    def list_prices(self, tenant_id: UUID, product_id: UUID) -> Sequence[PriceEntry]: ...

    def add_price(
        self,
        context: TenantContext,
        product_id: UUID,
        command_id: UUID,
        unit_price: Decimal,
        currency: str,
        effective_from: datetime,
        note: str | None,
        price_list: str,
    ) -> AddedEntry[PriceEntry]:
        """Next revision for the product, list and currency, in the product's current unit."""
        ...

    def list_stock(self, tenant_id: UUID, product_id: UUID) -> Sequence[StockEntry]: ...

    def add_stock(
        self,
        context: TenantContext,
        product_id: UUID,
        command_id: UUID,
        quantity: Decimal | None,
        as_of: datetime,
        note: str | None,
        location: str,
    ) -> AddedEntry[StockEntry]: ...

    def list_changes(self, tenant_id: UUID, entity_id: UUID) -> Sequence[ChangeRecord]: ...

    # categories
    def list_categories(self, tenant_id: UUID) -> Sequence[Category]: ...

    def create_category(self, context: TenantContext, name: str) -> Category: ...

    def update_category(
        self,
        context: TenantContext,
        category_id: UUID,
        name: str | None,
        archived: bool | None,
        expected_version: int,
    ) -> Category:
        """Rename and/or archive; renaming refreshes its products' search words."""
        ...

    def counts(
        self, tenant_id: UUID, currency: str | None, at: datetime, stale_before: datetime | None
    ) -> CatalogueCounts: ...
