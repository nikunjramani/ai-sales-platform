"""Turn an authenticated subject plus a requested tenant into a trusted TenantContext."""

from uuid import UUID

from sales_platform.platform.domain.tenancy import AccessDenied, TenantContext
from sales_platform.platform.ports.tenancy import MembershipReader


class TenantContextResolver:
    """The only intended way to build a TenantContext.

    subject must come from authentication (a portal session or API key; ADR 0013). The
    requested tenant is only a request; stored membership decides. Non-members get the same
    error as unknown tenants.
    """

    def __init__(self, memberships: MembershipReader) -> None:
        self._memberships = memberships

    def resolve(self, subject: str, tenant_id: UUID) -> TenantContext:
        roles = self._memberships.roles_for(tenant_id, subject)
        if not roles:
            raise AccessDenied("Not a member of the requested tenant")
        return TenantContext(tenant_id=tenant_id, subject=subject, roles=roles)
