"""Moved here by ADR 0026 (module order)."""

from typing import Any
from uuid import UUID

from sqlalchemy import Engine, func, select, update

from sales_platform.conversations.domain.contacts import ConsentStatus
from sales_platform.conversations.persistence.tables import ContactIdentity, Conversation
from sales_platform.conversations.ports.conversations import CustomerTarget
from sales_platform.enquiries.persistence.tables import Enquiry
from sales_platform.platform.persistence.models import Tenant
from sales_platform.platform.persistence.tenancy import tenant_transaction


class SqlCustomerTargets:
    """Where to write about an enquiry, and an identity's consent (either database role)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def for_enquiry(self, tenant_id: UUID, enquiry_id: UUID) -> CustomerTarget | None:
        query = _target_query().join(Enquiry, Enquiry.conversation_id == Conversation.id)
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                query.where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
            ).one_or_none()
        return None if row is None else _target(row)

    def for_identity(
        self, tenant_id: UUID, connection_id: UUID, external_id: str
    ) -> CustomerTarget | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                _target_query().where(
                    ContactIdentity.tenant_id == tenant_id,
                    ContactIdentity.connection_ref == connection_id,
                    ContactIdentity.external_id == external_id,
                )
            ).one_or_none()
        return None if row is None else _target(row)

    def set_consent(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        external_id: str,
        status: ConsentStatus,
        source: str,
    ) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            changed = connection.execute(
                update(ContactIdentity)
                .where(
                    ContactIdentity.tenant_id == tenant_id,
                    ContactIdentity.connection_ref == connection_id,
                    ContactIdentity.external_id == external_id,
                    ContactIdentity.consent_status != status.value,
                )
                .values(
                    consent_status=status.value,
                    consent_changed_at=func.now(),
                    consent_source=source[:100],
                )
            ).rowcount
        return bool(changed)

    def consent(self, tenant_id: UUID, connection_id: UUID, external_id: str) -> ConsentStatus:
        with tenant_transaction(self._engine, tenant_id) as connection:
            status = connection.execute(
                select(ContactIdentity.consent_status).where(
                    ContactIdentity.tenant_id == tenant_id,
                    ContactIdentity.connection_ref == connection_id,
                    ContactIdentity.external_id == external_id,
                )
            ).scalar_one_or_none()
        return ConsentStatus.IMPLIED if status is None else ConsentStatus(status)


def _target_query() -> Any:
    return (
        select(
            Conversation.id,
            Conversation.connection_ref,
            ContactIdentity.id.label("identity_id"),
            ContactIdentity.external_id,
            ContactIdentity.consent_status,
            Tenant.acknowledge_enquiries,
            Tenant.name.label("business_name"),
        )
        .select_from(Conversation)
        .join(ContactIdentity, ContactIdentity.id == Conversation.identity_id)
        .join(Tenant, Tenant.id == Conversation.tenant_id)
    )


def _target(row: Any) -> CustomerTarget:
    return CustomerTarget(
        row.id,
        row.connection_ref,
        row.identity_id,
        row.external_id,
        ConsentStatus(row.consent_status),
        row.acknowledge_enquiries,
        row.business_name,
    )
