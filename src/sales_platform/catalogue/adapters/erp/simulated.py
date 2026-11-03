"""In-memory ERP for development and tests. Data is synthetic and replaceable.

Each tenant has its own catalogue. Tests can inject failures per operation to prove callers
surface ERP errors instead of inventing values.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sales_platform.catalogue.domain.catalogue import PriceQuote, Product, StockLevel
from sales_platform.catalogue.domain.catalogue_search import (
    best_matches,
    product_terms,
    query_terms,
)
from sales_platform.catalogue.ports.erp import ErpError, ErpErrorCode
from sales_platform.platform.domain.money import Money
from sales_platform.platform.domain.tenancy import TenantContext
from sales_platform.platform.domain.validation import require_aware


@dataclass(frozen=True)
class SimulatedPrice:
    unit_price: Decimal
    currency: str
    price_list_id: str
    price_list_version: str
    effective_from: datetime
    effective_to: datetime | None = None


@dataclass
class SimulatedCatalogue:
    products: dict[str, Product]
    prices: dict[str, tuple[SimulatedPrice, ...]] = field(default_factory=dict)
    # A SKU missing here has no stock record; a None value means "ERP cannot say".
    stock: dict[str, Decimal | None] = field(default_factory=dict)
    warehouse: str = "MAIN"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SimulatedErp:
    source = "simulated-erp"

    def __init__(
        self,
        catalogues: Mapping[UUID, SimulatedCatalogue],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._catalogues = dict(catalogues)
        self._clock = clock
        # Operation name -> error to raise, e.g. {"get_prices": ErpError(...)}.
        self.failures: dict[str, ErpError] = {}

    def catalogue(self, tenant_id: UUID) -> SimulatedCatalogue:
        """A tenant's mutable catalogue, so tests and demos can change prices or stock."""
        return self._catalogues[tenant_id]

    def _catalogue(self, context: TenantContext, operation: str) -> SimulatedCatalogue:
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure
        catalogue = self._catalogues.get(context.tenant_id)
        if catalogue is None:
            raise ErpError(ErpErrorCode.UNSUPPORTED, "No ERP connection for this tenant")
        return catalogue

    def search_products(
        self, context: TenantContext, query: str, limit: int = 10
    ) -> Sequence[Product]:
        if not query.strip() or not 1 <= limit <= 50:
            raise ErpError(ErpErrorCode.INVALID_REQUEST, "Query and a limit of 1-50 required")
        catalogue = self._catalogue(context, "search_products")
        wanted = query_terms(query)
        scored = (
            (len(wanted & product_terms(sku, product.description, product.category)), sku, product)
            for sku, product in catalogue.products.items()
        )
        return best_matches(scored, limit)

    def list_categories(self, context: TenantContext) -> Sequence[str]:
        catalogue = self._catalogue(context, "list_categories")
        return sorted({p.category for p in catalogue.products.values() if p.category})

    def get_products(self, context: TenantContext, skus: Sequence[str]) -> Mapping[str, Product]:
        catalogue = self._catalogue(context, "get_products")
        return {sku: catalogue.products[sku] for sku in skus if sku in catalogue.products}

    def get_prices(
        self,
        context: TenantContext,
        skus: Sequence[str],
        currency: str,
        effective_at: datetime,
    ) -> Mapping[str, PriceQuote]:
        require_aware(effective_at, "effective_at")
        catalogue = self._catalogue(context, "get_prices")
        fetched_at = self._clock()
        result: dict[str, PriceQuote] = {}
        for sku in skus:
            product = catalogue.products.get(sku)
            candidates = [
                entry
                for entry in catalogue.prices.get(sku, ())
                if entry.currency == currency
                and entry.effective_from <= effective_at
                and (entry.effective_to is None or effective_at < entry.effective_to)
            ]
            if product is None or not candidates:
                continue
            entry = max(candidates, key=lambda candidate: candidate.effective_from)
            result[sku] = PriceQuote(
                sku=sku,
                unit_price=Money(entry.unit_price, entry.currency),
                unit=product.unit,
                price_list_id=entry.price_list_id,
                price_list_version=entry.price_list_version,
                effective_from=entry.effective_from,
                effective_to=entry.effective_to,
                fetched_at=fetched_at,
                source=self.source,
            )
        return result

    def get_availability(
        self, context: TenantContext, skus: Sequence[str], warehouse: str | None = None
    ) -> Mapping[str, StockLevel]:
        catalogue = self._catalogue(context, "get_availability")
        if warehouse is not None and warehouse != catalogue.warehouse:
            return {}
        fetched_at = self._clock()
        return {
            sku: StockLevel(
                sku=sku,
                unit=catalogue.products[sku].unit,
                available_quantity=catalogue.stock[sku],
                warehouse=catalogue.warehouse,
                fetched_at=fetched_at,
                source=self.source,
            )
            for sku in skus
            if sku in catalogue.products and sku in catalogue.stock
        }


