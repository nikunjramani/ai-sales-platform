"""ERP read and write contracts. Every ERP adapter, simulated or real, must pass the shared
contract suites (tests/unit/ports/test_erp_contract.py, test_erp_writer_contract.py).

Rules for implementations:
- Scope every call to context.tenant_id; never return another tenant's records.
- Omit unknown SKUs from results. Never return placeholder zero prices or stock.
- Stamp results with source and a timezone-aware fetched_at.
- Raise ErpError with a normalized code on failure; never fabricate a successful result.
- Writes (OrderWriter, phase 7): raise UNKNOWN_OUTCOME when the request may have reached the ERP
  (a timeout after sending); never claim success or failure you do not know. A second create
  with the same reference must never make a second order: answer CONFLICT or the same order.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from sales_platform.catalogue.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.platform.domain.jobs import ServiceError
from sales_platform.platform.domain.tenancy import TenantContext


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


@dataclass(frozen=True)
class OrderLineRequest:
    sku: str
    quantity: Decimal
    unit: str
    unit_price: Decimal  # the accepted price; the ERP must not re-price it silently


@dataclass(frozen=True)
class SalesOrderRequest:
    """What we ask the ERP to record. `customer` is the ERP's own customer account; None
    means the one the connection was set up with."""

    reference: str  # our stable external reference, e.g. QO-<order id>
    order_number: str  # our human number, e.g. O-1A2B3C4D (shown on the ERP's lines)
    customer: str | None
    currency: str
    lines: tuple[OrderLineRequest, ...]
    ordered_on: date


class ErpOrderState(StrEnum):
    """The ERP's view of an order, in our words (each provider maps its own statuses)."""

    DRAFT = "draft"  # recorded, not yet confirmed by the business in its ERP
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"  # partly delivered
    DELIVERED = "delivered"
    ON_HOLD = "on_hold"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ErpOrder:
    erp_order_id: str
    reference: str
    state: ErpOrderState
    fetched_at: datetime


class OrderWriter(Protocol):
    def create_sales_order(self, context: TenantContext, order: SalesOrderRequest) -> ErpOrder:
        """Raises ErpError: CONFLICT when the reference is already used, UNKNOWN_OUTCOME when
        the order may exist, other codes when it certainly was not created."""
        ...

    def get_order_by_external_reference(
        self, context: TenantContext, reference: str
    ) -> ErpOrder | None: ...

    def get_order_status(self, context: TenantContext, erp_order_id: str) -> ErpOrder:
        """NOT_FOUND when the ERP no longer has the order."""
        ...
