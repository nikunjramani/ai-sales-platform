"""Channel enrollment, bindings and action references (tenant-scoped).

SqlNotificationStore runs as the worker (builds review packets, stores references before
sending). SqlChannelStore runs as the API (enrollment, inbound action lookup, revocation).
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, Row, and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.domain.connections import ConnectionStatus
from sales_platform.domain.quotations import ReviewPacket, TaskStatus
from sales_platform.domain.tenancy import Role
from sales_platform.persistence.connections import matches_key
from sales_platform.persistence.models import (
    ApprovalActionRef,
    ApprovalTask,
    ChannelBindingRow,
    ChannelEnrollment,
    ConnectionRow,
    Enquiry,
    Membership,
    OutboundMessageRow,
    Quotation,
    QuotationVersion,
    Tenant,
)
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.channels import ActionReference, ChannelBinding

_BINDING_COLUMNS = (
    ChannelBindingRow.id,
    ChannelBindingRow.tenant_id,
    ChannelBindingRow.subject,
    ChannelBindingRow.channel,
    ChannelBindingRow.connection_id,
    ChannelBindingRow.external_user_id,
    ChannelBindingRow.created_at,
    ChannelBindingRow.revoked_at,
    ChannelBindingRow.connection_ref,
)


def _binding(row: Row[Any]) -> ChannelBinding:
    return ChannelBinding(
        binding_id=row.id,
        tenant_id=row.tenant_id,
        subject=row.subject,
        channel=row.channel,
        connection_id=row.connection_id,
        external_user_id=row.external_user_id,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        connection_ref=row.connection_ref,
    )


class SqlNotificationStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def review_packet(self, tenant_id: UUID, task_id: UUID) -> ReviewPacket | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(
                    ApprovalTask.id,
                    ApprovalTask.quotation_id,
                    ApprovalTask.version,
                    ApprovalTask.content_sha256,
                    QuotationVersion.content,
                    Enquiry.customer_reference,
                    Tenant.name,
                )
                .join(
                    QuotationVersion,
                    (QuotationVersion.tenant_id == ApprovalTask.tenant_id)
                    & (QuotationVersion.id == ApprovalTask.version_id),
                )
                .join(
                    Quotation,
                    (Quotation.tenant_id == ApprovalTask.tenant_id)
                    & (Quotation.id == ApprovalTask.quotation_id),
                )
                .join(
                    Enquiry,
                    (Enquiry.tenant_id == Quotation.tenant_id)
                    & (Enquiry.id == Quotation.enquiry_id),
                )
                .join(Tenant, Tenant.id == ApprovalTask.tenant_id)
                .where(
                    ApprovalTask.tenant_id == tenant_id,
                    ApprovalTask.id == task_id,
                    ApprovalTask.status == TaskStatus.PENDING.value,
                )
            ).one_or_none()
        if row is None:
            return None
        return ReviewPacket(
            tenant_id=tenant_id,
            tenant_name=row.name,
            task_id=row.id,
            quotation_id=row.quotation_id,
            version=row.version,
            content_sha256=row.content_sha256,
            customer_reference=row.customer_reference,
            content=row.content,
        )

    def approver_bindings(self, tenant_id: UUID) -> list[ChannelBinding]:
        is_approver = exists().where(
            Membership.tenant_id == ChannelBindingRow.tenant_id,
            Membership.subject == ChannelBindingRow.subject,
            Membership.role == Role.APPROVER.value,
        )
        # A disabled or broken connection gets no approval requests (P1-Q10).
        usable = exists().where(
            ConnectionRow.tenant_id == ChannelBindingRow.tenant_id,
            ConnectionRow.id == ChannelBindingRow.connection_ref,
            ConnectionRow.status == ConnectionStatus.ACTIVE.value,
        )
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                select(*_BINDING_COLUMNS)
                .where(
                    ChannelBindingRow.tenant_id == tenant_id,
                    ChannelBindingRow.revoked_at.is_(None),
                    is_approver,
                    usable,
                )
                .order_by(ChannelBindingRow.created_at, ChannelBindingRow.id)
            ).all()
        return [_binding(row) for row in rows]

    def prepare_reference(
        self,
        tenant_id: UUID,
        task_id: UUID,
        binding_id: UUID,
        token_sha256: str,
        expires_at: datetime,
    ) -> UUID | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                insert(ApprovalActionRef)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    task_id=task_id,
                    binding_id=binding_id,
                    token_sha256=token_sha256,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "task_id", "binding_id"])
            )
            row = connection.execute(
                select(
                    ApprovalActionRef.id,
                    ApprovalActionRef.token_sha256,
                    ApprovalActionRef.sent_at,
                )
                .where(
                    ApprovalActionRef.tenant_id == tenant_id,
                    ApprovalActionRef.task_id == task_id,
                    ApprovalActionRef.binding_id == binding_id,
                )
                .with_for_update()
            ).one()
            if row.sent_at is not None:
                return None
            if row.token_sha256 != token_sha256:
                # Never delivered (an earlier attempt failed): replace the token, so only
                # the one we are about to send can be used.
                connection.execute(
                    update(ApprovalActionRef)
                    .where(ApprovalActionRef.tenant_id == tenant_id, ApprovalActionRef.id == row.id)
                    .values(token_sha256=token_sha256, expires_at=expires_at)
                )
        reference_id: UUID = row.id
        return reference_id

    def mark_sent(self, tenant_id: UUID, reference_id: UUID) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                update(ApprovalActionRef)
                .where(
                    ApprovalActionRef.tenant_id == tenant_id, ApprovalActionRef.id == reference_id
                )
                .values(sent_at=func.now())
            )

    def link_message(self, tenant_id: UUID, message_id: UUID, reference_id: UUID) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                update(OutboundMessageRow)
                .where(
                    OutboundMessageRow.tenant_id == tenant_id,
                    OutboundMessageRow.id == message_id,
                )
                .values(action_ref_id=reference_id)
            )


class SqlChannelStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_enrollment(
        self,
        tenant_id: UUID,
        subject: str,
        channel: str,
        code_sha256: str,
        expires_at: datetime,
        connection_ref: UUID | None = None,
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                insert(ChannelEnrollment).values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    subject=subject,
                    channel=channel,
                    code_sha256=code_sha256,
                    expires_at=expires_at,
                    connection_ref=connection_ref,
                )
            )

    def enroll(
        self,
        tenant_id: UUID,
        channel: str,
        connection_id: str,
        external_user_id: str,
        code_sha256: str,
        now: datetime,
    ) -> ChannelBinding | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            enrollment = connection.execute(
                select(
                    ChannelEnrollment.id,
                    ChannelEnrollment.subject,
                    ChannelEnrollment.channel,
                    ChannelEnrollment.connection_ref,
                    ChannelEnrollment.expires_at,
                    ChannelEnrollment.consumed_at,
                )
                .where(
                    ChannelEnrollment.tenant_id == tenant_id,
                    ChannelEnrollment.code_sha256 == code_sha256,
                )
                .with_for_update()
            ).one_or_none()
            if enrollment is None or enrollment.channel != channel:
                return None
            # The account must belong to an active connection of this business (ADR 0018);
            # a code issued for one connection works only there.
            connection_ref = connection.execute(
                select(ConnectionRow.id).where(
                    ConnectionRow.tenant_id == tenant_id,
                    matches_key(channel, connection_id),
                    ConnectionRow.status == ConnectionStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if connection_ref is None or enrollment.connection_ref not in (None, connection_ref):
                return None
            same_account = and_(
                ChannelBindingRow.tenant_id == tenant_id,
                ChannelBindingRow.connection_ref == connection_ref,
                ChannelBindingRow.external_user_id == external_user_id,
            )
            if enrollment.consumed_at is not None:
                # Replay of the same enrollment event returns the binding it created.
                replay = connection.execute(
                    select(*_BINDING_COLUMNS).where(
                        same_account, ChannelBindingRow.enrollment_id == enrollment.id
                    )
                ).one_or_none()
                return None if replay is None else _binding(replay)
            if enrollment.expires_at <= now:
                return None
            taken = connection.execute(
                select(ChannelBindingRow.id).where(
                    same_account, ChannelBindingRow.revoked_at.is_(None)
                )
            ).first()
            if taken is not None:
                return None  # this account is already bound; revoke it first
            binding_id = uuid4()
            connection.execute(
                insert(ChannelBindingRow).values(
                    id=binding_id,
                    tenant_id=tenant_id,
                    subject=enrollment.subject,
                    channel=channel,
                    connection_id=connection_id,
                    connection_ref=connection_ref,
                    external_user_id=external_user_id,
                    enrollment_id=enrollment.id,
                )
            )
            connection.execute(
                update(ChannelEnrollment)
                .where(
                    ChannelEnrollment.tenant_id == tenant_id,
                    ChannelEnrollment.id == enrollment.id,
                )
                .values(consumed_at=func.now())
            )
            created = connection.execute(
                select(*_BINDING_COLUMNS).where(
                    ChannelBindingRow.tenant_id == tenant_id, ChannelBindingRow.id == binding_id
                )
            ).one()
        return _binding(created)

    def find_reference(
        self, tenant_id: UUID, token_sha256: str
    ) -> tuple[ActionReference, ChannelBinding] | None:
        return self._reference(tenant_id, ApprovalActionRef.token_sha256 == token_sha256)

    def get_reference(
        self, tenant_id: UUID, reference_id: UUID
    ) -> tuple[ActionReference, ChannelBinding] | None:
        return self._reference(tenant_id, ApprovalActionRef.id == reference_id)

    def _reference(
        self, tenant_id: UUID, condition: Any
    ) -> tuple[ActionReference, ChannelBinding] | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(
                    ApprovalActionRef.id.label("reference_id"),
                    ApprovalActionRef.task_id,
                    ApprovalActionRef.expires_at,
                    ApprovalActionRef.consumed_at,
                    ApprovalTask.content_sha256,
                    *_BINDING_COLUMNS,
                )
                .join(
                    ApprovalTask,
                    (ApprovalTask.tenant_id == ApprovalActionRef.tenant_id)
                    & (ApprovalTask.id == ApprovalActionRef.task_id),
                )
                .join(
                    ChannelBindingRow,
                    (ChannelBindingRow.tenant_id == ApprovalActionRef.tenant_id)
                    & (ChannelBindingRow.id == ApprovalActionRef.binding_id),
                )
                .where(
                    ApprovalActionRef.tenant_id == tenant_id,
                    condition,
                    ApprovalActionRef.sent_at.is_not(None),
                )
            ).one_or_none()
        if row is None:
            return None
        reference = ActionReference(
            reference_id=row.reference_id,
            task_id=row.task_id,
            binding_id=row.id,
            content_sha256=row.content_sha256,
            expires_at=row.expires_at,
            consumed_at=row.consumed_at,
        )
        return reference, _binding(row)

    def bindings_for(self, tenant_id: UUID, subject: str | None) -> list[ChannelBinding]:
        query = (
            select(*_BINDING_COLUMNS)
            .where(ChannelBindingRow.tenant_id == tenant_id)
            .order_by(ChannelBindingRow.created_at, ChannelBindingRow.id)
        )
        if subject is not None:
            query = query.where(ChannelBindingRow.subject == subject)
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(query).all()
        return [_binding(row) for row in rows]

    def revoke(self, tenant_id: UUID, binding_id: UUID, subject: str | None) -> bool:
        query = (
            update(ChannelBindingRow)
            .where(
                ChannelBindingRow.tenant_id == tenant_id,
                ChannelBindingRow.id == binding_id,
                ChannelBindingRow.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
        )
        if subject is not None:
            query = query.where(ChannelBindingRow.subject == subject)
        with tenant_transaction(self._engine, tenant_id) as connection:
            return connection.execute(query).rowcount == 1
