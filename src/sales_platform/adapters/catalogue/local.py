"""LocalCatalogue: our own product list behind the ERP port (phase 3, ADR 0020).

To the quotation core it looks exactly like an ERP: the same five reads, the same contract
tests, the same "best word matches only" search as the simulated ERP. It reads through the
CatalogueReadStore port (the adapter layer runs no SQL itself).

What the evidence means for our own data (Q10):
- price fetched_at = the moment of reading: the stored row is the source, so a typed price
  is never "stale"; the price list is "standard" and its version the row's revision (Q23);
- stock fetched_at = the "as of" time a person entered, so old counts are flagged stale by
  the business's max stock age, as with an ERP.
It never invents data: no price, no stock count or an archived product means absent.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime

from sales_platform.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.domain.catalogue_admin import (
    DEFAULT_LOCATION,
    LOCAL_PRICE_LIST,
    CatalogueProduct,
)
from sales_platform.domain.catalogue_search import query_terms
from sales_platform.domain.money import Money
from sales_platform.domain.tenancy import TenantContext
from sales_platform.domain.validation import require_aware
from sales_platform.ports.catalogue_admin import CatalogueReadStore, CatalogueUnavailable
from sales_platform.ports.erp import ErpError, ErpErrorCode


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _product(record: CatalogueProduct) -> Product:
    category = record.category
    return Product(
        sku=record.sku,
        description=record.fields.name,  # what customers see; the description is staff-only
        unit=record.fields.unit,
        minimum_quantity=record.fields.minimum_quantity,
        quantity_step=record.fields.quantity_step,
        category=category.name if category and category.archived_at is None else None,
    )


class LocalCatalogue:
    source = "local-catalogue"

    def __init__(
        self,
        store: CatalogueReadStore,
        clock: Callable[[], datetime] = _utc_now,
        location: str = DEFAULT_LOCATION,
    ) -> None:
        self._store = store
        self._clock = clock
        self._location = location

    def search_products(
        self, context: TenantContext, query: str, limit: int = 10
    ) -> Sequence[Product]:
        if not query.strip() or not 1 <= limit <= 50:
            raise ErpError(ErpErrorCode.INVALID_REQUEST, "Query and a limit of 1-50 required")
        terms = query_terms(query)
        if not terms:
            return []
        with _unavailable_as_retryable():
            return [_product(p) for p in self._store.search(context.tenant_id, terms, limit)]

    def list_categories(self, context: TenantContext) -> Sequence[str]:
        with _unavailable_as_retryable():
            return list(self._store.category_names_in_use(context.tenant_id))

    def get_products(self, context: TenantContext, skus: Sequence[str]) -> Mapping[str, Product]:
        with _unavailable_as_retryable():
            found = self._store.active_products(context.tenant_id, skus)
        return {record.sku: _product(record) for record in found}

    def get_prices(
        self,
        context: TenantContext,
        skus: Sequence[str],
        currency: str,
        effective_at: datetime,
    ) -> Mapping[str, PriceQuote]:
        require_aware(effective_at, "effective_at")
        with _unavailable_as_retryable():
            prices = self._store.effective_prices(
                context.tenant_id, skus, currency, effective_at, LOCAL_PRICE_LIST
            )
        fetched_at = self._clock()
        return {
            sku: PriceQuote(
                sku=sku,
                unit_price=Money(price.entry.unit_price, price.entry.currency),
                unit=price.entry.unit,
                price_list_id=price.entry.price_list,
                price_list_version=str(price.entry.revision),
                effective_from=price.entry.effective_from,
                effective_to=price.effective_to,
                fetched_at=fetched_at,
                source=self.source,
            )
            for sku, price in prices.items()
        }

    def get_availability(
        self, context: TenantContext, skus: Sequence[str], warehouse: str | None = None
    ) -> Mapping[str, StockLevel]:
        if warehouse is not None and warehouse != self._location:
            return {}
        with _unavailable_as_retryable():
            counts = self._store.current_stock(context.tenant_id, skus, self._location)
        return {
            sku: StockLevel(
                sku=sku,
                unit=count.unit,
                available_quantity=count.quantity,
                warehouse=count.location,
                fetched_at=count.as_of,
                source=self.source,
            )
            for sku, count in counts.items()
        }


@contextmanager
def _unavailable_as_retryable() -> Iterator[None]:
    """A store that cannot be reached is a temporary ERP failure, so jobs retry."""
    try:
        yield
    except CatalogueUnavailable as error:
        raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, str(error)) from error
