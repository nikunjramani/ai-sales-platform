"""Tenant-scoped repositories. Every query runs in a transaction with the tenant context set.

Queries also filter by tenant_id explicitly; PostgreSQL RLS is the second, independent guard.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, Engine, select, text

from sales_platform.platform.domain.tenancy import Role, TenantStatus
from sales_platform.platform.persistence.models import Membership, Tenant
from sales_platform.platform.ports.tenancy import TenantMembership


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
