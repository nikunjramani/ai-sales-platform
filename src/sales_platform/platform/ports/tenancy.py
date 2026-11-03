"""Contracts for reading tenant membership and tenant-owned configuration."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sales_platform.platform.domain.tenancy import Role, TenantStatus


class MembershipReader(Protocol):
    def roles_for(self, tenant_id: UUID, subject: str) -> frozenset[Role]:
        """Return the subject's roles in this tenant, or an empty set if not a member."""
        ...


@dataclass(frozen=True)
class TenantMembership:
    tenant_id: UUID
    tenant_name: str
    roles: frozenset[Role]
    status: TenantStatus = TenantStatus.ACTIVE


class TenantStatusReader(Protocol):
    def status(self, tenant_id: UUID) -> TenantStatus:
        """setting_up, active or suspended (phase 2); gates read it at the moment they act."""
        ...


class MembershipDirectory(Protocol):
    def tenants_for(self, subject: str) -> list[TenantMembership]:
        """Every tenant where this subject has a membership (used for login and tenant choice)."""
        ...
