"""Two-tenant negative tests through the real PostgreSQL runtime role (not the owner)."""

from collections.abc import Callable
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from sales_platform.enquiries.domain.requirements import RequestedLine
from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.domain.money import Money
from sales_platform.platform.domain.tenancy import AccessDenied, Role, TenantContext
from sales_platform.platform.persistence.tenancy import SqlMembershipReader, tenant_transaction
from sales_platform.quotations.application.pricing import QuotationPricingService
from sales_platform.quotations.domain.pricing import PricedQuotation
from sales_platform.quotations.persistence.pricing_policy import SqlPricingPolicyReader
from sales_platform.quotations.ports.pricing_policy import PricingPolicyNotConfigured
from tests.support.demo_catalogue import DemoCatalogue

type ContextFactory = Callable[[UUID, Role], TenantContext]


def test_membership_is_scoped_to_its_tenant(
    runtime_engine: Engine, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    resolver = TenantContextResolver(SqlMembershipReader(runtime_engine))
    context = resolver.resolve("sales@industrial.example", industrial_tenant)
    assert context.roles == {Role.SALES_OPERATOR}
    # A real member of tenant A gains nothing by naming tenant B.
    with pytest.raises(AccessDenied):
        resolver.resolve("sales@industrial.example", office_tenant)
    with pytest.raises(AccessDenied):
        resolver.resolve("sales@office.example", industrial_tenant)
    with pytest.raises(AccessDenied):
        resolver.resolve("sales@industrial.example", uuid4())


@pytest.mark.parametrize("table", ["memberships", "tenant_pricing_policies"])
def test_rows_are_invisible_without_or_across_tenant_context(
    runtime_engine: Engine, office_tenant: UUID, table: str
) -> None:
    with runtime_engine.connect() as connection:
        count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        assert count == 0, f"{table} visible without tenant context"
    with tenant_transaction(runtime_engine, office_tenant) as connection:
        tenants = set(connection.execute(text(f"SELECT DISTINCT tenant_id FROM {table}")).scalars())
        assert tenants == {office_tenant}


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO memberships (id, tenant_id, subject, role) "
        "VALUES (gen_random_uuid(), :tenant, 'intruder@example.test', 'approver')",
        "DELETE FROM memberships WHERE tenant_id = :tenant",
        "INSERT INTO tenants (id, name) VALUES (gen_random_uuid(), 'intruder')",
        "UPDATE tenant_pricing_policies SET tenant_id = gen_random_uuid() "
        "WHERE tenant_id = :tenant",
    ],
)
def test_runtime_role_cannot_change_access_or_policy(
    runtime_engine: Engine, industrial_tenant: UUID, statement: str
) -> None:
    with (
        pytest.raises(ProgrammingError),
        tenant_transaction(runtime_engine, industrial_tenant) as connection,
    ):
        connection.execute(text(statement), {"tenant": industrial_tenant})


def test_runtime_role_changes_pricing_of_its_own_business_only(
    runtime_engine: Engine, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    """Phase 2 (Q26): the pricing screen saves through a column grant limited by tenant RLS."""
    update_all = text("UPDATE tenant_pricing_policies SET tax_rate = 0")
    with runtime_engine.connect() as connection, connection.begin() as transaction:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(industrial_tenant)}
        )
        assert connection.execute(update_all).rowcount == 1  # its own row only
        other = connection.execute(
            text("SELECT count(*) FROM tenant_pricing_policies WHERE tenant_id = :id"),
            {"id": office_tenant},
        ).scalar_one()
        assert other == 0
        transaction.rollback()


def test_policies_are_read_per_tenant(
    runtime_engine: Engine,
    make_context: ContextFactory,
    industrial_tenant: UUID,
    office_tenant: UUID,
) -> None:
    reader = SqlPricingPolicyReader(runtime_engine)
    industrial = reader.get(make_context(industrial_tenant, Role.VIEWER))
    office = reader.get(make_context(office_tenant, Role.VIEWER))
    assert (industrial.currency, industrial.tax_rate) == ("INR", Decimal("0.180000"))
    assert (office.currency, office.tax_rate) == ("EUR", Decimal("0.190000"))
    with pytest.raises(PricingPolicyNotConfigured):
        reader.get(make_context(uuid4(), Role.VIEWER))


def test_end_to_end_pricing_with_database_policy(
    runtime_engine: Engine, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    resolver = TenantContextResolver(SqlMembershipReader(runtime_engine))
    service = QuotationPricingService(
        DemoCatalogue({industrial_tenant: "industrial", office_tenant: "office"}),
        SqlPricingPolicyReader(runtime_engine),
    )
    context = resolver.resolve("sales@industrial.example", industrial_tenant)
    outcome = service.price(context, [RequestedLine("IND-BOLT-M8", Decimal(100), "each")])
    assert isinstance(outcome, PricedQuotation)
    assert outcome.total == Money(Decimal("280.25"), "INR")
