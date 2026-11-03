"""Moved here by ADR 0026 (module order)."""

from uuid import UUID

from sqlalchemy import Connection, column, exists, func, select, table

from sales_platform.conversations.persistence.tables import Conversation
from sales_platform.enquiries.domain.follow_ups import BEING_READ, OpenEnquiry
from sales_platform.enquiries.domain.intake import EnquiryStatus
from sales_platform.enquiries.persistence.tables import Enquiry, WorkflowRun

_ROUTABLE = [status.value for status in (*BEING_READ, EnquiryStatus.NEEDS_CLARIFICATION)]


EXTRACTION_GRAPHS = "enquiry-extraction-%"

# The one database-level read across the module order (ADR 0026): an enquiry that already has a
# quotation never takes follow-ups. The quotations table is owned by quotations (a later module),
# so it is named here, read-only, instead of importing quotations' code.
_QUOTATIONS = table("quotations", column("tenant_id"), column("enquiry_id"))


def open_enquiries(
    connection: Connection, tenant_id: UUID, conversation_id: UUID
) -> list[tuple[OpenEnquiry, UUID]]:
    """Enquiries a message could continue, locked, each with its correlation ID."""
    asked = (
        select(func.max(WorkflowRun.updated_at))
        .where(
            WorkflowRun.tenant_id == Enquiry.tenant_id,
            WorkflowRun.enquiry_id == Enquiry.id,
            WorkflowRun.graph_version.like(EXTRACTION_GRAPHS),
            WorkflowRun.status == EnquiryStatus.NEEDS_CLARIFICATION.value,
        )
        .scalar_subquery()
    )
    quoted = exists().where(
        _QUOTATIONS.c.tenant_id == Enquiry.tenant_id, _QUOTATIONS.c.enquiry_id == Enquiry.id
    )
    rows = connection.execute(
        select(
            Enquiry.id,
            Enquiry.status,
            Enquiry.received_at,
            Enquiry.correlation_id,
            asked.label("asked_at"),
            quoted.label("quoted"),
        )
        .where(
            Enquiry.tenant_id == tenant_id,
            Enquiry.conversation_id == conversation_id,
            Enquiry.status.in_(_ROUTABLE),
        )
        .order_by(Enquiry.received_at)
        .with_for_update(of=Enquiry)
    ).all()
    return [
        (
            OpenEnquiry(
                row.id, EnquiryStatus(row.status), row.received_at, row.asked_at, row.quoted
            ),
            row.correlation_id,
        )
        for row in rows
    ]


def lock_conversation_of(connection: Connection, tenant_id: UUID, enquiry_id: UUID) -> None:
    """Worker side of the lock order: the enquiry's conversation first (if it has one)."""
    conversation_id = connection.execute(
        select(Enquiry.conversation_id).where(
            Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id
        )
    ).scalar_one_or_none()
    if conversation_id is not None:
        connection.execute(
            select(Conversation.id)
            .where(Conversation.tenant_id == tenant_id, Conversation.id == conversation_id)
            .with_for_update()
        )
