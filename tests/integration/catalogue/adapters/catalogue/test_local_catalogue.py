"""LocalCatalogue on PostgreSQL: the ERP contract checks and search cases, for both roles,
and each business routed to its own product source through its catalogue connection."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine

from sales_platform.catalogue.adapters.catalogue.local import LocalCatalogue
from sales_platform.catalogue.ports.erp import ErpError, ErpErrorCode, ProductSource
from sales_platform.composition import build_catalogue, build_catalogue_store, build_runtime
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.seed import INDUSTRIAL_TENANT, LOCAL_TENANT

pytestmark = pytest.mark.integration

CASES = json.loads(
    (Path(__file__).parents[4] / "data" / "catalogue_search_cases.json").read_text("utf-8")
)["cases"]
LOCAL = TenantContext(LOCAL_TENANT, "sales@local.example", frozenset({Role.SALES_OPERATOR}))
OTHER = TenantContext(INDUSTRIAL_TENANT, "sales@industrial.example", frozenset({Role.VIEWER}))


@pytest.fixture(params=["runtime", "worker"])
def catalogue(
    request: pytest.FixtureRequest, runtime_engine: Engine, worker_engine: Engine
) -> ProductSource:
    engine = runtime_engine if request.param == "runtime" else worker_engine
    return LocalCatalogue(build_catalogue_store(engine))


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case['query']}/{case['limit']}")
def test_search_quality_cases(catalogue: ProductSource, case: dict[str, object]) -> None:
    found = catalogue.search_products(LOCAL, str(case["query"]), int(str(case["limit"])))
    assert [product.sku for product in found] == case["expected"]


def test_contract_checks(catalogue: ProductSource) -> None:
    now = datetime.now(UTC)
    assert set(catalogue.get_products(LOCAL, ["IND-FAN-450"])) == {"IND-FAN-450"}
    price = catalogue.get_prices(LOCAL, ["IND-FAN-450"], "INR", now)["IND-FAN-450"]
    assert price.is_effective_at(now) and price.fetched_at.tzinfo is not None
    assert str(price.unit_price.amount) == "4250.00"  # decimals kept exactly
    level = catalogue.get_availability(LOCAL, ["IND-FAN-450"])["IND-FAN-450"]
    assert level.available_quantity == 120 and level.fetched_at.tzinfo is not None
    for unknown in ("CONTRACT-UNKNOWN-SKU",):
        assert catalogue.get_products(LOCAL, [unknown]) == {}
        assert catalogue.get_prices(LOCAL, [unknown], "INR", now) == {}
        assert catalogue.get_availability(LOCAL, [unknown]) == {}
    assert catalogue.get_products(OTHER, ["IND-FAN-450"]) == {}
    assert catalogue.search_products(OTHER, "IND-FAN-450") == []
    assert catalogue.list_categories(LOCAL) == ["Cables", "Exhaust fans", "Fasteners", "Pumps"]
    with pytest.raises(ValueError, match="timezone"):
        catalogue.get_prices(LOCAL, ["IND-FAN-450"], "INR", datetime(2026, 10, 22))
    with pytest.raises(ErpError) as raised:
        catalogue.search_products(LOCAL, "fan", 51)
    assert raised.value.code is ErpErrorCode.INVALID_REQUEST


def test_each_business_reads_its_own_source(
    integration_settings: Settings, worker_engine: Engine
) -> None:
    catalogue = build_catalogue(worker_engine, build_runtime(integration_settings, worker_engine))
    local = catalogue.get_prices(LOCAL, ["IND-FAN-450"], "INR", datetime.now(UTC))
    simulated = catalogue.get_prices(OTHER, ["IND-FAN-450"], "INR", datetime.now(UTC))
    assert local["IND-FAN-450"].source == "local-catalogue"
    assert local["IND-FAN-450"].price_list_id == "standard"
    assert simulated["IND-FAN-450"].source == "simulated-erp"
