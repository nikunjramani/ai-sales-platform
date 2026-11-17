"""Job runner outcomes: success, retry with backoff, dead letters and lost leases."""

from datetime import timedelta
from uuid import uuid4

import pytest

from sales_platform.catalogue.ports.sources import CatalogueSourceMissing, ErpError, ErpErrorCode
from sales_platform.platform.application.jobs import JobRunner
from sales_platform.platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.platform.ports.jobs import ClaimedJob, LeaseLost


class FakeQueue:
    def __init__(self, job: ClaimedJob | None) -> None:
        self.job = job
        self.events: list[tuple[str, object]] = []
        self.lose_lease = False

    def claim(self, lease: timedelta) -> ClaimedJob | None:
        job, self.job = self.job, None
        return job

    def _record(self, event: str, detail: object) -> None:
        if self.lose_lease:
            raise LeaseLost("reclaimed")
        self.events.append((event, detail))

    def complete(self, job: ClaimedJob) -> None:
        self._record("complete", None)

    def retry(self, job: ClaimedJob, delay: timedelta, error: str) -> None:
        self._record("retry", (delay, error))

    def fail(self, job: ClaimedJob, error: str) -> None:
        self._record("fail", error)


def job(attempt: int = 1, max_attempts: int = 5) -> ClaimedJob:
    return ClaimedJob(
        uuid4(), uuid4(), JobKind.PREPARE_ENQUIRY, {}, attempt, max_attempts, uuid4(), uuid4()
    )


def runner(queue: FakeQueue, handler: object) -> JobRunner:
    return JobRunner(queue, {JobKind.PREPARE_ENQUIRY: handler}, jitter=lambda: 0.5)  # type: ignore[dict-item]


def failing(error: Exception) -> object:
    def handler(_: ClaimedJob) -> None:
        raise error

    return handler


def test_empty_queue_returns_false() -> None:
    assert runner(FakeQueue(None), lambda _: None).run_once() is False


def test_success_completes() -> None:
    queue = FakeQueue(job())
    assert runner(queue, lambda _: None).run_once() is True
    assert queue.events == [("complete", None)]


def test_transient_error_is_retried_with_backoff_and_type_only() -> None:
    queue = FakeQueue(job(attempt=2))
    runner(queue, failing(ConnectionError("secret customer text"))).run_once()
    assert queue.events == [("retry", (timedelta(seconds=5), "ConnectionError"))]


def test_last_attempt_goes_dead() -> None:
    queue = FakeQueue(job(attempt=5, max_attempts=5))
    runner(queue, failing(ConnectionError())).run_once()
    assert queue.events == [("fail", "ConnectionError after 5 attempts")]


def test_permanent_error_is_not_retried() -> None:
    queue = FakeQueue(job())
    runner(queue, failing(PermanentJobError("Enquiry not found"))).run_once()
    assert queue.events == [("fail", "PermanentJobError: Enquiry not found")]


def test_unknown_kind_goes_dead() -> None:
    queue = FakeQueue(job())
    JobRunner(queue, {}).run_once()
    assert queue.events[0][0] == "fail"


@pytest.mark.parametrize("handler", [lambda _: None, failing(ConnectionError())])
def test_lost_lease_is_tolerated(handler: object) -> None:
    queue = FakeQueue(job())
    queue.lose_lease = True
    assert runner(queue, handler).run_once() is True
    assert queue.events == []


def test_erp_errors_that_retrying_cannot_fix_stop_at_once() -> None:
    queue = FakeQueue(job())
    runner(queue, failing(CatalogueSourceMissing())).run_once()
    [(event, detail)] = queue.events
    assert event == "fail" and "No product source is connected" in str(detail)
    for code in (ErpErrorCode.UNAUTHENTICATED, ErpErrorCode.FORBIDDEN, ErpErrorCode.NOT_FOUND):
        queue = FakeQueue(job())
        runner(queue, failing(ErpError(code, "refused"))).run_once()
        assert queue.events[0][0] == "fail"


def test_temporary_erp_errors_are_retried() -> None:
    queue = FakeQueue(job())
    runner(queue, failing(ErpError(ErpErrorCode.RATE_LIMITED, "slow down"))).run_once()
    [(event, detail)] = queue.events
    assert event == "retry" and "rate_limited" in str(detail)


def test_a_suspended_business_s_jobs_wait() -> None:
    from uuid import uuid4

    from sales_platform.platform.application.jobs import BusinessSuspended, PauseWhileSuspended
    from sales_platform.platform.domain.tenancy import TenantStatus

    ran: list[str] = []
    statuses = {"suspended": TenantStatus.SUSPENDED}

    class Statuses:
        def status(self, tenant_id: object) -> TenantStatus:
            return statuses.get(str(tenant_id), TenantStatus.ACTIVE)

    def job(tenant: object) -> ClaimedJob:
        return ClaimedJob(uuid4(), tenant, JobKind.SEND_ACCOUNT_EMAIL, {}, 1, 5, uuid4(), uuid4())  # type: ignore[arg-type]

    gate = PauseWhileSuspended(lambda claimed: ran.append(str(claimed.tenant_id)), Statuses())
    gate(job(None))  # account emails of people, not of a business
    gate(job("live"))
    with pytest.raises(BusinessSuspended):
        gate(job("suspended"))
    assert ran == ["None", "live"]
