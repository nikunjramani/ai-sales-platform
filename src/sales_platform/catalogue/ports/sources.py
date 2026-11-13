"""The product source contract. Every product source (our own list or a provider package's
`catalogue_reader`) must pass the shared suite: tests/unit/catalogue/ports/test_erp_contract.py.

Rules for implementations:
- Scope every call to context.tenant_id; never return another tenant's records.
- Omit unknown SKUs from results. Never return placeholder zero prices or stock.
- Stamp results with source and a timezone-aware fetched_at.
- Raise ErpError with a normalized code on failure; never fabricate a successful result.

Callers reach a provider's source through product_source_of, which raises
OperationUnsupported for a tool that is not one.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from sales_platform.catalogue.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.ports.providers import OperationUnsupported
from sales_platform.platform.domain.jobs import ServiceError
from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.platform.ports.secrets import SecretStore


class ErpErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    TEMPORARY_UNAVAILABLE = "temporary_unavailable"
    INVALID_REQUEST = "invalid_request"
    CONFLICT = "conflict"
    UNKNOWN_OUTCOME = "unknown_outcome"


_RETRYABLE = frozenset({ErpErrorCode.RATE_LIMITED, ErpErrorCode.TEMPORARY_UNAVAILABLE})


class ErpError(ServiceError):
    code: ErpErrorCode

    def __init__(self, code: ErpErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code

    @property
    def retryable(self) -> bool:
        """Safe to retry later for reads. Writes with unknown_outcome need reconciliation."""
        return self.code in _RETRYABLE


class CatalogueSourceMissing(ErpError):
    """The business has no active product source (or its provider is not installed).

    Retrying cannot help, so jobs stop at once; the enquiry goes to review (P1-Q3, P1-Q4).
    """

    def __init__(self, message: str = "No product source is connected for this business") -> None:
        super().__init__(ErpErrorCode.UNSUPPORTED, message)


class ProductSource(Protocol):
    def search_products(
        self, context: TenantContext, query: str, limit: int = 10
    ) -> Sequence[Product]: ...

    def list_categories(self, context: TenantContext) -> Sequence[str]:
        """The business's product groups, sorted; empty if the ERP has none."""
        ...

    def get_products(self, context: TenantContext, skus: Sequence[str]) -> Mapping[str, Product]:
        """Return known products keyed by SKU; unknown SKUs are absent."""
        ...

    def get_prices(
        self,
        context: TenantContext,
        skus: Sequence[str],
        currency: str,
        effective_at: datetime,
    ) -> Mapping[str, PriceQuote]:
        """Return the price effective at effective_at in currency; absent if none exists."""
        ...

    def get_availability(
        self, context: TenantContext, skus: Sequence[str], warehouse: str | None = None
    ) -> Mapping[str, StockLevel]: ...


# --- writes (phase 7, ADR 0024) ------------------------------------------------------------


@runtime_checkable
class ProductSourceProvider(Protocol):
    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ProductSource:
        """The product source behind a connection with Purpose.CATALOGUE."""
        ...


def product_source_of(provider: object) -> ProductSourceProvider:
    if not isinstance(provider, ProductSourceProvider):
        raise OperationUnsupported("This tool is not a product source")
    return provider
