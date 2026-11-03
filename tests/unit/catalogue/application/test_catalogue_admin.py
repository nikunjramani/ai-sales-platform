from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sales_platform.accounts.ports.accounts import NotAllowed, PricingSettings, StoredPricing
from sales_platform.catalogue.adapters.erp.simulated import demo_catalogue
from sales_platform.catalogue.application.catalogue_admin import (
    CurrencyNotSet,
    ProductService,
    SourceNotEditable,
)
from sales_platform.catalogue.domain.catalogue_admin import (
    InvalidCatalogueEntry,
    PriceState,
    ProductFields,
)
from sales_platform.catalogue.ports.catalogue_admin import (
    ProductArchived,
    ProductQuery,
    ProductReferenced,
    VersionConflict,
)
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.platform.domain.tenancy import (
    PermissionDenied,
    Role,
    TenantContext,
    TenantStatus,
)
from sales_platform.providers.local_catalogue import LocalCatalogueProvider
from sales_platform.providers.simulated import SimulatedProvider
from tests.support.catalogue_store import InMemoryCatalogueStore

TENANT = UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
WEEK = 7 * 24 * 3600


@dataclass
class Businesses:
    status_value: TenantStatus = TenantStatus.ACTIVE
    pricing_value: PricingSettings = field(
        default_factory=lambda: PricingSettings("INR", 2, "half_up", Decimal("0.18"), 900, WEEK, 7)
    )

    def status(self, tenant_id: UUID) -> TenantStatus:
        return self.status_value

    def pricing(self, context: TenantContext) -> StoredPricing:
        return StoredPricing(self.pricing_value, 1)

    def settings(self, context: TenantContext) -> SimpleNamespace:
        return SimpleNamespace(time_zone="Asia/Kolkata")


@dataclass
class Sources:
    provider_key: str = "local_catalogue"

    def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
        return [
            Connection(
                uuid4(), tenant_id, self.provider_key, "Our product list",
                frozenset({Purpose.CATALOGUE}), {}, {}, None, ConnectionStatus.ACTIVE, None,
                "test", NOW, NOW,
            )
        ]  # fmt: skip


@dataclass
class Harness:
    service: ProductService
    store: InMemoryCatalogueStore
    businesses: Businesses
    sources: Sources


@pytest.fixture
def harness() -> Harness:
    store = InMemoryCatalogueStore.from_simulated(
        {TENANT: demo_catalogue("industrial")}, lambda: NOW
    )
    businesses, sources = Businesses(), Sources()
    registry = ProviderRegistry([LocalCatalogueProvider(store), SimulatedProvider(Path("unused"))])
    service = ProductService(store, store, businesses, sources, registry, lambda: NOW)  # type: ignore[arg-type]
    return Harness(service, store, businesses, sources)


def ctx(*roles: Role) -> TenantContext:
    return TenantContext(TENANT, "person@example.test", frozenset(roles))


SALES = ctx(Role.SALES_OPERATOR)


def _fan(harness: Harness) -> UUID:
    return harness.store.active_products(TENANT, ["IND-FAN-450"])[0].product_id


@pytest.mark.parametrize(
    "role", [Role.ADMINISTRATOR, Role.SALES_OPERATOR, Role.APPROVER, Role.VIEWER]
)
def test_all_staff_read(harness: Harness, role: Role) -> None:
    summary = harness.service.summary(ctx(role))
    assert summary.source is not None and summary.source.editable
    assert summary.counts.active_products == 5 and summary.units == ("box", "each", "kg", "metre")
    assert len(harness.service.list_products(ctx(role), ProductQuery()).items) == 5


@pytest.mark.parametrize("role", [Role.APPROVER, Role.VIEWER, Role.INTAKE_SERVICE])
def test_only_administrators_and_sales_operators_edit(harness: Harness, role: Role) -> None:
    with pytest.raises(PermissionDenied):
        harness.service.create_product(ctx(role), "X-1", ProductFields("X", "each"))
    if role is Role.INTAKE_SERVICE:
        with pytest.raises(PermissionDenied):
            harness.service.summary(ctx(role))


def test_another_source_is_read_only(harness: Harness) -> None:
    harness.sources.provider_key = "simulated"
    summary = harness.service.summary(SALES)
    assert summary.source is not None and not summary.source.editable
    with pytest.raises(SourceNotEditable, match="Our product list"):
        harness.service.create_product(SALES, "X-1", ProductFields("X", "each"))


def test_suspended_business_is_read_only(harness: Harness) -> None:
    harness.businesses.status_value = TenantStatus.SUSPENDED
    with pytest.raises(NotAllowed):
        harness.service.add_price(SALES, _fan(harness), Decimal(1), None, None, uuid4())


def test_create_validates_and_update_changes_only_given_fields(harness: Harness) -> None:
    with pytest.raises(InvalidCatalogueEntry) as error:
        harness.service.create_product(SALES, "bad sku", ProductFields("X", "each"))
    assert error.value.field == "sku"
    created = harness.service.create_product(SALES, "X-1", ProductFields(" Widget ", "each"))
    assert created.product.fields.name == "Widget" and created.current_price is None
    changed = harness.service.update_product(
        SALES, created.product.product_id, {"minimum_quantity": Decimal(5)}, 1
    )
    assert changed.product.fields.name == "Widget" and changed.product.fields.minimum_quantity == 5
    with pytest.raises(InvalidCatalogueEntry) as error:
        harness.service.update_product(SALES, created.product.product_id, {"sku": "Y"}, 2)
    assert error.value.field == "sku"
    with pytest.raises(VersionConflict):
        harness.service.update_product(SALES, created.product.product_id, {"name": "Z"}, 1)


