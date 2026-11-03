"""Durable intake records (API runtime role) and preparation updates (worker role)."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert

from sales_platform.domain.attachments import AttachmentStatus
from sales_platform.domain.conversations import FollowUp
from sales_platform.domain.intake import (
    EnquiryStatus,
    InboundEnvelope,
    InboundEventType,
)
from sales_platform.domain.jobs import JobKind, JobStatus
from sales_platform.persistence.conversations import (
    EXTRACTION_GRAPHS,
    ConversationRef,
    append_inbound,
    ensure_conversation,
    open_enquiries,
)
from sales_platform.persistence.models import (
    Attachment,
    ConversationMessage,
    Enquiry,
    InboxEvent,
    Job,
    WorkflowRun,
)
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.intake import (
    AttachmentView,
    EnquiryRouter,
    EnquirySummary,
    EnquiryView,
    ExtractedLineView,
    ExtractionView,
    Receipt,
    StoredAttachment,
)

MAX_JOB_ATTEMPTS = 5


def _receipt(
    connection: Connection,
    tenant_id: UUID,
    connection_ref: UUID,
    provider_event_id: str,
) -> Receipt | None:
    """The enquiry a stored message belongs to (its own, or the one it continued)."""
    row = connection.execute(
        select(
            Enquiry.id,
            Enquiry.status,
            InboxEvent.payload_sha256,
            InboxEvent.correlation_id,
            InboxEvent.received_at,
            (Enquiry.inbox_event_id != InboxEvent.id).label("continued"),
        )
        .join(
            ConversationMessage,
            (ConversationMessage.tenant_id == InboxEvent.tenant_id)
            & (ConversationMessage.inbox_event_id == InboxEvent.id),
        )
        .join(
            Enquiry,
            (Enquiry.tenant_id == ConversationMessage.tenant_id)
            & (Enquiry.id == ConversationMessage.enquiry_id),
        )
        .where(
            InboxEvent.tenant_id == tenant_id,
            InboxEvent.connection_ref == connection_ref,
            InboxEvent.provider_event_id == provider_event_id,
            InboxEvent.event_type == InboundEventType.CUSTOMER_MESSAGE.value,
        )
    ).one_or_none()
    if row is None:
        return None
    return Receipt(
        enquiry_id=row.id,
        status=EnquiryStatus(row.status),
        payload_sha256=row.payload_sha256,
        correlation_id=row.correlation_id,
        received_at=row.received_at,
        continued=row.continued,
    )


class SqlIntakeStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_receipt(
        self, tenant_id: UUID, connection_ref: UUID, provider_event_id: str
    ) -> Receipt | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return _receipt(connection, tenant_id, connection_ref, provider_event_id)

    def record(
        self,
        envelope: InboundEnvelope,
        payload_sha256: str,
        enquiry_id: UUID,
        router: EnquiryRouter | None = None,
        hold_for_review: bool = False,
    ) -> tuple[Receipt, bool]:
        tenant_id = envelope.tenant_id
        with tenant_transaction(self._engine, tenant_id) as connection:
            # If a concurrent request holds the same key, PostgreSQL waits for it to finish;
            # DO NOTHING then returns no row and we read the winner's receipt instead.
            inserted = connection.execute(
                insert(InboxEvent)
                .values(
                    id=envelope.event_id,
                    tenant_id=tenant_id,
                    channel=envelope.channel,
                    connection_id=envelope.connection_id,
                    connection_ref=envelope.connection_ref,
                    event_type=InboundEventType.CUSTOMER_MESSAGE.value,
                    provider_event_id=envelope.provider_event_id,
                    payload_sha256=payload_sha256,
                    envelope=envelope.to_json(),
                    received_at=envelope.received_at,
                    correlation_id=envelope.correlation_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "tenant_id",
                        "connection_ref",
                        "provider_event_id",
                        "event_type",
                    ]
                )
                .returning(InboxEvent.id)
            ).scalar_one_or_none()
            if inserted is None:
                existing = _receipt(
                    connection,
                    tenant_id,
                    envelope.connection_ref,
                    envelope.provider_event_id,
                )
                if existing is None:
                    raise RuntimeError("Conflicting inbox event is not visible")
                return existing, False

            conversation = None
            if envelope.sender_external_id is not None:
                conversation = ensure_conversation(
                    connection,
                    tenant_id,
                    envelope.connection_ref,
                    envelope.sender_external_id,
                    verified=True,
                    display_name=envelope.sender_external_id,
                    at=envelope.received_at,
                )
            if conversation is not None and router is not None:
                candidates = open_enquiries(connection, tenant_id, conversation.conversation_id)
                follow_up = router([item for item, _ in candidates], envelope.received_at)
                if follow_up is not None:
                    correlation = dict(
                        (item.enquiry_id, correlation_id) for item, correlation_id in candidates
                    )
                    receipt = _continue(
                        connection,
                        envelope,
                        payload_sha256,
                        conversation,
                        follow_up,
                        correlation[follow_up.enquiry_id],
                        hold_for_review,
                    )
                    return receipt, True
            _create(connection, envelope, enquiry_id, conversation, hold_for_review)
        receipt = Receipt(
            enquiry_id=enquiry_id,
            status=EnquiryStatus.RECEIVED,
            payload_sha256=payload_sha256,
            correlation_id=envelope.correlation_id,
            received_at=envelope.received_at,
        )
        return receipt, True

    def get_enquiry(self, tenant_id: UUID, enquiry_id: UUID) -> EnquiryView | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            enquiry = connection.execute(
                select(
                    Enquiry.id,
                    Enquiry.channel,
                    Enquiry.status,
                    Enquiry.customer_reference,
                    Enquiry.received_at,
                    Enquiry.correlation_id,
                    InboxEvent.connection_ref,
                )
                .join(
                    InboxEvent,
                    (InboxEvent.tenant_id == Enquiry.tenant_id)
                    & (InboxEvent.id == Enquiry.inbox_event_id),
                )
                .where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
            ).one_or_none()
            if enquiry is None:
                return None
            attachments = connection.execute(
                select(
                    Attachment.id,
                    Attachment.filename,
                    Attachment.declared_content_type,
                    Attachment.size_bytes,
                    Attachment.status,
                    Attachment.rejection_reason,
                )
                .where(Attachment.tenant_id == tenant_id, Attachment.enquiry_id == enquiry_id)
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
            run = connection.execute(
                select(WorkflowRun.status, WorkflowRun.outcome)
                .where(
                    WorkflowRun.tenant_id == tenant_id,
                    WorkflowRun.enquiry_id == enquiry_id,
                    WorkflowRun.graph_version.like("enquiry-extraction-%"),
                    WorkflowRun.outcome.is_not(None),
                )
                .order_by(WorkflowRun.updated_at.desc())
                .limit(1)
            ).one_or_none()
        return EnquiryView(
            enquiry_id=enquiry.id,
            channel=enquiry.channel,
            status=EnquiryStatus(enquiry.status),
            customer_reference=enquiry.customer_reference,
            received_at=enquiry.received_at,
            correlation_id=enquiry.correlation_id,
            attachments=tuple(
                AttachmentView(
                    attachment_id=row.id,
                    filename=row.filename,
                    declared_content_type=row.declared_content_type,
                    size_bytes=row.size_bytes,
                    status=AttachmentStatus(row.status),
                    rejection_reason=row.rejection_reason,
                )
                for row in attachments
            ),
            extraction=None if run is None else _extraction_view(run.status, run.outcome),
            connection_ref=enquiry.connection_ref,
        )


def _queue_preparation(
    connection: Connection, tenant_id: UUID, payload: dict[str, str], correlation_id: UUID
) -> None:
    connection.execute(
        insert(Job).values(
            id=uuid4(),
            tenant_id=tenant_id,
            kind=JobKind.PREPARE_ENQUIRY.value,
            payload=payload,
            status=JobStatus.PENDING.value,
            max_attempts=MAX_JOB_ATTEMPTS,
            run_at=func.now(),
            correlation_id=correlation_id,
        )
    )


def _insert_attachments(
    connection: Connection, envelope: InboundEnvelope, enquiry_id: UUID
) -> list[UUID]:
    for item in envelope.attachments:
        connection.execute(
            insert(Attachment).values(
                id=item.attachment_id,
                tenant_id=envelope.tenant_id,
                enquiry_id=enquiry_id,
                filename=item.filename,
                declared_content_type=item.declared_content_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                status=AttachmentStatus.QUARANTINED.value,
                storage_key=item.storage_key,
            )
        )
    return [item.attachment_id for item in envelope.attachments]


def _create(
    connection: Connection,
    envelope: InboundEnvelope,
    enquiry_id: UUID,
    conversation: ConversationRef | None,
    hold_for_review: bool = False,
) -> None:
    """A new enquiry with its first message and the preparation job (which also acknowledges
    it to the customer when the business wants that, D14)."""
    tenant_id = envelope.tenant_id
    connection.execute(
        insert(Enquiry).values(
            id=enquiry_id,
            tenant_id=tenant_id,
            inbox_event_id=envelope.event_id,
            channel=envelope.channel,
            status=EnquiryStatus.RECEIVED.value,
            customer_reference=envelope.sender_external_id,
            text=envelope.text,
            correlation_id=envelope.correlation_id,
            received_at=envelope.received_at,
            conversation_id=None if conversation is None else conversation.conversation_id,
            contact_id=None if conversation is None else conversation.contact_id,
        )
    )
    attachment_ids = _insert_attachments(connection, envelope, enquiry_id)
    append_inbound(
        connection,
        tenant_id,
        None if conversation is None else conversation.conversation_id,
        enquiry_id,
        envelope.event_id,
        envelope.text,
        attachment_ids,
        envelope.received_at,
    )
    payload = {"enquiry_id": str(enquiry_id)}
    if hold_for_review:
        payload["hold"] = "true"
    elif conversation is not None:
        payload["acknowledge"] = "true"
    _queue_preparation(connection, tenant_id, payload, envelope.correlation_id)


def _continue(
    connection: Connection,
    envelope: InboundEnvelope,
    payload_sha256: str,
    conversation: ConversationRef,
    follow_up: FollowUp,
    correlation_id: UUID,
    hold_for_review: bool = False,
) -> Receipt:
    """Add the message to an open enquiry. A waiting one goes back to received and is prepared
    and read again; one still being read is read again when its round ends (Q3)."""
    tenant_id = envelope.tenant_id
    enquiry_id = follow_up.enquiry_id
    attachment_ids = _insert_attachments(connection, envelope, enquiry_id)
    append_inbound(
        connection,
        tenant_id,
        conversation.conversation_id,
        enquiry_id,
        envelope.event_id,
        envelope.text,
        attachment_ids,
        envelope.received_at,
    )
    if follow_up.reopen:
        connection.execute(
            update(Enquiry)
            .where(
                Enquiry.tenant_id == tenant_id,
                Enquiry.id == enquiry_id,
                Enquiry.status == EnquiryStatus.NEEDS_CLARIFICATION.value,
            )
            .values(status=EnquiryStatus.RECEIVED.value, updated_at=func.now())
        )
        payload = {"enquiry_id": str(enquiry_id)}
        if hold_for_review:
            payload["hold"] = "true"
        _queue_preparation(connection, tenant_id, payload, correlation_id)
    status = connection.execute(
        select(Enquiry.status).where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
    ).scalar_one()
    return Receipt(
        enquiry_id=enquiry_id,
        status=EnquiryStatus(status),
        payload_sha256=payload_sha256,
        correlation_id=envelope.correlation_id,
        received_at=envelope.received_at,
        continued=True,
    )


def _extraction_view(status: str, outcome: dict[str, Any]) -> ExtractionView:
    return ExtractionView(
        outcome=status,
        lines=tuple(
            ExtractedLineView(str(line["sku"]), str(line["quantity"]), str(line["unit"]))
            for line in outcome.get("lines", [])
        ),
        questions=tuple(str(question) for question in outcome.get("questions", [])),
        review_reasons=tuple(str(reason) for reason in outcome.get("review_reasons", [])),
    )


class SqlPreparationStore:
    """Worker-role access; every method sets the job's tenant context first."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def attachments_for(self, tenant_id: UUID, enquiry_id: UUID) -> list[StoredAttachment] | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            exists = connection.execute(
                select(Enquiry.id).where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
            ).one_or_none()
            if exists is None:
                return None
            rows = connection.execute(
                select(Attachment.id, Attachment.declared_content_type, Attachment.status)
                .where(Attachment.tenant_id == tenant_id, Attachment.enquiry_id == enquiry_id)
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
        return [
            StoredAttachment(row.id, row.declared_content_type, AttachmentStatus(row.status))
            for row in rows
        ]

    def mark_attachment(
        self,
        tenant_id: UUID,
        attachment_id: UUID,
        status: AttachmentStatus,
        detected_content_type: str | None,
        reason: str | None,
        storage_key: str,
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                update(Attachment)
                .where(Attachment.tenant_id == tenant_id, Attachment.id == attachment_id)
                .values(
                    status=status.value,
                    detected_content_type=detected_content_type,
                    rejection_reason=reason,
                    storage_key=storage_key,
                    updated_at=func.now(),
                )
            )

    def finish_preparation(
        self, tenant_id: UUID, enquiry_id: UUID, status: EnquiryStatus, correlation_id: UUID
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            # Only the first completion moves the enquiry on, so a re-run cannot queue twice.
            changed = connection.execute(
                update(Enquiry)
                .where(
                    Enquiry.tenant_id == tenant_id,
                    Enquiry.id == enquiry_id,
                    Enquiry.status == EnquiryStatus.RECEIVED.value,
                )
                .values(status=status.value, updated_at=func.now())
            ).rowcount
            if changed and status is EnquiryStatus.READY_FOR_EXTRACTION:
                # Each reading is the next round (phase 4): a follow-up gets a fresh thread.
                last_round = connection.execute(
                    select(func.coalesce(func.max(WorkflowRun.round), 0)).where(
                        WorkflowRun.tenant_id == tenant_id,
                        WorkflowRun.enquiry_id == enquiry_id,
                        WorkflowRun.graph_version.like(EXTRACTION_GRAPHS),
                    )
                ).scalar_one()
                connection.execute(
                    insert(Job).values(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        kind=JobKind.EXTRACT_REQUIREMENTS.value,
                        payload={"enquiry_id": str(enquiry_id), "round": str(last_round + 1)},
                        status=JobStatus.PENDING.value,
                        max_attempts=MAX_JOB_ATTEMPTS,
                        run_at=func.now(),
                        correlation_id=correlation_id,
                    )
                )


PREVIEW_CHARACTERS = 160


class SqlEnquiryListReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list_enquiries(
        self, tenant_id: UUID, statuses: frozenset[EnquiryStatus] | None, limit: int
    ) -> list[EnquirySummary]:
        query = (
            select(
                Enquiry.id,
                Enquiry.channel,
                Enquiry.status,
                Enquiry.customer_reference,
                Enquiry.received_at,
                func.left(Enquiry.text, PREVIEW_CHARACTERS).label("preview"),
            )
            .where(Enquiry.tenant_id == tenant_id)
            .order_by(Enquiry.received_at.desc(), Enquiry.id)
            .limit(limit)
        )
        if statuses is not None:
            query = query.where(Enquiry.status.in_([status.value for status in statuses]))
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(query).all()
        return [
            EnquirySummary(
                enquiry_id=row.id,
                channel=row.channel,
                status=EnquiryStatus(row.status),
                customer_reference=row.customer_reference,
                received_at=row.received_at,
                preview=row.preview,
            )
            for row in rows
        ]
