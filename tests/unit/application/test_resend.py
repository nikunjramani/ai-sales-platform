"""Resend a failed customer message (phase 5): who may, which messages, and only once."""

from uuid import UUID, uuid4

import pytest

from sales_platform.application.resend import MessageNotResendable, ResendService
from sales_platform.domain.connections import Purpose
from sales_platform.domain.messages import MessageStatus, TemplateKey
from sales_platform.domain.tenancy import PermissionDenied, Role, TenantContext
from sales_platform.ports.messages import MessageLinks, NewMessage
from tests.unit.conftest import MemoryOutbox, MemoryQuotations


def queue(outbox: MemoryOutbox, tenant_id: UUID, purpose: Purpose, status: MessageStatus) -> UUID:
    message_id = outbox.enqueue(
        NewMessage(
            tenant_id,
            uuid4(),
            "one_click_tool",
            purpose,
            TemplateKey.QUOTATION_READY,
            {},
            "customer-1",
            f"key-{uuid4()}",
            "Your quotation is ready",
            {},
            MessageLinks(),
            uuid4(),
        )  # fmt: skip
    )
    outbox.set_status(
        tenant_id, message_id, status, expected=frozenset({MessageStatus.PENDING}), error="x"
    )
    return message_id


def context(tenant_id: UUID, role: Role) -> TenantContext:
    return TenantContext(tenant_id, f"user:{role.value}", frozenset({role}))


@pytest.fixture
def outbox(quotation_db: MemoryQuotations) -> MemoryOutbox:
    return MemoryOutbox(quotation_db)


def test_sales_operators_resend_a_failed_customer_message_once(
    outbox: MemoryOutbox, industrial_tenant: UUID
) -> None:
    failed = queue(outbox, industrial_tenant, Purpose.CUSTOMER_MESSAGES, MessageStatus.FAILED)
    service = ResendService(outbox)

    first = service.resend(context(industrial_tenant, Role.SALES_OPERATOR), failed)
    again = service.resend(context(industrial_tenant, Role.ADMINISTRATOR), failed)

    copy = outbox.get(industrial_tenant, first)
    assert first == again != failed
    assert copy is not None and copy.status is MessageStatus.PENDING
    assert copy.body_text == "Your quotation is ready"
    assert copy.params["resent_by"] == "user:sales_operator"
    original = outbox.get(industrial_tenant, failed)
    assert original is not None and original.status is MessageStatus.FAILED  # kept as it was


@pytest.mark.parametrize("role", [Role.APPROVER, Role.VIEWER])
def test_others_may_not_resend(outbox: MemoryOutbox, industrial_tenant: UUID, role: Role) -> None:
    failed = queue(outbox, industrial_tenant, Purpose.CUSTOMER_MESSAGES, MessageStatus.FAILED)
    with pytest.raises(PermissionDenied):
        ResendService(outbox).resend(context(industrial_tenant, role), failed)


def test_unknown_manager_or_other_business_messages_are_not_resent(
    outbox: MemoryOutbox, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    unknown = queue(outbox, industrial_tenant, Purpose.CUSTOMER_MESSAGES, MessageStatus.UNKNOWN)
    manager = queue(outbox, industrial_tenant, Purpose.MANAGER_APPROVALS, MessageStatus.FAILED)
    other = queue(outbox, office_tenant, Purpose.CUSTOMER_MESSAGES, MessageStatus.FAILED)
    admin = context(industrial_tenant, Role.ADMINISTRATOR)
    for message_id in (unknown, manager, other):
        with pytest.raises(MessageNotResendable):
            ResendService(outbox).resend(admin, message_id)
