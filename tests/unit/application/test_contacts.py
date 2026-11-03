"""Masking for viewers (Q12) and who may merge (D13)."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from sales_platform.application.contacts import ContactService
from sales_platform.domain.tenancy import PermissionDenied, Role, TenantContext
from sales_platform.ports.contacts import ContactSummary

TENANT = uuid4()
ASHA = ContactSummary(
    uuid4(), "Asha", None, "asha@example.com", "+919876543210", False, False, 1,
    datetime(2026, 10, 28, tzinfo=UTC),
)  # fmt: skip


class Reader:
    def contacts(self, tenant_id: UUID, query: str | None, limit: int) -> list[ContactSummary]:
        return [ASHA]


class Merger:
    merged: list[tuple[UUID, UUID]] = []

    def merge(self, tenant_id: UUID, source_id: UUID, target_id: UUID, actor: str) -> bool:
        self.merged.append((source_id, target_id))
        return True


def service() -> ContactService:
    return ContactService(cast(Any, Reader()), Merger(), cast(Any, None))


def context(role: Role) -> TenantContext:
    return TenantContext(TENANT, f"{role}@example", frozenset({role}))


@pytest.mark.parametrize(
    ("role", "email", "phone"),
    [
        (Role.VIEWER, "a***@example.com", "+91*******210"),
        (Role.APPROVER, "asha@example.com", "+919876543210"),
        (Role.SALES_OPERATOR, "asha@example.com", "+919876543210"),
    ],
)
def test_viewers_see_masked_details(role: Role, email: str, phone: str) -> None:
    [found] = service().contacts(context(role), None)
    assert (found.email, found.phone) == (email, phone)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.APPROVER])
def test_only_administrators_and_sales_operators_merge(role: Role) -> None:
    with pytest.raises(PermissionDenied):
        service().merge(context(role), uuid4(), uuid4())
    service().merge(context(Role.SALES_OPERATOR), uuid4(), uuid4())
