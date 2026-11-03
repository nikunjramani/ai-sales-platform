"""Run one queued job: claim it, call its handler, then complete, retry or fail it.

Handlers must be idempotent: a crash after the handler's work but before `complete` means the
job runs again once its lease expires. Errors are recorded by type only, never with payloads.
"""

import logging
import random
from collections.abc import Callable, Mapping
from datetime import timedelta

from sales_platform.domain.jobs import JobKind, PermanentJobError, retry_delay
from sales_platform.domain.tenancy import TenantStatus
from sales_platform.ports.erp import ErpError
from sales_platform.ports.jobs import ClaimedJob, JobQueue, LeaseLost
from sales_platform.ports.tenancy import TenantStatusReader

type JobHandler = Callable[[ClaimedJob], None]

logger = logging.getLogger(__name__)


class BusinessSuspended(Exception):
    """The business is suspended: its jobs wait (and are retried) instead of running."""


class PauseWhileSuspended:
    """Wraps a job handler: a suspended business's jobs do nothing and are retried with backoff
    until the operator lifts the suspension or the job dies (phase 2, Q13)."""

    def __init__(self, handler: "JobHandler", statuses: TenantStatusReader) -> None:
        self._handler = handler
        self._statuses = statuses

    def __call__(self, job: ClaimedJob) -> None:
        if job.tenant_id is not None:
            if self._statuses.status(job.tenant_id) is TenantStatus.SUSPENDED:
                raise BusinessSuspended("The business is suspended")
        self._handler(job)


class JobRunner:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[JobKind, JobHandler],
        lease: timedelta = timedelta(seconds=60),
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._queue = queue
        self._handlers = dict(handlers)
        self._lease = lease
        self._jitter = jitter

    def run_once(self) -> bool:
        """Process at most one job. Returns False when no job was available."""
        job = self._queue.claim(self._lease)
        if job is None:
            return False
        try:
            outcome = self._run(job)
        except LeaseLost:
            # Another worker reclaimed the job after our lease expired; its result wins.
            outcome = "lease lost"
        logger.info(
            "job %s kind=%s attempt=%s/%s correlation=%s outcome=%s",
            job.job_id,
            job.kind,
            job.attempt,
            job.max_attempts,
            job.correlation_id,
            outcome,
        )
        return True

    def _run(self, job: ClaimedJob) -> str:
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._queue.fail(job, f"No handler for job kind {job.kind}")
            return "dead (no handler)"
        try:
            handler(job)
        except PermanentJobError as error:
            self._queue.fail(job, f"PermanentJobError: {error}")
            return "dead (permanent error)"
        except ErpError as error:
            if error.retryable:
                return self._retry(job, f"ErpError {error.code}")
            # Retrying cannot fix it (e.g. no product source, unauthenticated): stop now (P1-Q4).
            self._queue.fail(job, f"ErpError {error.code}: {error}"[:500])
            return "dead (ERP error, not retryable)"
        except Exception as error:  # noqa: BLE001 - any other failure is retried with backoff
            return self._retry(job, type(error).__name__)
        self._queue.complete(job)
        return "succeeded"

    def _retry(self, job: ClaimedJob, summary: str) -> str:
        if job.attempt >= job.max_attempts:
            self._queue.fail(job, f"{summary} after {job.attempt} attempts")
            return f"dead ({summary})"
        delay = retry_delay(job.attempt, jitter=self._jitter)
        self._queue.retry(job, delay, summary)
        return f"retry in {delay.total_seconds():.1f}s ({summary})"
