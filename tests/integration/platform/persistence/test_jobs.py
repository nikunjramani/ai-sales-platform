"""Job queue behaviour on PostgreSQL: leases, crash recovery, fencing, retries, isolation."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from sales_platform.platform.persistence.jobs import SqlJobQueue, job_status
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.platform.ports.jobs import LeaseLost

LEASE = timedelta(seconds=30)


def enqueue(owner_engine: Engine, tenant_id: UUID, max_attempts: int = 3) -> UUID:
    job_id = uuid4()
    with tenant_transaction(owner_engine, tenant_id) as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (id, tenant_id, kind, payload, status, max_attempts, run_at, "
                "correlation_id) VALUES (:id, :tenant, 'prepare_enquiry', '{}', 'pending', "
                ":max_attempts, now(), :correlation)"
            ),
            {
                "id": job_id,
                "tenant": tenant_id,
                "max_attempts": max_attempts,
                "correlation": uuid4(),
            },
        )
    return job_id


def expire_lease(worker_engine: Engine, job_id: UUID) -> None:
    """Simulate a crashed worker: its lease runs out without completion."""
    with worker_engine.begin() as connection:
        connection.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )


def test_worker_claims_across_tenants_one_job_each(
    owner_engine: Engine, worker_engine: Engine, industrial_tenant: UUID, office_tenant: UUID
) -> None:
    first = enqueue(owner_engine, industrial_tenant)
    second = enqueue(owner_engine, office_tenant)
    queue = SqlJobQueue(worker_engine)
    claimed = [queue.claim(LEASE), queue.claim(LEASE)]
    assert {job.job_id for job in claimed if job} == {first, second}
    assert queue.claim(LEASE) is None  # both are leased; nothing else is available


def test_crashed_worker_job_is_reclaimed_and_stale_result_rejected(
    owner_engine: Engine, worker_engine: Engine, industrial_tenant: UUID
) -> None:
    job_id = enqueue(owner_engine, industrial_tenant)
    queue = SqlJobQueue(worker_engine)
    crashed = queue.claim(LEASE)
    assert crashed is not None and crashed.attempt == 1
    expire_lease(worker_engine, job_id)

    recovered = queue.claim(LEASE)
    assert recovered is not None and recovered.job_id == job_id and recovered.attempt == 2
    with pytest.raises(LeaseLost):
        queue.complete(crashed)  # the old worker wakes up too late
    queue.complete(recovered)
    assert job_status(worker_engine, job_id) == ("succeeded", 2, None)


def test_retry_waits_for_backoff_then_runs_again(
    owner_engine: Engine, worker_engine: Engine, industrial_tenant: UUID
) -> None:
    job_id = enqueue(owner_engine, industrial_tenant)
    queue = SqlJobQueue(worker_engine)
    job = queue.claim(LEASE)
    assert job is not None
    queue.retry(job, timedelta(hours=1), "ConnectionError")
    assert job_status(worker_engine, job_id) == ("pending", 1, "ConnectionError")
    assert queue.claim(LEASE) is None  # not due yet
    with worker_engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET run_at = now() WHERE id = :id"), {"id": job_id})
    again = queue.claim(LEASE)
    assert again is not None and again.attempt == 2


def test_expired_final_attempt_becomes_dead(
    owner_engine: Engine, worker_engine: Engine, industrial_tenant: UUID
) -> None:
    job_id = enqueue(owner_engine, industrial_tenant, max_attempts=1)
    queue = SqlJobQueue(worker_engine)
    assert queue.claim(LEASE) is not None
    expire_lease(worker_engine, job_id)
    assert queue.claim(LEASE) is None
    status, attempts, error = job_status(worker_engine, job_id) or ("", 0, None)
    assert (status, attempts) == ("dead", 1) and error and "final attempt" in error


def test_worker_role_sees_only_the_queue_across_tenants(
    owner_engine: Engine, worker_engine: Engine, runtime_engine: Engine, industrial_tenant: UUID
) -> None:
    enqueue(owner_engine, industrial_tenant)
    with tenant_transaction(owner_engine, industrial_tenant) as connection:
        connection.execute(
            text(
                "INSERT INTO inbox_events (id, tenant_id, channel, connection_id, "
                "connection_ref, provider_event_id, payload_sha256, envelope, received_at, "
                "correlation_id) SELECT gen_random_uuid(), :tenant, 'simulated', 'c', id, "
                "'event-000001', :hash, '{}', now(), gen_random_uuid() FROM connections "
                "WHERE provider_key = 'simulated'"
            ),
            {"tenant": industrial_tenant, "hash": "0" * 64},
        )
    with worker_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM inbox_events")).scalar_one() == 0
    with tenant_transaction(worker_engine, industrial_tenant) as connection:
        assert connection.execute(text("SELECT count(*) FROM inbox_events")).scalar_one() == 1
    # Since M4 the worker may read memberships (to notify current approvers), but only
    # within a tenant context; it can still never change them or delete jobs.
    with worker_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM memberships")).scalar_one() == 0
    for statement in ("DELETE FROM memberships", "DELETE FROM jobs", "SELECT * FROM api_keys"):
        with pytest.raises(ProgrammingError), worker_engine.begin() as connection:
            connection.execute(text(statement))
