from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sales_platform.application.catalogue_sync import (
    CatalogueSyncHandler,
    CatalogueSyncService,
    SyncNotAvailable,
)
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.catalogue_sync import (
    SYNC_MINUTES_KEY,
    CatalogueFeed,
    FeedIssue,
    FeedProduct,
    SyncCounts,
    SyncRun,
    SyncTrigger,
)
from sales_platform.domain.connections import (
    Capabilities,
    CatalogueCapabilities,
    Connection,
    ConnectionStatus,
    Purpose,
)
from sales_platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.domain.tenancy import PermissionDenied, Role, TenantContext
from sales_platform.ports.erp import ErpError, ErpErrorCode
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.ports.providers import (
    OperationUnsupported,
    ProviderDefaults,
    RenderedMessage,
    SendReceipt,
    TestResult,
)

NOW = datetime(2026, 10, 29, tzinfo=UTC)
TENANT = uuid4()


class CopyingSource(ProviderDefaults):
    key = "copying_source"
    display_name = "Copying source"
    purposes = frozenset({Purpose.CATALOGUE})
    capabilities = Capabilities(catalogue=CatalogueCapabilities(sync=True))

    def __init__(self) -> None:
        self.error: Exception | None = None

    def test(self, conn: Connection, secrets: object) -> TestResult:
        return TestResult(ok=True)

    def send(self, conn: Connection, secrets: object, message: RenderedMessage) -> SendReceipt:
        raise OperationUnsupported("A product source sends no messages")

    def fetch_catalogue(self, conn: Connection, secrets: object) -> CatalogueFeed:
        if self.error is not None:
            raise self.error
        return CatalogueFeed((FeedProduct("PUMP-100", "Pump", "each", "PUMP-100"),))


class LiveOnlySource(CopyingSource):
    key = "live_only"
    capabilities = Capabilities(catalogue=CatalogueCapabilities(live_prices=True))


@dataclass
class Connections:
    rows: list[Connection] = field(default_factory=list)

    def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
        return [row for row in self.rows if row.active and row.serves(Purpose.CATALOGUE)]


@dataclass
class Store:
    started: list[tuple[UUID, SyncTrigger]] = field(default_factory=list)
    applied: list[tuple[CatalogueFeed, datetime | None]] = field(default_factory=list)
    failed: list[tuple[str, datetime | None]] = field(default_factory=list)
    requested: int = 0

    def start(self, tenant_id: UUID, connection_id: UUID, trigger: SyncTrigger) -> UUID:
        self.started.append((connection_id, trigger))
        return uuid4()

    def apply(
        self,
        tenant_id: UUID,
        run_id: UUID,
        feed: CatalogueFeed,
        actor: str,
        next_run_at: datetime | None,
    ) -> SyncCounts:
        self.applied.append((feed, next_run_at))
        return SyncCounts(created=len(feed.products))

    def fail(
        self,
        tenant_id: UUID,
        run_id: UUID,
        error: str,
        issues: Sequence[FeedIssue],
        next_run_at: datetime | None,
    ) -> None:
        self.failed.append((error, next_run_at))

    def latest(self, tenant_id: UUID, connection_id: UUID) -> SyncRun | None:
        return None

    def request(self, tenant_id: UUID, connection_id: UUID, actor: str) -> bool:
        self.requested += 1
        return self.requested == 1


def _connection(provider_key: str, minutes: object = 30, active: bool = True) -> Connection:
    return Connection(
        uuid4(), TENANT, provider_key, "Source", frozenset({Purpose.CATALOGUE}),
        {SYNC_MINUTES_KEY: minutes}, {}, None,
        ConnectionStatus.ACTIVE if active else ConnectionStatus.DISABLED, None, "t", NOW, NOW,
    )  # fmt: skip


def _job(connection: Connection, trigger: str = "schedule") -> ClaimedJob:
    payload = {"connection_id": str(connection.connection_id), "trigger": trigger}
    return ClaimedJob(uuid4(), TENANT, JobKind.SYNC_CATALOGUE, payload, 1, 5, uuid4(), uuid4())


@pytest.fixture
def source() -> CopyingSource:
    return CopyingSource()


def _handler(rows: list[Connection], store: Store, *providers: object) -> CatalogueSyncHandler:
    registry = ProviderRegistry(list(providers))  # type: ignore[arg-type]
    return CatalogueSyncHandler(Connections(rows), registry, object(), store, lambda: NOW)  # type: ignore[arg-type]


def test_a_scheduled_run_stores_the_feed_and_queues_the_next_one(source: CopyingSource) -> None:
    connection, store = _connection(source.key), Store()
    _handler([connection], store, source)(_job(connection))
    [(feed, next_run_at)] = store.applied
    assert [p.sku for p in feed.products] == ["PUMP-100"]
    assert next_run_at == NOW + timedelta(minutes=30)


def test_a_manual_run_does_not_repeat(source: CopyingSource) -> None:
    connection, store = _connection(source.key), Store()
    _handler([connection], store, source)(_job(connection, "manual"))
    assert store.applied[0][1] is None and store.started[0][1] is SyncTrigger.MANUAL


def test_a_failing_source_is_recorded_and_the_schedule_goes_on(source: CopyingSource) -> None:
    connection, store = _connection(source.key, minutes=15), Store()
    source.error = ErpError(ErpErrorCode.UNAUTHENTICATED, "ERPNext refused the API key")
    _handler([connection], store, source)(_job(connection))  # the job itself succeeds
    assert store.failed == [("ERPNext refused the API key", NOW + timedelta(minutes=15))]
    source.error = ValueError("The source offers the same SKU twice")
    _handler([connection], store, source)(_job(connection))
    assert store.failed[-1][0] == "The source offers the same SKU twice"
    assert store.applied == []  # the last good copy stays


def test_the_schedule_ends_when_the_source_was_replaced_or_disabled(source: CopyingSource) -> None:
    old, store = _connection(source.key), Store()
    replacement = _connection(source.key)
    _handler([replacement], store, source)(_job(old))
    _handler([_connection(source.key, active=False)], store, source)(_job(old))
    assert store.started == []


def test_a_live_only_source_is_not_copied() -> None:
    live = LiveOnlySource()
    connection, store = _connection(live.key), Store()
    _handler([connection], store, live)(_job(connection))
    assert store.started == []


def test_a_broken_payload_is_permanent(source: CopyingSource) -> None:
    job = ClaimedJob(uuid4(), TENANT, JobKind.SYNC_CATALOGUE, {}, 1, 5, uuid4(), uuid4())
    with pytest.raises(PermanentJobError):
        _handler([], Store(), source)(job)


def test_sync_now_needs_catalogue_manage_and_a_copied_source(source: CopyingSource) -> None:
    connection, store = _connection(source.key), Store()
    service = CatalogueSyncService(Connections([connection]), ProviderRegistry([source]), store)  # type: ignore[arg-type]
    operator = TenantContext(TENANT, "op", frozenset({Role.SALES_OPERATOR}))
    assert service.request(operator) is True
    assert service.request(operator) is False  # one already waits
    with pytest.raises(PermissionDenied):
        service.request(TenantContext(TENANT, "v", frozenset({Role.VIEWER})))
    status = service.status(TenantContext(TENANT, "v", frozenset({Role.VIEWER})))
    assert status is not None and status.every_minutes == 30
    empty = CatalogueSyncService(Connections([]), ProviderRegistry([source]), store)  # type: ignore[arg-type]
    assert empty.status(operator) is None
    with pytest.raises(SyncNotAvailable):
        empty.request(operator)
