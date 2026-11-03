"""Copying a business's ERP or shop catalogue into our catalogue tables (phase 6, ADR 0023).

A product source with CatalogueCapabilities.sync hands over a CatalogueFeed: its whole current
catalogue (products, the price in force per product and the stock count), already mapped to our
words. The core stores it as a copy: the ERP stays the authority (ADR 0003, 0020), so synced
products are read-only in the portal and a product the ERP no longer offers is archived.

Mapping rules (user answers and ADR 0023):
- units are renamed exactly, never converted: the connection's own unit map first, then the
  unit words the enquiry reader already knows ("Nos" -> each). Anything else is not copied and
  is reported as an issue for staff;
- prices only in the business currency and only when they exclude tax; otherwise none;
- stock: the sum of the chosen warehouses, as "available" (on hand minus reserved) or "on
  hand", as the business chose. A negative ERP balance means nothing is available (0).
A full feed is sent every run (no "changed since"), so deletions in the ERP are never missed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sales_platform.catalogue.domain.catalogue_admin import (
    CATALOGUE_UNITS,
    DEFAULT_LOCATION,
    MAX_ACTIVE_PRODUCTS,
    MAX_CATEGORY,
    MAX_NAME,
    QUANTITY_DECIMALS,
    InvalidCatalogueEntry,
    ProductFields,
    check_product_fields,
    check_sku,
)
from sales_platform.catalogue.domain.units import normalise_unit
from sales_platform.platform.domain.validation import require_aware

MAX_ISSUES = 200  # issues kept per run; the count says how many there were
MIN_SYNC_MINUTES = 5
MAX_SYNC_MINUTES = 1440
DEFAULT_SYNC_MINUTES = 15
# The connection config key a synced source keeps its interval under (set by its provider).
SYNC_MINUTES_KEY = "sync_minutes"


class SyncStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncTrigger(StrEnum):
    SCHEDULE = "schedule"  # the repeating run; it queues the next one
    MANUAL = "manual"  # "Sync now" in the portal; it does not repeat
    CONNECTED = "connected"  # the first run after the source was enabled


@dataclass(frozen=True)
class FeedProduct:
    """One product as the source offers it, already in our words (checked on creation)."""

    sku: str
    name: str
    unit: str
    external_id: str
    category: str | None = None
    description: str | None = None
    minimum_quantity: Decimal = Decimal(1)
    quantity_step: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        check_sku(self.sku)
        checked = check_product_fields(
            ProductFields(
                name=self.name[:MAX_NAME],
                unit=self.unit,
                description=self.description,
                minimum_quantity=self.minimum_quantity,
                quantity_step=self.quantity_step,
            )
        )
        object.__setattr__(self, "name", checked.name)
        object.__setattr__(self, "description", checked.description)
        if self.category is not None:
            name = " ".join(self.category.split())[:MAX_CATEGORY]
            object.__setattr__(self, "category", name or None)
        if not self.external_id or len(self.external_id) > 200:
            raise InvalidCatalogueEntry("external_id", "The source's ID is 1-200 characters")


@dataclass(frozen=True)
class FeedPrice:
    sku: str
    unit_price: Decimal
    currency: str
    unit: str
    effective_from: datetime

    def __post_init__(self) -> None:
        require_aware(self.effective_from, "effective_from")
        if not self.unit_price.is_finite() or self.unit_price <= 0:
            raise InvalidCatalogueEntry("unit_price", "A synced price must be greater than 0")


@dataclass(frozen=True)
class FeedStock:
    sku: str
    quantity: Decimal | None  # None: the source does not count this product (not tracked)
    unit: str
    as_of: datetime

    def __post_init__(self) -> None:
        require_aware(self.as_of, "as_of")
        if self.quantity is not None and (not self.quantity.is_finite() or self.quantity < 0):
            raise InvalidCatalogueEntry("quantity", "A synced stock count cannot be negative")


@dataclass(frozen=True)
class FeedIssue:
    """Something the source offers that was not copied, in words staff can act on."""

    code: str  # unmapped_unit | invalid_product | currency_mismatch | tax_included | ...
    message: str
    sku: str | None = None


@dataclass(frozen=True)
class CatalogueFeed:
    products: tuple[FeedProduct, ...]
    prices: tuple[FeedPrice, ...] = ()
    stock: tuple[FeedStock, ...] = ()
    issues: tuple[FeedIssue, ...] = ()
    location: str = DEFAULT_LOCATION  # where the copied stock counts are stored

    def __post_init__(self) -> None:
        if len(self.products) > MAX_ACTIVE_PRODUCTS:
            raise ValueError(
                f"The source offers {len(self.products)} products; at most "
                f"{MAX_ACTIVE_PRODUCTS:,} can be copied"
            )
        skus = [product.sku.casefold() for product in self.products]
        if len(set(skus)) != len(skus):
            raise ValueError("The source offers the same SKU twice")


@dataclass(frozen=True)
class SyncCounts:
    created: int = 0
    updated: int = 0
    archived: int = 0  # no longer offered by the source
    unchanged: int = 0
    prices_added: int = 0
    stock_added: int = 0
    issues: int = 0


@dataclass(frozen=True)
class SyncRun:
    run_id: UUID
    connection_id: UUID
    trigger: SyncTrigger
    status: SyncStatus
    started_at: datetime
    finished_at: datetime | None
    counts: SyncCounts | None
    issues: tuple[FeedIssue, ...] = ()
    error: str | None = None
    next_run_at: datetime | None = None


@dataclass(frozen=True)
class UnitMap:
    """The connection's exact renames from the source's unit codes to ours."""

    renames: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "UnitMap":
        """'Nos=each, Kgs = kg' -> renames; any part that is not code=unit is refused."""
        renames: dict[str, str] = {}
        for part in (item.strip() for item in text.replace("\n", ",").split(",")):
            if not part:
                continue
            code, separator, unit = (piece.strip() for piece in part.partition("="))
            if not separator or not code or unit not in CATALOGUE_UNITS:
                raise ValueError(
                    f"Unit map entries are CODE=unit with unit one of "
                    f"{', '.join(sorted(CATALOGUE_UNITS))}; '{part}' is not"
                )
            renames[code.casefold()] = unit
        return cls(renames)

    def unit_for(self, code: str) -> str | None:
        """Our unit for the source's code, or None (not copied, reported)."""
        return self.renames.get(code.strip().casefold()) or normalise_unit(code)


