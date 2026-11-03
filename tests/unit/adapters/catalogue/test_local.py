"""LocalCatalogue specifics (the shared ERP contract is in tests/unit/ports)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from quotation_app.adapters.catalogue.local import LocalCatalogue
from quotation_app.adapters.erp.simulated import demo_catalogue
from quotation_app.adapters.providers.local_catalogue import LocalCatalogueProvider, build
from quotation_app.domain.catalogue_admin import ProductFields
from quotation_app.domain.connections import Connection, ConnectionStatus, Purpose
from quotation_app.domain.tenancy import Role, TenantContext
from quotation_app.ports.erp import ErpError, ErpErrorCode
from quotation_app.ports.providers import OperationUnsupported, ProviderEnvironment
from tests.support.catalogue_store import InMemoryCatalogueStore

TENANT = UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
CONTEXT = TenantContext(TENANT, "sales@example.test", frozenset({Role.SALES_OPERATOR}))


def _store() -> InMemoryCatalogueStore:
    return InMemoryCatalogueStore.from_simulated(
        {TENANT: demo_catalogue("industrial")}, lambda: NOW
    )


def _fan(store: InMemoryCatalogueStore) -> UUID:
    return store.active_products(TENANT, ["IND-FAN-450"])[0].product_id


def test_price_evidence_names_the_standard_list_revision_and_read_time() -> None:
    store = _store()
    read_at = NOW + timedelta(minutes=3)
    catalogue = LocalCatalogue(store, clock=lambda: read_at)
    price = catalogue.get_prices(CONTEXT, ["IND-FAN-450"], "INR", NOW)["IND-FAN-450"]
    assert (str(price.unit_price.amount), price.price_list_id, price.price_list_version) == (
        "4250.00",
        "standard",
        "2",
    )
    assert price.fetched_at == read_at and price.source == "local-catalogue"
    assert price.effective_to is None
    assert catalogue.get_prices(CONTEXT, ["IND-FAN-450"], "EUR", NOW) == {}


def test_a_scheduled_price_ends_the_current_one() -> None:
    store = _store()
    start = NOW + timedelta(days=2)
    store.add_price(
        CONTEXT, _fan(store), uuid4(), Decimal("4400.00"), "INR", start, None, "standard"
    )
    catalogue = LocalCatalogue(store, clock=lambda: NOW)
    current = catalogue.get_prices(CONTEXT, ["IND-FAN-450"], "INR", NOW)["IND-FAN-450"]
    assert current.effective_to == start and current.unit_price.amount == Decimal("4250.00")
    later = catalogue.get_prices(CONTEXT, ["IND-FAN-450"], "INR", start)["IND-FAN-450"]
    assert later.unit_price.amount == Decimal("4400.00") and later.price_list_version == "3"


def test_stock_evidence_is_the_entered_as_of_time_and_not_tracked_is_unknown() -> None:
    store = _store()
    as_of = NOW + timedelta(minutes=1)  # newer than the seeded count
    store.add_stock(CONTEXT, _fan(store), uuid4(), Decimal("7"), as_of, None, "MAIN")
    levels = LocalCatalogue(store).get_availability(CONTEXT, ["IND-FAN-450", "IND-CABLE-2C"])
    fan = levels["IND-FAN-450"]
    assert (fan.available_quantity, fan.fetched_at) == (Decimal(7), as_of)
    assert levels["IND-CABLE-2C"].available_quantity is None
    assert LocalCatalogue(store).get_availability(CONTEXT, ["IND-FAN-450"], "BERLIN") == {}
    assert set(LocalCatalogue(store).get_availability(CONTEXT, ["IND-FAN-450"], "MAIN")) == {
        "IND-FAN-450"
    }


def test_archived_products_and_categories_disappear() -> None:
    store = _store()
    catalogue = LocalCatalogue(store)
    store.set_archived(CONTEXT, _fan(store), True, 1, 10_000)
    assert catalogue.get_products(CONTEXT, ["IND-FAN-450"]) == {}
    assert catalogue.get_prices(CONTEXT, ["IND-FAN-450"], "INR", NOW) == {}
    assert [p.sku for p in catalogue.search_products(CONTEXT, "IND-FAN")] == ["IND-FAN-600"]
    fasteners = next(c for c in store.list_categories(TENANT) if c.name == "Fasteners")
    store.update_category(CONTEXT, fasteners.category_id, None, True, fasteners.row_version)
    assert catalogue.get_products(CONTEXT, ["IND-BOLT-M8"])["IND-BOLT-M8"].category is None
    assert "Fasteners" not in catalogue.list_categories(CONTEXT)
    assert catalogue.search_products(CONTEXT, "fasteners") == []  # only its category said so


def test_customers_see_the_name_not_the_staff_description() -> None:
    store = _store()
    product = store.create_product(
        CONTEXT, "X-1", ProductFields("Blue widget", "each", description="Margin 40%"), 10_000
    )
    found = LocalCatalogue(store).get_products(CONTEXT, [product.sku])["X-1"]
    assert found.description == "Blue widget"
    assert LocalCatalogue(store).search_products(CONTEXT, "margin") == []


def test_store_failure_is_a_retryable_erp_error() -> None:
    store = _store()
    store.unavailable = True
    with pytest.raises(ErpError) as raised:
        LocalCatalogue(store).get_products(CONTEXT, ["IND-FAN-450"])
    assert raised.value.code is ErpErrorCode.TEMPORARY_UNAVAILABLE and raised.value.retryable


def test_words_without_search_terms_find_nothing() -> None:
    assert LocalCatalogue(_store()).search_products(CONTEXT, "a !") == []


def _connection(purposes: frozenset[Purpose]) -> Connection:
    return Connection(
        uuid4(), TENANT, "local_catalogue", "Our product list", purposes, {}, {}, None,
        ConnectionStatus.ACTIVE, None, "test", NOW, NOW,
    )  # fmt: skip


def test_provider_reads_only_with_a_store_and_a_catalogue_purpose() -> None:
    provider = build(ProviderEnvironment("test", Path("unused"), catalogue_store=_store()))
    catalogue = _connection(frozenset({Purpose.CATALOGUE}))
    assert provider.capabilities.catalogue_editable
    assert provider.test(catalogue, None).ok  # type: ignore[arg-type]
    assert provider.catalogue_reader(catalogue, None).list_categories(CONTEXT)  # type: ignore[arg-type]
    with pytest.raises(OperationUnsupported):
        provider.catalogue_reader(_connection(frozenset({Purpose.CUSTOMER_MESSAGES})), None)  # type: ignore[arg-type]
    without = LocalCatalogueProvider(None)
    assert not without.test(catalogue, None).ok  # type: ignore[arg-type]
    with pytest.raises(OperationUnsupported):
        without.catalogue_reader(catalogue, None)  # type: ignore[arg-type]
