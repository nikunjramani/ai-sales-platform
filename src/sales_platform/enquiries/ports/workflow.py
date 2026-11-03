"""Business-record access for the enquiry workflow (worker role, always tenant-scoped).

Workflow runs map a tenant's enquiry to a LangGraph thread. Checkpoints are only ever opened
through this mapping, so a thread ID from one tenant is never resumed for another.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sales_platform.enquiries.domain.requirements import ExtractionOutcome
from sales_platform.enquiries.ports.interpretation import EnquiryTranscript


@dataclass(frozen=True)
class CleanAttachment:
    attachment_id: UUID
    filename: str
    content_type: str
    storage_key: str


@dataclass(frozen=True)
class EnquiryContent:
    transcript: EnquiryTranscript  # every customer message, plus the previous round's questions
    attachments: tuple[CleanAttachment, ...]  # only attachments that passed inspection
    last_message_seq: int = 0  # the newest message read; later ones trigger another round


@dataclass(frozen=True)
class ReadingCap:
    """Daily reading budget for enquiries from some tools (hosted chat, user answer Q9)."""

    provider_keys: frozenset[str]
    per_business_day: int


class WorkflowStore(Protocol):
    def readings_today(
        self, tenant_id: UUID, enquiry_id: UUID, run_id: UUID, provider_keys: frozenset[str]
    ) -> int | None:
        """How many other readings of the business's enquiries from these tools started today
        (UTC), or None if this enquiry did not come from one of them."""
        ...

    def enquiry_content(self, tenant_id: UUID, enquiry_id: UUID) -> EnquiryContent | None: ...

    def start_run(
        self, tenant_id: UUID, enquiry_id: UUID, graph_version: str, round: int = 1
    ) -> UUID:
        """Return the run (thread) ID for this enquiry, graph version and round, creating it once.

        Each reading of an enquiry is one round (phase 4): a follow-up message starts the next
        round on a fresh thread, and earlier rounds stay as history."""
        ...

    def record_outcome(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        run_id: UUID,
        outcome: ExtractionOutcome,
        details: Mapping[str, object],
    ) -> bool:
        """Store the result and move the enquiry to the matching status. Safe to repeat.

        Returns True when the enquiry has this outcome (now or from an earlier recording),
        False when the result is history only (superseded, see below).

        If the customer wrote again after this round read the messages (details
        "message_seq_upto"), or a file is still in quarantine, the result stays as history
        only: the enquiry goes back to received and is prepared and read again (Q3)."""
        ...
