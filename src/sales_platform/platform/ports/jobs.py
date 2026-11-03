"""Durable job queue contract used by workers.

Fencing: every claim gets a fresh lease token. complete/retry/fail only succeed while that
token is still current, so a worker whose lease expired cannot overwrite a newer attempt.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from sales_platform.platform.domain.jobs import JobKind, PermanentJobError


class LeaseLost(Exception):
    """The job was reclaimed by another worker after this worker's lease expired."""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: UUID
    tenant_id: UUID | None  # None only for send_account_email (sign-up and reset emails)
    kind: JobKind
    payload: dict[str, str]
    attempt: int
    max_attempts: int
    lease_token: UUID
    correlation_id: UUID

    def require_tenant(self) -> UUID:
        """The business this job works for; every kind except account emails has one."""
        if self.tenant_id is None:
            raise PermanentJobError(f"A {self.kind} job needs a business")
        return self.tenant_id


class JobQueue(Protocol):
    def claim(self, lease: timedelta) -> ClaimedJob | None: ...

    def complete(self, job: ClaimedJob) -> None: ...

    def retry(self, job: ClaimedJob, delay: timedelta, error: str) -> None: ...

    def fail(self, job: ClaimedJob, error: str) -> None:
        """Mark dead: visible for an operator; never retried automatically."""
        ...
