"""Shared ERP contract suite: every ERP adapter must pass these tests.

To add an adapter, register a factory in ADAPTERS. Each factory returns the adapter, two
tenant contexts and a SKU that exists only for the first tenant. A real adapter that needs a
sandbox registers with `pytest.param(..., marks=pytest.mark.integration)` so it is skipped
unless --run-integration is passed. Our own catalogue on PostgreSQL runs the same search cases
in tests/integration/adapters/catalogue.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from quotation_app.adapters.catalogue.local import LocalCatalogue
from quotation_app.adapters.erp.simulated import SimulatedErp, demo_catalogues
from quotation_app.adapters.providers.simulated import SimulatedProvider
from quotation_app.application.catalogue_sources import CatalogueSourceResolver, TenantCatalogue
from quotation_app.application.providers import ProviderRegistry
from quotation_app.domain.connections import Connection, ConnectionStatus, Purpose
from quotation_app.domain.tenancy import Role, TenantContext
from quotation_app.persistence.seed import INDUSTRIAL_TENANT, OFFICE_TENANT
from quotation_app.ports.erp import ErpError, ErpErrorCode, ErpReader
from tests.support.catalogue_store import InMemoryCatalogueStore

SEARCH_CASES = json.loads(
    (Path(__file__).parents[2] / "data" / "catalogue_search_cases.json").read_text("utf-8")
)["cases"]


@dataclass(frozen=True)
class ContractSetup:
    erp: ErpReader
    tenant: TenantContext
    other_tenant: TenantContext
    known_sku: str
    currency: str


def _context(tenant_id: UUID) -> TenantContext:
    return TenantContext(tenant_id, "contract@example.test", frozenset({Role.SALES_OPERATOR}))


def _simulated() -> ContractSetup:
    return ContractSetup(
        erp=SimulatedErp(demo_catalogues(INDUSTRIAL_TENANT, OFFICE_TENANT)),
        tenant=_context(INDUSTRIAL_TENANT),
        other_tenant=_context(OFFICE_TENANT),
        known_sku="IND-FAN-450",
        currency="INR",
    )


def _tenant_catalogue() -> ContractSetup:
    """Phase 1: the per-business router over the simulated provider's demo catalogues."""
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def source(tenant_id: UUID, name: str) -> Connection:
        return Connection(
            uuid4(), tenant_id, "simulated", "Demo", frozenset({Purpose.CATALOGUE}),
            {"catalogue": name}, {}, "simulated-local", ConnectionStatus.ACTIVE, None, "test",
            now, now,
        )  # fmt: skip

    class Sources:
        def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
            name = {INDUSTRIAL_TENANT: "industrial", OFFICE_TENANT: "office"}[tenant_id]
            return [source(tenant_id, name)]

    resolver = CatalogueSourceResolver(
        Sources(),  # type: ignore[arg-type]
        ProviderRegistry([SimulatedProvider(Path("unused"))]),
        None,  # type: ignore[arg-type]
    )
    return replace(_simulated(), erp=TenantCatalogue(resolver))


def _local_in_memory() -> ContractSetup:
    """Phase 3: our own catalogue, holding the same synthetic data."""
    store = InMemoryCatalogueStore.from_simulated(demo_catalogues(INDUSTRIAL_TENANT, OFFICE_TENANT))
    return replace(_simulated(), erp=LocalCatalogue(store))


ADAPTERS: list[object] = [
    pytest.param(_simulated, id="simulated"),
    pytest.param(_tenant_catalogue, id="tenant-catalogue"),
    pytest.param(_local_in_memory, id="local-memory"),
]


@pytest.fixture(params=ADAPTERS)
def setup(request: pytest.FixtureRequest) -> ContractSetup:
    factory: Callable[[], ContractSetup] = request.param
    return factory()


def test_known_sku_returns_evidence(setup: ContractSetup) -> None:
    erp, tenant, sku = setup.erp, setup.tenant, setup.known_sku
    now = datetime.now(UTC)
    assert set(erp.get_products(tenant, [sku])) == {sku}
    price = erp.get_prices(tenant, [sku], setup.currency, now)[sku]
    assert price.unit_price.currency == setup.currency
    assert price.unit_price.amount >= 0
    assert price.is_effective_at(now)
    assert price.source and price.fetched_at.tzinfo is not None
    level = erp.get_availability(tenant, [sku])[sku]
    assert level.source and level.fetched_at.tzinfo is not None


def test_unknown_sku_is_absent_not_zero(setup: ContractSetup) -> None:
    erp, tenant, unknown = setup.erp, setup.tenant, "CONTRACT-UNKNOWN-SKU"
    assert erp.get_products(tenant, [unknown]) == {}
    assert erp.get_prices(tenant, [unknown], setup.currency, datetime.now(UTC)) == {}
    assert erp.get_availability(tenant, [unknown]) == {}


def test_other_tenant_cannot_see_records(setup: ContractSetup) -> None:
    erp, other, sku = setup.erp, setup.other_tenant, setup.known_sku
    assert erp.get_products(other, [sku]) == {}
    assert erp.get_prices(other, [sku], setup.currency, datetime.now(UTC)) == {}
    assert erp.get_availability(other, [sku]) == {}
    assert all(product.sku != sku for product in erp.search_products(other, sku))


def test_search_is_tenant_scoped(setup: ContractSetup) -> None:
    results = setup.erp.search_products(setup.tenant, setup.known_sku)
    assert setup.known_sku in {product.sku for product in results}


def test_naive_effective_date_is_rejected(setup: ContractSetup) -> None:
    with pytest.raises(ValueError, match="timezone"):
        setup.erp.get_prices(
            setup.tenant, [setup.known_sku], setup.currency, datetime(2026, 10, 22)
        )


@pytest.mark.parametrize(("query", "limit"), [("", 10), ("  ", 10), ("fan", 0), ("fan", 51)])
def test_invalid_search_is_rejected(setup: ContractSetup, query: str, limit: int) -> None:
    with pytest.raises(ErpError) as raised:
        setup.erp.search_products(setup.tenant, query, limit)
    assert raised.value.code is ErpErrorCode.INVALID_REQUEST


def test_categories_are_sorted_and_the_businesss_own(setup: ContractSetup) -> None:
    mine = list(setup.erp.list_categories(setup.tenant))
    theirs = list(setup.erp.list_categories(setup.other_tenant))
    assert mine == sorted(mine) and mine
    assert not set(mine) & set(theirs)


@pytest.mark.parametrize("case", SEARCH_CASES, ids=lambda case: f"{case['query']}/{case['limit']}")
def test_search_quality_cases(setup: ContractSetup, case: dict[str, object]) -> None:
    found = setup.erp.search_products(setup.tenant, str(case["query"]), int(str(case["limit"])))
    assert [product.sku for product in found] == case["expected"]
