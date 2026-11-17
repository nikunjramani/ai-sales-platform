"""Each business's product source, found through its catalogue connection (ADR 0018).

Before phase 1 the API and worker used one hardcoded simulated ERP for every business. Now a
business's active connection with Purpose.CATALOGUE names its provider, and the provider builds
the reader (provider.catalogue_reader). TenantCatalogue is an ProductSource that sends each call to
the reader of the calling business, so pricing, matching and acceptance code is unchanged.

No active source, or its provider is not installed: CatalogueSourceMissing (retrying cannot
help; the enquiry goes to review, P1-Q3/Q4). Readers are cached per business for `ttl`; a
changed connection (new ID or updated_at) gets a new reader.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sales_platform.catalogue.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.catalogue.ports.sources import (
    CatalogueSourceMissing,
    ProductSource,
    product_source_of,
)
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.ports.connections import ConnectionStore
from sales_platform.integrations.ports.providers import OperationUnsupported
from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.platform.ports.secrets import SecretStore


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Cached:
    key: tuple[UUID, datetime]  # connection ID and its updated_at
    reader: ProductSource
    checked_at: datetime


class CatalogueSourceResolver:
    def __init__(
        self,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        secrets: SecretStore,
        ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._secrets = secrets
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[UUID, _Cached] = {}
        self._lock = threading.Lock()

    def reader_for(self, tenant_id: UUID) -> ProductSource:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(tenant_id)
        if cached is not None and now - cached.checked_at < self._ttl:
            return cached.reader
        sources = self._connections.catalogue_sources(tenant_id)
        if not sources:
            raise CatalogueSourceMissing()
        [source] = sources  # at most one active source per business (database rule, P1-Q7)
        key = (source.connection_id, source.updated_at)
        if cached is not None and cached.key == key:
            reader = cached.reader
        else:
            provider = self._registry.find(source.provider_key)
            if provider is None:
                raise CatalogueSourceMissing(
                    f"The product source provider '{source.provider_key}' is not installed"
                )
            try:
                reader = product_source_of(provider).catalogue_reader(source, self._secrets)
            except OperationUnsupported as error:
                raise CatalogueSourceMissing(str(error)) from None
        with self._lock:
            self._cache[tenant_id] = _Cached(key, reader, now)
        return reader


class TenantCatalogue:
    """An ProductSource that routes every call to the calling business's own product source."""

    def __init__(self, resolver: CatalogueSourceResolver) -> None:
        self._resolver = resolver

    def search_products(
        self, context: TenantContext, query: str, limit: int = 10
    ) -> Sequence[Product]:
        return self._reader(context).search_products(context, query, limit)

    def list_categories(self, context: TenantContext) -> Sequence[str]:
        return self._reader(context).list_categories(context)

    def get_products(self, context: TenantContext, skus: Sequence[str]) -> Mapping[str, Product]:
        return self._reader(context).get_products(context, skus)

    def get_prices(
        self,
        context: TenantContext,
        skus: Sequence[str],
        currency: str,
        effective_at: datetime,
    ) -> Mapping[str, PriceQuote]:
        return self._reader(context).get_prices(context, skus, currency, effective_at)

    def get_availability(
        self, context: TenantContext, skus: Sequence[str], warehouse: str | None = None
    ) -> Mapping[str, StockLevel]:
        return self._reader(context).get_availability(context, skus, warehouse)

    def _reader(self, context: TenantContext) -> ProductSource:
        return self._resolver.reader_for(context.tenant_id)