def test_prices_use_the_business_currency_and_minor_units(harness: Harness) -> None:
    fan = _fan(harness)
    added = harness.service.add_price(SALES, fan, Decimal(4400), None, " new list ", uuid4())
    assert (str(added.entry.unit_price), added.entry.currency, added.entry.revision) == (
        "4400.00",
        "INR",
        3,
    )
    assert added.entry.note == "new list"
    detail = harness.service.get_product(SALES, fan)
    assert detail.current_price is not None and detail.current_price.entry.revision == 3
    history = harness.service.price_history(SALES, fan)
    assert [item.state for item in history] == [
        PriceState.CURRENT,
        PriceState.PAST,
        PriceState.PAST,
    ]
    harness.businesses.pricing_value = PricingSettings(None, None, None, None, None, None, 7)
    with pytest.raises(CurrencyNotSet):
        harness.service.add_price(SALES, fan, Decimal(1), None, None, uuid4())


def test_scheduled_price_and_replays(harness: Harness) -> None:
    fan, command = _fan(harness), uuid4()
    start = NOW + timedelta(days=3)
    first = harness.service.add_price(SALES, fan, Decimal("4500.00"), start, None, command)
    again = harness.service.add_price(SALES, fan, Decimal("4500.00"), start, None, command)
    assert (first.replayed, again.replayed, again.entry.price_id) == (
        False,
        True,
        first.entry.price_id,
    )
    detail = harness.service.get_product(SALES, fan)
    assert detail.scheduled_price is not None and detail.scheduled_price.effective_from == start
    assert detail.current_price is not None and detail.current_price.effective_to == start
    with pytest.raises(InvalidCatalogueEntry):
        harness.service.add_price(SALES, fan, Decimal(1), NOW - timedelta(days=1), None, uuid4())


def test_stock_staleness_uses_the_business_limit(harness: Harness) -> None:
    fan = _fan(harness)
    harness.service.record_stock(SALES, fan, Decimal(80), NOW + timedelta(minutes=1), None, uuid4())
    detail = harness.service.get_product(SALES, fan)
    assert detail.stock is not None and detail.stock.entry.quantity == 80 and not detail.stock.stale
    other = harness.service.create_product(SALES, "X-2", ProductFields("Y", "each")).product
    harness.service.record_stock(
        SALES, other.product_id, Decimal(1), NOW - timedelta(days=8), None, uuid4()
    )
    stale = harness.service.get_product(SALES, other.product_id).stock
    assert stale is not None and stale.stale
    assert harness.service.summary(SALES).counts.stale_stock == 1


def test_unit_locked_once_priced_and_archive_restore_delete(harness: Harness) -> None:
    product = harness.service.create_product(SALES, "X-3", ProductFields("Z", "each")).product
    harness.service.add_price(SALES, product.product_id, Decimal(2), None, None, uuid4())
    with pytest.raises(InvalidCatalogueEntry):
        harness.service.update_product(SALES, product.product_id, {"unit": "box"}, 1)
    with pytest.raises(ProductReferenced):
        harness.service.delete_product(SALES, product.product_id)
    archived = harness.service.archive_product(SALES, product.product_id, 1)
    assert archived.product.archived_at is not None and archived.current_price is None
    with pytest.raises(ProductArchived):
        harness.service.record_stock(SALES, product.product_id, None, None, None, uuid4())
    restored = harness.service.restore_product(SALES, product.product_id, 2)
    assert restored.current_price is not None
    unused = harness.service.create_product(SALES, "X-4", ProductFields("W", "each")).product
    harness.service.delete_product(SALES, unused.product_id)
    actions = [c.action for c in harness.service.changes(SALES, unused.product_id)]
    assert actions == ["deleted", "created"]


def test_list_shows_current_price_and_stock(harness: Harness) -> None:
    page = harness.service.list_products(SALES, ProductQuery(text="fan"))
    fan = next(item for item in page.items if item.product.sku == "IND-FAN-450")
    assert fan.current_price is not None and str(fan.current_price.entry.unit_price) == "4250.00"
    assert fan.stock is not None and fan.stock.entry.quantity == 120
    pump = harness.service.list_products(SALES, ProductQuery(text="pump")).items[0]
    assert pump.current_price is None
    with pytest.raises(ValueError):
        harness.service.list_products(SALES, ProductQuery(limit=101))


def test_categories(harness: Harness) -> None:
    created = harness.service.create_category(SALES, " Valves ")
    assert created.name == "Valves"
    renamed = harness.service.update_category(SALES, created.category_id, "Taps", None, 1)
    assert renamed.name == "Taps" and renamed.row_version == 2
    names = [c.name for c in harness.service.list_categories(SALES)]
    assert "Taps" in names and "Valves" not in names
