"""Use-case tests: permissions, tenant scoping and ERP failure handling."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from sales_platform.catalogue.ports.sources import ErpError, ErpErrorCode
from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.platform.domain.money import Money
from sales_platform.platform.domain.tenancy import PermissionDenied, Role, TenantContext
from sales_platform.quotations.application.pricing import QuotationPricingService
from sales_platform.quotations.domain.pricing import (
    IssueCode,
    PricedQuotation,
    PricingPolicy,
    PricingRejected,
    StockStatus,
)
from sales_platform.quotations.ports.pricing_policy import PricingPolicyNotConfigured
from tests.support.demo_catalogue import DemoCatalogue

type ContextFactory = Callable[[UUID, Role], TenantContext]


@pytest.fixture
def service(
    demo_catalogue: DemoCatalogue, policy_reader: Any, now: datetime
) -> QuotationPricingService:
    return QuotationPricingService(demo_catalogue, policy_reader, clock=lambda: now)


@pytest.fixture
def industrial_sales(make_context: ContextFactory, industrial_tenant: UUID) -> TenantContext:
    return make_context(industrial_tenant, Role.SALES_OPERATOR)


def each(sku: str, quantity: str) -> RequestedLine:
    return RequestedLine(sku, Decimal(quantity), "each")


def test_sales_operator_prices_with_own_policy_and_current_price_list(
    service: QuotationPricingService, industrial_sales: TenantContext
) -> None:
    outcome = service.price(industrial_sales, [each("IND-FAN-450", "2")])
    assert isinstance(outcome, PricedQuotation)
    line = outcome.lines[0]
    # The 2025 price list has expired; the 2026 entry is selected.
    assert line.unit_price == Money(Decimal("4250.00"), "INR")
    assert line.price_evidence.price_list_id == "standard"
    assert outcome.total == Money(Decimal("10030.00"), "INR")


@pytest.mark.parametrize("role", [Role.VIEWER, Role.APPROVER])
def test_permission_is_checked_before_any_erp_call(
    service: QuotationPricingService,
    demo_catalogue: DemoCatalogue,
    make_context: ContextFactory,
    industrial_tenant: UUID,
    role: Role,
) -> None:
    demo_catalogue.failures["get_products"] = ErpError(ErpErrorCode.UNKNOWN_OUTCOME, "not called")
    with pytest.raises(PermissionDenied):
        service.price(make_context(industrial_tenant, role), [each("IND-FAN-450", "1")])


def test_tenant_cannot_price_another_tenants_product(
    service: QuotationPricingService, make_context: ContextFactory, office_tenant: UUID
) -> None:
    office_sales = make_context(office_tenant, Role.SALES_OPERATOR)
    outcome = service.price(office_sales, [each("IND-FAN-450", "1")])
    assert isinstance(outcome, PricingRejected)
    assert [issue.code for issue in outcome.issues] == [IssueCode.PRODUCT_NOT_FOUND]


def test_demo_catalogue_exposes_each_business_case(
    service: QuotationPricingService,
    industrial_sales: TenantContext,
    make_context: ContextFactory,
    office_tenant: UUID,
) -> None:
    missing = service.price(industrial_sales, [each("IND-PUMP-2HP", "1")])
    assert isinstance(missing, PricingRejected)
    assert missing.issues[0].code is IssueCode.PRICE_MISSING

    office_sales = make_context(office_tenant, Role.SALES_OPERATOR)
    office = service.price(office_sales, [each("OFF-CHAIR-ERG", "4"), each("OFF-DESK-160", "4")])
    assert isinstance(office, PricedQuotation)
    assert [line.stock.status for line in office.lines] == [
        StockStatus.UNAVAILABLE,
        StockStatus.INSUFFICIENT,
    ]

    cable = service.price(industrial_sales, [RequestedLine("IND-CABLE-2C", Decimal(3), "metre")])
    assert isinstance(cable, PricedQuotation)
    assert cable.lines[0].stock.status is StockStatus.UNKNOWN


def test_erp_failure_propagates_instead_of_fabricating(
    service: QuotationPricingService, demo_catalogue: DemoCatalogue, industrial_sales: TenantContext
) -> None:
    demo_catalogue.failures["get_prices"] = ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
    with pytest.raises(ErpError) as raised:
        service.price(industrial_sales, [each("IND-FAN-450", "1")])
    assert raised.value.retryable


def test_missing_policy_is_refused(
    service: QuotationPricingService,
    policy_reader: Any,
    industrial_sales: TenantContext,
    industrial_tenant: UUID,
) -> None:
    del policy_reader.policies[industrial_tenant]
    with pytest.raises(PricingPolicyNotConfigured):
        service.price(industrial_sales, [each("IND-FAN-450", "1")])


def test_foreign_policy_is_refused(
    service: QuotationPricingService,
    policy_reader: Any,
    industrial_sales: TenantContext,
    industrial_tenant: UUID,
    office_tenant: UUID,
    make_policy: Callable[[UUID, str], PricingPolicy],
) -> None:
    policy_reader.policies[industrial_tenant] = make_policy(office_tenant, "EUR")
    with pytest.raises(RuntimeError, match="another tenant"):
        service.price(industrial_sales, [each("IND-FAN-450", "1")])
