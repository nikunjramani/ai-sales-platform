"""Behaviour specific to the simulated ERP (the shared contract is in tests/unit/ports)."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from sales_platform.adapters.erp.simulated import SimulatedErp
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.ports.erp import ErpError, ErpErrorCode

type ContextFactory = Callable[[UUID, Role], TenantContext]


def test_unconfigured_tenant_is_unsupported(
    simulated_erp: SimulatedErp, make_context: ContextFactory
) -> None:
    with pytest.raises(ErpError) as raised:
        simulated_erp.get_products(make_context(uuid4(), Role.VIEWER), ["IND-FAN-450"])
    assert raised.value.code is ErpErrorCode.UNSUPPORTED
    assert not raised.value.retryable


def test_injected_failure_is_raised(
    simulated_erp: SimulatedErp, make_context: ContextFactory, industrial_tenant: UUID
) -> None:
    simulated_erp.failures["get_availability"] = ErpError(ErpErrorCode.RATE_LIMITED, "slow")
    with pytest.raises(ErpError) as raised:
        simulated_erp.get_availability(
            make_context(industrial_tenant, Role.VIEWER), ["IND-FAN-450"]
        )
    assert raised.value.retryable


def test_stock_none_means_unknown_quantity(
    simulated_erp: SimulatedErp,
    make_context: ContextFactory,
    industrial_tenant: UUID,
    now: datetime,
) -> None:
    levels = simulated_erp.get_availability(
        make_context(industrial_tenant, Role.VIEWER), ["IND-CABLE-2C", "IND-FAN-450"]
    )
    assert levels["IND-CABLE-2C"].available_quantity is None
    assert levels["IND-FAN-450"].available_quantity == Decimal(120)
    assert levels["IND-FAN-450"].fetched_at == now


def test_other_warehouse_returns_nothing(
    simulated_erp: SimulatedErp, make_context: ContextFactory, industrial_tenant: UUID
) -> None:
    context = make_context(industrial_tenant, Role.VIEWER)
    assert simulated_erp.get_availability(context, ["IND-FAN-450"], warehouse="ELSEWHERE") == {}


def test_categories_are_the_tenants_own_product_groups(
    simulated_erp: SimulatedErp,
    make_context: ContextFactory,
    industrial_tenant: UUID,
    office_tenant: UUID,
) -> None:
    industrial = make_context(industrial_tenant, Role.VIEWER)
    assert simulated_erp.list_categories(industrial) == [
        "Cables", "Exhaust fans", "Fasteners", "Pumps",
    ]  # fmt: skip
    office = make_context(office_tenant, Role.VIEWER)
    assert simulated_erp.list_categories(office) == ["Desks", "Office chairs"]
