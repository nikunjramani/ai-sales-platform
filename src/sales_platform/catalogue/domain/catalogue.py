"""Facts read from the business's ERP. The ERP is authoritative; these are timestamped copies."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sales_platform.platform.domain.money import Money
from sales_platform.platform.domain.validation import require_aware, require_decimal, require_text


@dataclass(frozen=True)
class Product:
    sku: str
    description: str
    unit: str
    minimum_quantity: Decimal
    quantity_step: Decimal
    # The ERP's product group (e.g. "Pumps"); used to tell customers what the business sells.
    category: str | None = None

    def __post_init__(self) -> None:
        require_text(self.sku, "sku", 64)
        if self.category is not None:
            require_text(self.category, "category", 100)
        require_text(self.description, "description", 500)
        require_text(self.unit, "unit", 32)
        require_decimal(self.minimum_quantity, "minimum_quantity")
        require_decimal(self.quantity_step, "quantity_step")
        if self.minimum_quantity <= 0 or self.quantity_step <= 0:
            raise ValueError("Minimum quantity and quantity step must be positive")


@dataclass(frozen=True)
class PriceQuote:
    """One price-list entry, with enough evidence to explain where the price came from."""

    sku: str
    unit_price: Money
    unit: str
    price_list_id: str
    price_list_version: str
    effective_from: datetime
    effective_to: datetime | None
    fetched_at: datetime
    source: str

    def __post_init__(self) -> None:
        for name in ("sku", "unit", "price_list_id", "price_list_version", "source"):
            require_text(getattr(self, name), name)
        require_aware(self.effective_from, "effective_from")
        require_aware(self.fetched_at, "fetched_at")
        if self.unit_price.amount < 0:
            raise ValueError("Unit price cannot be negative")
        if self.effective_to is not None:
            require_aware(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValueError("effective_to must be after effective_from")

    def is_effective_at(self, moment: datetime) -> bool:
        return self.effective_from <= moment and (
            self.effective_to is None or moment < self.effective_to
        )


@dataclass(frozen=True)
class StockLevel:
    """available_quantity is None when the ERP cannot state a number; that means unknown."""

    sku: str
    unit: str
    available_quantity: Decimal | None
    warehouse: str | None
    fetched_at: datetime
    source: str

    def __post_init__(self) -> None:
        require_text(self.sku, "sku", 64)
        require_text(self.unit, "unit", 32)
        require_text(self.source, "source")
        require_aware(self.fetched_at, "fetched_at")
        if self.available_quantity is not None:
            require_decimal(self.available_quantity, "available_quantity")
            if self.available_quantity < 0:
                raise ValueError("Available quantity cannot be negative")
