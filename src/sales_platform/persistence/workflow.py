"""Workflow runs (tenant-scoped) and the LangGraph PostgreSQL checkpointer (worker role)."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from sqlalchemy import Connection as SqlConnection
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from sales_platform.domain.attachments import AttachmentStatus
from sales_platform.domain.conversations import Author, Direction
from sales_platform.domain.intake import EnquiryStatus
from sales_platform.domain.jobs import JobKind
from sales_platform.domain.requirements import ExtractionOutcome
from sales_platform.persistence.conversations import EXTRACTION_GRAPHS, lock_conversation_of
from sales_platform.persistence.jobs import queue_job
from sales_platform.persistence.models import (
    Attachment,
    ConversationMessage,
    Enquiry,
    WorkflowRun,
)
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.interpretation import CustomerText, EnquiryTranscript, OpenItem
from sales_platform.ports.workflow import CleanAttachment, EnquiryContent

RUNNING = "running"


class SqlWorkflowStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enquiry_content(self, tenant_id: UUID, enquiry_id: UUID) -> EnquiryContent | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            found = connection.execute(
                select(Enquiry.id).where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
            ).scalar_one_or_none()
            if found is None:
                return None
            inbound = connection.execute(
                select(
                    ConversationMessage.seq, ConversationMessage.text, ConversationMessage.author
                )
                .where(
                    ConversationMessage.tenant_id == tenant_id,
                    ConversationMessage.enquiry_id == enquiry_id,
                    ConversationMessage.direction == Direction.INBOUND.value,
                )
                .order_by(ConversationMessage.seq)
            ).all()
            # Phase 5: what the business typed itself on its tool is context from our side,
            # never the customer's words (and never a source for verbatim checks).
            messages = [row for row in inbound if row.author == Author.CUSTOMER.value]
            staff = [row.text for row in inbound if row.author == Author.STAFF.value]
            previous = connection.execute(
                select(WorkflowRun.outcome)
                .where(
                    WorkflowRun.tenant_id == tenant_id,
                    WorkflowRun.enquiry_id == enquiry_id,
                    WorkflowRun.graph_version.like(EXTRACTION_GRAPHS),
                    WorkflowRun.status == EnquiryStatus.NEEDS_CLARIFICATION.value,
                )
                .order_by(WorkflowRun.round.desc(), WorkflowRun.updated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            rows = connection.execute(
                select(
                    Attachment.id,
                    Attachment.filename,
                    Attachment.declared_content_type,
                    Attachment.storage_key,
                )
                .where(
                    Attachment.tenant_id == tenant_id,
                    Attachment.enquiry_id == enquiry_id,
                    Attachment.status == AttachmentStatus.CLEAN.value,
                )
                .order_by(Attachment.created_at, Attachment.id)
            ).all()
        asked, open_items = _previous_round(previous)
        return EnquiryContent(
            EnquiryTranscript(
                tuple(CustomerText(index, row.text) for index, row in enumerate(messages, start=1)),
                (*asked, *(f"(The business wrote to the customer: {text})" for text in staff)),
                open_items,
            ),
            tuple(
                CleanAttachment(row.id, row.filename, row.declared_content_type, row.storage_key)
                for row in rows
            ),
            max((row.seq for row in messages), default=0),
        )

    def readings_today(
        self, tenant_id: UUID, enquiry_id: UUID, run_id: UUID, provider_keys: frozenset[str]
    ) -> int | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            channel = connection.execute(
                select(Enquiry.channel).where(
                    Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id
                )
            ).scalar_one_or_none()
            if channel not in provider_keys:
                return None
            count: int = connection.execute(
                select(func.count())
                .select_from(WorkflowRun)
                .join(
                    Enquiry,
                    (Enquiry.tenant_id == WorkflowRun.tenant_id)
                    & (Enquiry.id == WorkflowRun.enquiry_id),
                )
                .where(
                    WorkflowRun.tenant_id == tenant_id,
                    WorkflowRun.graph_version.like(EXTRACTION_GRAPHS),
                    WorkflowRun.id != run_id,
                    WorkflowRun.created_at >= func.date_trunc("day", func.now(), "UTC"),
                    Enquiry.channel.in_(sorted(provider_keys)),
                    # Readings the cap itself stopped did not use the model.
                    func.coalesce(WorkflowRun.outcome["reading_limit"].astext, "") != "true",
                )
            ).scalar_one()
        return count

    def start_run(
        self, tenant_id: UUID, enquiry_id: UUID, graph_version: str, round: int = 1
    ) -> UUID:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                insert(WorkflowRun)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    enquiry_id=enquiry_id,
                    graph_version=graph_version,
                    round=round,
                    status=RUNNING,
                )
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "enquiry_id", "graph_version", "round"]
                )
            )
            run_id: UUID = connection.execute(
                select(WorkflowRun.id).where(
                    WorkflowRun.tenant_id == tenant_id,
                    WorkflowRun.enquiry_id == enquiry_id,
                    WorkflowRun.graph_version == graph_version,
                    WorkflowRun.round == round,
                )
            ).scalar_one()
        return run_id

    def record_outcome(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        run_id: UUID,
        outcome: ExtractionOutcome,
        details: Mapping[str, object],
    ) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            lock_conversation_of(connection, tenant_id, enquiry_id)
            enquiry = connection.execute(
                select(Enquiry.status, Enquiry.correlation_id)
                .where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
                .with_for_update()
            ).one()
            connection.execute(
                update(WorkflowRun)
                .where(WorkflowRun.tenant_id == tenant_id, WorkflowRun.id == run_id)
                .values(status=outcome.value, outcome=dict(details), updated_at=func.now())
            )
            if enquiry.status != EnquiryStatus.READY_FOR_EXTRACTION.value:
                # A repeat after the first recording moved it on.
                return bool(enquiry.status == outcome.value)
            if _changed_since_read(connection, tenant_id, enquiry_id, details):
                # The customer wrote again while this round was reading (Q3): this result is
                # history only; the enquiry is prepared and read again with every message.
                connection.execute(
                    update(Enquiry)
                    .where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
                    .values(status=EnquiryStatus.RECEIVED.value, updated_at=func.now())
                )
                queue_job(
                    connection,
                    tenant_id,
                    JobKind.PREPARE_ENQUIRY,
                    {"enquiry_id": str(enquiry_id)},
                    enquiry.correlation_id,
                )
                return False
            connection.execute(
                update(Enquiry)
                .where(Enquiry.tenant_id == tenant_id, Enquiry.id == enquiry_id)
                .values(status=EnquiryStatus(outcome.value).value, updated_at=func.now())
            )
            # Only the first recording moves the enquiry on, so drafting is queued once (M4).
            if outcome is ExtractionOutcome.READY_FOR_QUOTATION:
                queue_job(
                    connection,
                    tenant_id,
                    JobKind.DRAFT_QUOTATION,
                    {"enquiry_id": str(enquiry_id)},
                    enquiry.correlation_id,
                )
        return True


def _changed_since_read(
    connection: SqlConnection, tenant_id: UUID, enquiry_id: UUID, details: Mapping[str, object]
) -> bool:
    """A newer customer message than the round read, or a file not yet inspected."""
    upto = details.get("message_seq_upto")
    newer = connection.execute(
        select(func.count())
        .select_from(ConversationMessage)
        .where(
            ConversationMessage.tenant_id == tenant_id,
            ConversationMessage.enquiry_id == enquiry_id,
            ConversationMessage.direction == Direction.INBOUND.value,
            ConversationMessage.author == Author.CUSTOMER.value,
            ConversationMessage.seq > (upto if isinstance(upto, int) else 2**62),
        )
    ).scalar_one()
    quarantined = connection.execute(
        select(func.count())
        .select_from(Attachment)
        .where(
            Attachment.tenant_id == tenant_id,
            Attachment.enquiry_id == enquiry_id,
            Attachment.status == AttachmentStatus.QUARANTINED.value,
        )
    ).scalar_one()
    return bool(newer or quarantined)


def _previous_round(
    outcome: Mapping[str, Any] | None,
) -> tuple[tuple[str, ...], tuple[OpenItem, ...]]:
    """Our questions of the last round that asked, and the items it matched but could not
    turn into a line (missing or wrong quantity/unit): a bare answer may complete them."""
    if outcome is None:
        return (), ()
    priced = {str(line["sku"]) for line in outcome.get("lines", [])}
    items = outcome.get("items", [])
    open_items = []
    for match in outcome.get("matches", []):
        sku = match.get("sku")
        index = int(match["item_index"])
        if match.get("status") != "matched" or sku is None or sku in priced:
            continue
        if 0 <= index < len(items):
            item = items[index]
            open_items.append(
                OpenItem(str(item["source_quote"]), str(item["product_text"]), str(sku))
            )
    return tuple(str(q) for q in outcome.get("questions", [])), tuple(open_items)


def psycopg_url(sqlalchemy_url: str) -> str:
    """Convert postgresql+psycopg://... to the libpq form psycopg expects."""
    return (
        make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


@contextmanager
def open_checkpointer(worker_url: str) -> Iterator[PostgresSaver]:
    """A dedicated worker-role connection for LangGraph checkpoints (autocommit, as required).

    The schema already exists (migration 0004), so setup() is never called at runtime.
    """
    connection: Connection[DictRow]
    with Connection.connect(
        psycopg_url(worker_url),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        connect_timeout=3,
    ) as connection:
        yield PostgresSaver(connection)