_PRICES_FROM = datetime(2026, 1, 1, tzinfo=UTC)


DEMO_CATALOGUES = ("industrial", "office")


def demo_catalogues(industrial_tenant: UUID, office_tenant: UUID) -> dict[UUID, SimulatedCatalogue]:
    """Two fictional businesses whose items exercise each pricing and stock rule."""
    return {
        industrial_tenant: demo_catalogue("industrial"),
        office_tenant: demo_catalogue("office"),
    }


def demo_catalogue(name: str) -> SimulatedCatalogue:
    """A fresh copy of one named demo catalogue ("industrial" or "office"); KeyError otherwise."""

    def each(sku: str, description: str, category: str) -> Product:
        return Product(sku, description, "each", Decimal(1), Decimal(1), category)

    industrial = SimulatedCatalogue(
        products={
            "IND-FAN-450": each("IND-FAN-450", "Industrial exhaust fan 450 mm", "Exhaust fans"),
            "IND-BOLT-M8": Product(
                "IND-BOLT-M8",
                "M8 hex bolt, zinc plated",
                "each",
                Decimal(100),
                Decimal(50),
                "Fasteners",
            ),
            "IND-CABLE-2C": Product(
                "IND-CABLE-2C", "2-core power cable", "metre", Decimal(1), Decimal("0.5"), "Cables"
            ),
            "IND-PUMP-2HP": each("IND-PUMP-2HP", "2 HP centrifugal pump", "Pumps"),
            # A second fan size makes "exhaust fans" genuinely ambiguous in demos and tests.
            "IND-FAN-600": each("IND-FAN-600", "Industrial exhaust fan 600 mm", "Exhaust fans"),
        },
        prices={
            "IND-FAN-450": (
                SimulatedPrice(
                    Decimal("3990.00"),
                    "INR",
                    "STD-2025",
                    "3",
                    datetime(2025, 1, 1, tzinfo=UTC),
                    _PRICES_FROM,
                ),
                SimulatedPrice(Decimal("4250.00"), "INR", "STD-2026", "1", _PRICES_FROM),
            ),
            "IND-FAN-600": (
                SimulatedPrice(Decimal("6890.00"), "INR", "STD-2026", "1", _PRICES_FROM),
            ),
            "IND-BOLT-M8": (
                SimulatedPrice(Decimal("2.375"), "INR", "STD-2026", "1", _PRICES_FROM),
            ),
            "IND-CABLE-2C": (
                SimulatedPrice(Decimal("38.40"), "INR", "STD-2026", "1", _PRICES_FROM),
            ),
            # IND-PUMP-2HP deliberately has no price: it must never be quoted as zero.
        },
        stock={
            "IND-FAN-450": Decimal(120),
            "IND-BOLT-M8": Decimal(5000),
            "IND-CABLE-2C": None,
            "IND-PUMP-2HP": Decimal(4),
            "IND-FAN-600": Decimal(15),
        },
    )
    office = SimulatedCatalogue(
        products={
            "OFF-CHAIR-ERG": each("OFF-CHAIR-ERG", "Ergonomic office chair", "Office chairs"),
            "OFF-DESK-160": each("OFF-DESK-160", "Desk 160 x 80 cm", "Desks"),
        },
        prices={
            "OFF-CHAIR-ERG": (
                SimulatedPrice(Decimal("189.99"), "EUR", "EU-2026", "2", _PRICES_FROM),
            ),
            "OFF-DESK-160": (
                SimulatedPrice(Decimal("349.50"), "EUR", "EU-2026", "2", _PRICES_FROM),
            ),
        },
        stock={"OFF-CHAIR-ERG": Decimal(0), "OFF-DESK-160": Decimal(3)},
        warehouse="BERLIN",
    )
    return {"industrial": industrial, "office": office}[name]
