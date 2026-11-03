from typing import Any
from uuid import UUID

import pytest

from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.domain.tenancy import AccessDenied, Role


def test_resolver_uses_stored_membership_not_requested_tenant(
    membership_reader: Any, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    membership_reader.rows[(industrial_tenant, "sales@a.example")] = frozenset(
        {Role.SALES_OPERATOR}
    )
    resolver = TenantContextResolver(membership_reader)

    context = resolver.resolve("sales@a.example", industrial_tenant)
    assert context.tenant_id == industrial_tenant
    assert context.roles == {Role.SALES_OPERATOR}
    # Asking for another tenant does not grant membership there.
    with pytest.raises(AccessDenied):
        resolver.resolve("sales@a.example", office_tenant)
    with pytest.raises(AccessDenied):
        resolver.resolve("stranger@a.example", industrial_tenant)
