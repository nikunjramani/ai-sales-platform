"""Tenant-scoped repositories. Every query runs in a transaction with the tenant context set.

Queries also filter by tenant_id explicitly; PostgreSQL RLS is the second, independent guard.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

from sqlalchemy import Connection, Engine, select, text

from sales_platform.domain.money import RoundingMode
from sales_platform.domain.pricing import PricingPolicy
from sales_platform.domain.tenancy import Role, TenantContext, TenantStatus
from sales_platform.persistence.models import Membership, Tenant, TenantPricingPolicy
from sales_platform.ports.tenancy import PricingPolicyNotConfigured, TenantMembership


@contextmanager
def tenant_transaction(engine: Engine, tenant_id: UUID) -> Iterator[Connection]:
    """Open a transaction whose RLS tenant context ends when the transaction ends.

    set_config(..., true) is transaction-local, so a pooled connection cannot carry this
    tenant into its next checkout.
    """
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        yield connection


class SqlMembershipReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def roles_for(self, tenant_id: UUID, subject: str) -> frozenset[Role]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            values = connection.execute(
                select(Membership.role).where(
                    Membership.tenant_id == tenant_id, Membership.subject == subject
                )
            ).scalars()
            return frozenset(Role(value) for value in values)

    def tenants_for(self, subject: str) -> list[TenantMembership]:
        """Uses the narrow membership_self_lookup policy (app.subject), not a tenant context."""
        with self._engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.subject', :subject, true)"), {"subject": subject}
            )
            rows = connection.execute(
                select(Membership.tenant_id, Tenant.name, Tenant.status, Membership.role)
                .join(Tenant, Tenant.id == Membership.tenant_id)
                .where(Membership.subject == subject)
                .order_by(Tenant.name, Membership.tenant_id)
            ).all()
        grouped: dict[UUID, TenantMembership] = {}
        for row in rows:
            known = grouped.get(row.tenant_id)
            roles = (known.roles if known else frozenset()) | {Role(row.role)}
            grouped[row.tenant_id] = TenantMembership(
                row.tenant_id, row.name, roles, TenantStatus(row.status)
            )
        return list(grouped.values())


class SqlPricingPolicyReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, context: TenantContext) -> PricingPolicy:
        table = TenantPricingPolicy
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            row = connection.execute(
                select(
                    table.tenant_id,
                    table.version,
                    table.currency,
                    table.minor_units,
                    table.rounding_mode,
                    table.tax_rate,
                    table.max_price_age_seconds,
                    table.max_stock_age_seconds,
                ).where(table.tenant_id == context.tenant_id)
            ).one_or_none()
        if row is None:
            raise PricingPolicyNotConfigured("No pricing policy is configured for this tenant")
        if (
            row.currency is None
            or row.minor_units is None
            or row.rounding_mode is None
            or row.tax_rate is None
            or row.max_price_age_seconds is None
            or row.max_stock_age_seconds is None
        ):
            # "Set later" (phase 2): never price with values nobody has confirmed.
            raise PricingPolicyNotConfigured("The pricing settings are not complete yet")
        return PricingPolicy(
            tenant_id=row.tenant_id,
            version=row.version,
            currency=row.currency,
            minor_units=row.minor_units,
            rounding=RoundingMode(row.rounding_mode),
            tax_rate=row.tax_rate,
            max_price_age=timedelta(seconds=row.max_price_age_seconds),
            max_stock_age=timedelta(seconds=row.max_stock_age_seconds),
        )