def summed_stock(balances: Sequence[tuple[Decimal, Decimal]], *, available: bool) -> Decimal:
    """Stock over the chosen warehouses from (on hand, reserved) pairs.

    "available" subtracts what existing orders reserved. A negative result (ERPs may allow
    negative stock) means nothing is available, so it is 0, never a negative count.
    """
    total = sum(
        ((on_hand - reserved) if available else on_hand for on_hand, reserved in balances),
        Decimal(0),
    )
    result = max(total, Decimal(0)).quantize(Decimal(1).scaleb(-QUANTITY_DECIMALS))
    # 12.000 -> 12 (never 1.2E+1): whole numbers are stored without decimals.
    return result.quantize(Decimal(1)) if result == result.to_integral_value() else result


def quantity_step(whole_numbers: bool) -> Decimal:
    """Units counted in whole numbers step by 1; others allow our smallest step."""
    return Decimal(1) if whole_numbers else Decimal(1).scaleb(-QUANTITY_DECIMALS)


def sync_minutes(config: Mapping[str, object]) -> int:
    """A connection's sync interval; missing or out of range falls back to the default."""
    value = config.get(SYNC_MINUTES_KEY)
    if isinstance(value, int) and not isinstance(value, bool):
        if MIN_SYNC_MINUTES <= value <= MAX_SYNC_MINUTES:
            return value
    return DEFAULT_SYNC_MINUTES


def keep_issues(issues: Sequence[FeedIssue]) -> tuple[FeedIssue, ...]:
    return tuple(issues[:MAX_ISSUES])
