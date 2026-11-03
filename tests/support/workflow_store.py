"""Test double: the enquiry workflow's store in memory (moved from src, ADR 0026 step 2)."""

from collections.abc import Mapping
from uuid import UUID, uuid4

from sales_platform.enquiries.domain.requirements import ExtractionOutcome
from sales_platform.enquiries.ports.workflow import EnquiryContent


class InMemoryWorkflowStore:
    """WorkflowStore in memory, for the enquiry workflow unit tests."""

    def __init__(self) -> None:
        self.contents: dict[tuple[UUID, UUID], EnquiryContent] = {}
        self.runs: dict[tuple[UUID, UUID, str, int], UUID] = {}
        self.outcomes: dict[UUID, tuple[ExtractionOutcome, Mapping[str, object]]] = {}

    def enquiry_content(self, tenant_id: UUID, enquiry_id: UUID) -> EnquiryContent | None:
        return self.contents.get((tenant_id, enquiry_id))

    def readings_today(
        self, tenant_id: UUID, enquiry_id: UUID, run_id: UUID, provider_keys: frozenset[str]
    ) -> int | None:
        return None

    def start_run(
        self, tenant_id: UUID, enquiry_id: UUID, graph_version: str, round: int = 1
    ) -> UUID:
        return self.runs.setdefault((tenant_id, enquiry_id, graph_version, round), uuid4())

    def record_outcome(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        run_id: UUID,
        outcome: ExtractionOutcome,
        details: Mapping[str, object],
    ) -> bool:
        self.outcomes[enquiry_id] = (outcome, details)
        return True
