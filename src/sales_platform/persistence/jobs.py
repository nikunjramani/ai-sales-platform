"""PostgreSQL job queue for the worker role.

Claiming uses FOR UPDATE SKIP LOCKED: concurrent workers each lock a different row instead of
waiting or taking the same job. Times come from the database clock so all workers agree.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, func, insert, text

from sales_platform.domain.jobs import JobKind, JobStatus
from sales_platform.persistence.models import Job
from sales_platform.ports.jobs import ClaimedJob, LeaseLost

MAX_JOB_ATTEMPTS = 5

_EXPIRE_FINAL_ATTEMPTS = text(
    "UPDATE jobs SET status = 'dead', lease_token = NULL, updated_at = now(), "
    "last_error = 'Lease expired after final attempt' "
    "WHERE status = 'running' AND lease_expires_at < now() AND attempts >= max_attempts"
)

_CLAIM = text(
    "UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_token = :token, "
    "lease_expires_at = now() + make_interval(secs => :lease_seconds), updated_at = now() "
    "WHERE id = ("
    "  SELECT id FROM jobs"
    "  WHERE (status = 'pending' AND run_at <= now())"
    "     OR (status = 'running' AND lease_expires_at < now())"
    "  ORDER BY run_at, created_at"
    "  FOR UPDATE SKIP LOCKED LIMIT 1"
    ") "
    "RETURNING id, tenant_id, kind, payload, attempts, max_attempts, correlation_id"
)


def queue_job(
    connection: Connection,
    tenant_id: UUID | None,
    kind: JobKind,
    payload: Mapping[str, str],
    correlation_id: UUID,
    run_at: datetime | None = None,
) -> None:
    """Queue a job inside the caller's transaction, so it exists only if the change commits.

    run_at delays it (e.g. the expiry of a quotation); by default it can run at once.
    tenant_id is None only for account emails (send_account_email), which the database enforces.
    """
    connection.execute(
        insert(Job).values(
            id=uuid4(),
            tenant_id=tenant_id,
            kind=kind.value,
            payload=dict(payload),
            status=JobStatus.PENDING.value,
            max_attempts=MAX_JOB_ATTEMPTS,
            run_at=func.now() if run_at is None else run_at,
            correlation_id=correlation_id,
        )
    )


class SqlJobQueue:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(self, lease: timedelta) -> ClaimedJob | None:
        token = uuid4()
        with self._engine.begin() as connection:
            connection.execute(_EXPIRE_FINAL_ATTEMPTS)
            row = connection.execute(
                _CLAIM, {"token": token, "lease_seconds": lease.total_seconds()}
            ).one_or_none()
        if row is None:
            return None
        return ClaimedJob(
            job_id=row.id,
            tenant_id=row.tenant_id,
            kind=JobKind(row.kind),
            payload={str(key): str(value) for key, value in row.payload.items()},
            attempt=row.attempts,
            max_attempts=row.max_attempts,
            lease_token=token,
            correlation_id=row.correlation_id,
        )

    def complete(self, job: ClaimedJob) -> None:
        self._finish(job, "succeeded", None, None)

    def retry(self, job: ClaimedJob, delay: timedelta, error: str) -> None:
        self._finish(job, "pending", delay, error)

    def fail(self, job: ClaimedJob, error: str) -> None:
        self._finish(job, "dead", None, error)

    def _finish(
        self, job: ClaimedJob, status: str, delay: timedelta | None, error: str | None
    ) -> None:
        # Fenced: only the holder of the current lease token may change the job.
        with self._engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE jobs SET status = :status, lease_token = NULL, "
                    "lease_expires_at = NULL, last_error = :error, updated_at = now(), "
                    # Explicit cast: PostgreSQL cannot infer the type of a NULL parameter.
                    "run_at = COALESCE(now() + make_interval("
                    "secs => CAST(:delay AS double precision)), run_at) "
                    "WHERE id = :id AND lease_token = :token AND status = 'running'"
                ),
                {
                    "status": status,
                    "error": None if error is None else error[:500],
                    "delay": None if delay is None else delay.total_seconds(),
                    "id": job.job_id,
                    "token": job.lease_token,
                },
            )
            if result.rowcount != 1:
                raise LeaseLost(f"Job {job.job_id} lease is no longer held")


def job_status(engine: Engine, job_id: UUID) -> tuple[str, int, str | None] | None:
    """Worker-role helper for diagnostics and tests: (status, attempts, last_error)."""
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, attempts, last_error FROM jobs WHERE id = :id"), {"id": job_id}
        ).one_or_none()
    return None if row is None else (row.status, row.attempts, row.last_error)
