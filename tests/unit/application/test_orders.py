"""Orders over in-memory fakes: creation rules, the ERP write (intent before call), chaos
cases (timeout after commit, worker stopped while sending), reconciliation, staff resolution,
ERP status refresh, customer notices and "track my order". The PostgreSQL store is tested in
tests/integration/persistence/test_orders.py.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.adapters.erp.simulated_orders import SimulatedOrderWriter
from sales_platform.application.jobs import JobRunner
from sales_platform.application.orders import (
    CreateOrderHandler,
    Destination,
    ErpOrderOperationHandler,
    OrderNotificationHandler,
    OrderService,
    OrderStatusRoute,
    ReconcileOrderOperationHandler,
    RefreshOrderStatusHandler,
)
from sales_platform.domain.catalogue import StockLevel
from sales_platform.domain.connections import (
    Capabilities,
    Connection,
    ConnectionStatus,
    OrderCapabilities,
    Purpose,
)
from sales_platform.domain.intake import OrderStatusRequest
from sales_platform.domain.jobs import JobKind
from sales_platform.domain.messages import TemplateKey
from sales_platform.domain.orders import (
    RECONCILE_WINDOW,
    EventSource,
    Fulfilment,
    OperationStatus,
    Order,
    OrderCreation,
    OrderEvent,
    OrderEventType,
    OrderNotAllowed,
    OrderOperation,
    OrderStatus,
    Resolution,
    order_number,
)
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.ports.erp import ErpError, ErpErrorCode, ErpOrderState
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.ports.orders import (
    AcceptedQuotation,
    NewOrder,
    OperationOutcome,
    OrderExists,
    OrderFilter,
    OrderListItem,
    StatusChange,
)

NOW = datetime(2026, 10, 29, 10, tzinfo=UTC)
TENANT = uuid4()
CONNECTION = uuid4()


def _content() -> dict[str, object]:
    return {
        "currency": "INR",
        "subtotal": "8500.00",
        "tax": "1530.00",
        "total": "10030.00",
        "lines": [
            {"line_index": 0, "sku": "PUMP-100", "description": "Pump", "quantity": "2",
             "unit": "each", "unit_price": "4250.00", "line_total": "8500.00"},
        ],
    }  # fmt: skip


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class MemoryOrders:
    """Implements ports.orders.OrderStore in memory; jobs are recorded, not run."""

    clock: Clock
    creation: OrderCreation = OrderCreation.STAFF
    quotations: dict[UUID, AcceptedQuotation] = field(default_factory=dict)
    orders: dict[UUID, Order] = field(default_factory=dict)
    events_: list[OrderEvent] = field(default_factory=list)
    operations_: dict[UUID, OrderOperation] = field(default_factory=dict)
    jobs: list[tuple[JobKind, dict[str, str], datetime | None]] = field(default_factory=list)
    contacts: dict[UUID, UUID] = field(default_factory=dict)  # enquiry -> contact
    identities: dict[tuple[UUID, str], UUID] = field(default_factory=dict)

    def order_creation(self, tenant_id: UUID) -> OrderCreation:
        return self.creation

    def set_order_creation(self, tenant_id: UUID, value: OrderCreation, actor: str) -> None:
        self.creation = value

    def accepted_quotation(self, tenant_id: UUID, quotation_id: UUID) -> AcceptedQuotation | None:
        return self.quotations.get(quotation_id)

    def create(self, tenant_id: UUID, new: NewOrder) -> Order:
        existing = self.for_quotation(tenant_id, new.quotation.quotation_id)
        if existing is not None:
            raise OrderExists(existing.order_id)
        order = Order(
            new.order_id, tenant_id, order_number(new.order_id), new.quotation.quotation_id,
            new.quotation.version, new.quotation.content_sha256, new.quotation.response_id,
            new.quotation.enquiry_id, self.contacts.get(new.quotation.enquiry_id), new.status,
            new.fulfilment, new.erp_connection_id, new.erp_customer, None, new.currency,
            new.subtotal, new.tax, new.total, new.lines, new.actor, self.clock(), self.clock(),
            new.hold_reason,
        )  # fmt: skip
        self.orders[order.order_id] = order
        self._event(order.order_id, OrderEventType.CREATED, None, OrderStatus.PLACED, new.source)
        if new.status is OrderStatus.ON_HOLD:
            self._event(order.order_id, OrderEventType.HELD, OrderStatus.PLACED, new.status)
        if new.external_reference is not None:
            assert new.erp_connection_id is not None
            operation = OrderOperation(
                uuid4(), order.order_id, new.external_reference, new.erp_connection_id,
                OperationStatus.PENDING, None, 0, None, self.clock(), self.clock(),
            )  # fmt: skip
            self.operations_[operation.operation_id] = operation
            if new.send_now:
                self._queue_operation(operation.operation_id)
        return order

    def get(self, tenant_id: UUID, order_id: UUID) -> Order | None:
        return self.orders.get(order_id)

    def for_quotation(self, tenant_id: UUID, quotation_id: UUID) -> Order | None:
        return next((o for o in self.orders.values() if o.quotation_id == quotation_id), None)

    def for_enquiry(self, tenant_id: UUID, enquiry_id: UUID) -> Order | None:
        return next((o for o in self.orders.values() if o.enquiry_id == enquiry_id), None)

    def list(self, tenant_id: UUID, query: OrderFilter) -> Sequence[OrderListItem]:
        raise NotImplementedError

    def events(self, tenant_id: UUID, order_id: UUID) -> Sequence[OrderEvent]:
        return [event for event in self.events_ if event.order_id == order_id]

    def event(self, tenant_id: UUID, event_id: UUID) -> OrderEvent | None:
        return next((e for e in self.events_ if e.event_id == event_id), None)

    def operations(self, tenant_id: UUID, order_id: UUID) -> Sequence[OrderOperation]:
        return [op for op in self.operations_.values() if op.order_id == order_id]

    def operation(self, tenant_id: UUID, operation_id: UUID) -> OrderOperation | None:
        return self.operations_.get(operation_id)

    def for_identity(
        self, tenant_id: UUID, connection_id: UUID, external_id: str
    ) -> Sequence[Order]:
        contact = self.identities.get((connection_id, external_id))
        return [o for o in self.orders.values() if contact and o.contact_id == contact]

    def open_erp_orders(self, tenant_id: UUID, connection_id: UUID) -> Sequence[Order]:
        return [
            o
            for o in self.orders.values()
            if o.erp_connection_id == connection_id and o.erp_order_id and o.open
        ]

    def change_status(self, tenant_id: UUID, change: StatusChange) -> Order | None:
        order = self.orders[change.order_id]
        if order.status is not change.expected:
            return None
        updated = replace(order, status=change.target, hold_reason=change.hold_reason)
        if change.erp_customer is not None:
            updated = replace(updated, erp_customer=change.erp_customer)
        self.orders[order.order_id] = updated
        event = self._event(
            order.order_id, change.event_type, change.expected, change.target, change.source
        )
        if change.notify_customer:
            self.jobs.append((JobKind.NOTIFY_ORDER, {"event_id": str(event.event_id)}, None))
        if change.send_operation:
            for operation in self.operations(tenant_id, order.order_id):
                if operation.status is OperationStatus.PENDING:
                    self._queue_operation(operation.operation_id)
        return updated

    def begin_send(self, tenant_id: UUID, operation_id: UUID) -> bool:
        operation = self.operations_[operation_id]
        if operation.status is not OperationStatus.PENDING:
            return False
        self.operations_[operation_id] = replace(
            operation, status=OperationStatus.SENDING, attempts=operation.attempts + 1
        )
        return True

    def finish_operation(
        self,
        tenant_id: UUID,
        operation_id: UUID,
        expected: frozenset[OperationStatus],
        outcome: OperationOutcome,
    ) -> bool:
        operation = self.operations_[operation_id]
        if operation.status not in expected:
            return False
        unknown_since = operation.unknown_since
        if outcome.status is OperationStatus.UNKNOWN and unknown_since is None:
            unknown_since = self.clock()
        self.operations_[operation_id] = replace(
            operation,
            status=outcome.status,
            erp_order_id=outcome.erp_order_id or operation.erp_order_id,
            last_error=outcome.error,
            unknown_since=unknown_since,
            updated_at=self.clock(),
        )
        order = self.orders[operation.order_id]
        target = outcome.order_status
        moved = target is not None and target is not order.status
        self.orders[order.order_id] = replace(
            order,
            erp_order_id=outcome.erp_order_id or order.erp_order_id,
            status=target if moved and target is not None else order.status,
        )
        if outcome.event_type is not None:
            event = self._event(
                order.order_id,
                outcome.event_type,
                order.status if moved else None,
                target if moved else None,
            )
            if moved and outcome.notify_customer:
                self.jobs.append((JobKind.NOTIFY_ORDER, {"event_id": str(event.event_id)}, None))
        if outcome.send_again:
            self._queue_operation(operation_id)
        if outcome.reconcile_at is not None:
            payload = {"operation_id": str(operation_id)}
            self.jobs.append((JobKind.RECONCILE_ORDER_OPERATION, payload, outcome.reconcile_at))
        return True

    def retry_operation(
        self, tenant_id: UUID, operation_id: UUID, actor: str, event_type: OrderEventType
    ) -> bool:
        operation = self.operations_[operation_id]
        if operation.status not in {OperationStatus.FAILED, OperationStatus.NEEDS_ATTENTION}:
            return False
        self.operations_[operation_id] = replace(
            operation, status=OperationStatus.PENDING, unknown_since=None
        )
        self._event(operation.order_id, event_type, None, None, EventSource.STAFF)
        self._queue_operation(operation_id)
        return True

    def refresh_due(self, tenant_id: UUID, connection_id: UUID, run_at: datetime) -> None:
        payload = {"connection_id": str(connection_id)}
        if not any(kind is JobKind.REFRESH_ORDER_STATUS for kind, _, _ in self.jobs):
            self.jobs.append((JobKind.REFRESH_ORDER_STATUS, payload, run_at))

    # --- helpers ---------------------------------------------------------------------------
    def _event(
        self,
        order_id: UUID,
        kind: OrderEventType,
        before: OrderStatus | None,
        after: OrderStatus | None,
        source: EventSource = EventSource.SYSTEM,
    ) -> OrderEvent:
        event = OrderEvent(uuid4(), order_id, kind, before, after, source, "a", {}, self.clock())
        self.events_.append(event)
        return event

    def _queue_operation(self, operation_id: UUID) -> None:
        self.jobs.append((JobKind.ERP_ORDER_OPERATION, {"operation_id": str(operation_id)}, None))

    def take(self, kind: JobKind) -> Sequence[dict[str, str]]:
        """The queued jobs of one kind, removed from the queue (tests run them by hand)."""
        found = [payload for queued, payload, _ in self.jobs if queued is kind]
        self.jobs = [job for job in self.jobs if job[0] is not kind]
        return found


@dataclass
class Stock:
    levels: dict[str, Decimal | None] = field(default_factory=dict)
    down: bool = False

    def get_availability(
        self, context: TenantContext, skus: Sequence[str], warehouse: str | None = None
    ) -> Mapping[str, StockLevel]:
        if self.down:
            raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, "down")
        return {
            sku: StockLevel(sku, "each", self.levels[sku], None, NOW, "test")
            for sku in skus
            if sku in self.levels
        }


@dataclass(frozen=True)
class FakeProvider:
    capabilities: Capabilities
    key: str = "fake_erp"


@dataclass
class Destinations:
    writer: SimulatedOrderWriter | None
    abilities: OrderCapabilities = field(
        default_factory=lambda: OrderCapabilities(
            create_order=True, reference_lookup=True, order_status=True
        )
    )

    def _destination(self) -> Destination | None:
        if self.writer is None:
            return None
        connection = Connection(
            CONNECTION, TENANT, "fake_erp", "ERP", frozenset({Purpose.ORDERS}), {}, {}, None,
            ConnectionStatus.ACTIVE, None, "t", NOW, NOW,
        )  # fmt: skip
        provider: Any = FakeProvider(Capabilities(orders=self.abilities))
        return Destination(connection, provider, self.writer)

    def for_business(self, tenant_id: UUID) -> Destination | None:
        return self._destination()

    def for_connection(self, tenant_id: UUID, connection_id: UUID) -> Destination | None:
        return self._destination()


@dataclass
class Messenger:
    sent: list[tuple[TemplateKey, Mapping[str, object], str]] = field(default_factory=list)

    def send(
        self,
        tenant_id: UUID,
        enquiry_id: UUID,
        template: TemplateKey,
        render: Mapping[str, object],
        key: str,
        correlation_id: UUID,
    ) -> UUID:
        self.sent.append((template, render, key))
        return uuid4()

    def send_to(
        self,
        tenant_id: UUID,
        target: Any,
        template: TemplateKey,
        render: Mapping[str, object],
        key: str,
        correlation_id: UUID,
    ) -> UUID:
        self.sent.append((template, render, key))
        return uuid4()


@dataclass
class World:
    clock: Clock
    store: MemoryOrders
    stock: Stock
    destinations: Destinations
    service: OrderService

    @property
    def writer(self) -> SimulatedOrderWriter:
        assert self.destinations.writer is not None
        return self.destinations.writer

    def accepted(self) -> UUID:
        quotation_id = uuid4()
        self.store.quotations[quotation_id] = AcceptedQuotation(
            quotation_id, uuid4(), "accepted", 1, _content(), "a" * 64, uuid4()
        )
        return quotation_id

    def staff(self, role: Role = Role.SALES_OPERATOR) -> TenantContext:
        return TenantContext(TENANT, "sam@example.com", frozenset({role}))

    def run(self, kind: JobKind) -> None:
        handlers: dict[JobKind, Any] = {
            JobKind.ERP_ORDER_OPERATION: ErpOrderOperationHandler(
                self.store, self.destinations, self.clock
            ),
            JobKind.RECONCILE_ORDER_OPERATION: ReconcileOrderOperationHandler(
                self.store, self.destinations, self.clock
            ),
            JobKind.REFRESH_ORDER_STATUS: RefreshOrderStatusHandler(
                self.store, self.destinations, self.clock
            ),
        }
        for payload in self.store.take(kind):
            handlers[kind](_job(kind, payload))

    def only_operation(self) -> OrderOperation:
        [operation] = self.store.operations_.values()
        return operation


def _job(kind: JobKind, payload: dict[str, str], attempt: int = 1) -> ClaimedJob:
    return ClaimedJob(uuid4(), TENANT, kind, payload, attempt, 5, uuid4(), uuid4())


def _world(writer: SimulatedOrderWriter | None = None) -> World:
    clock = Clock()
    store = MemoryOrders(clock)
    stock = Stock({"PUMP-100": Decimal("12")})
    destinations = Destinations(writer)
    return World(clock, store, stock, destinations, OrderService(store, stock, destinations, clock))  # type: ignore[arg-type]


@pytest.fixture
def local() -> World:
    return _world()


@pytest.fixture
def erp() -> World:
    return _world(SimulatedOrderWriter(clock=lambda: NOW))


# --- creating --------------------------------------------------------------------------------


def test_an_accepted_quotation_becomes_one_local_order_copying_its_totals(local: World) -> None:
    order = local.service.create(local.staff(), local.accepted())
    assert (order.status, order.fulfilment, str(order.total)) == (
        OrderStatus.PLACED,
        Fulfilment.LOCAL,
        "10030.00",
    )
    assert [str(line.unit_price) for line in order.lines] == ["4250.00"]
    assert local.store.operations_ == {}
    with pytest.raises(OrderExists):
        local.service.create(local.staff(), order.quotation_id)


def test_only_accepted_quotations_and_order_managers_may_create(local: World) -> None:
    quotation_id = local.accepted()
    local.store.quotations[quotation_id] = replace(
        local.store.quotations[quotation_id], status="approved", response_id=None
    )
    with pytest.raises(OrderNotAllowed):
        local.service.create(local.staff(), quotation_id)
    from sales_platform.domain.tenancy import PermissionDenied

    with pytest.raises(PermissionDenied):
        local.service.create(local.staff(Role.VIEWER), local.accepted())


def test_short_stock_holds_the_order_for_staff(erp: World) -> None:
    erp.stock.levels["PUMP-100"] = Decimal("1")
    order = erp.service.create(erp.staff(), erp.accepted())
    assert order.status is OrderStatus.ON_HOLD and "PUMP-100" in (order.hold_reason or "")
    assert erp.store.take(JobKind.ERP_ORDER_OPERATION) == []  # nothing sent while held
    assert erp.only_operation().status is OperationStatus.PENDING


def test_unknown_stock_is_not_a_shortfall_and_a_down_source_means_try_again(local: World) -> None:
    local.stock.levels["PUMP-100"] = None
    assert local.service.create(local.staff(), local.accepted()).status is OrderStatus.PLACED
    local.stock.down = True
    from sales_platform.application.orders import OrderUnavailable

    with pytest.raises(OrderUnavailable):
        local.service.create(local.staff(), local.accepted())


def test_automatic_creation_happens_only_when_the_business_chose_it(local: World) -> None:
    handler = CreateOrderHandler(local.service)
    quotation_id = local.accepted()
    handler(_job(JobKind.CREATE_ORDER, {"quotation_id": str(quotation_id)}))
    assert local.store.orders == {}
    local.store.creation = OrderCreation.AUTOMATIC
    handler(_job(JobKind.CREATE_ORDER, {"quotation_id": str(quotation_id)}))
    handler(_job(JobKind.CREATE_ORDER, {"quotation_id": str(quotation_id)}))  # a retry
    assert len(local.store.orders) == 1


# --- the ERP write ---------------------------------------------------------------------------


def test_an_erp_order_is_written_once_with_its_stable_reference(erp: World) -> None:
    order = erp.service.create(erp.staff(), erp.accepted(), erp_customer="Big Buyer")
    erp.run(JobKind.ERP_ORDER_OPERATION)
    operation = erp.only_operation()
    assert operation.status is OperationStatus.SUCCEEDED
    assert operation.external_reference == f"QO-{order.order_id.hex}"
    [request] = erp.writer.requests
    assert (request.reference, request.customer, request.lines[0].unit_price) == (
        operation.external_reference,
        "Big Buyer",
        Decimal("4250.00"),
    )
    stored = erp.store.orders[order.order_id]
    assert stored.erp_order_id == "SO-00001" and stored.status is OrderStatus.PLACED  # a draft
    assert erp.store.take(JobKind.REFRESH_ORDER_STATUS)  # its status is followed from now on


def test_a_confirmed_erp_order_tells_the_customer(erp: World) -> None:
    erp.writer.initial_state = ErpOrderState.CONFIRMED
    order = erp.service.create(erp.staff(), erp.accepted())
    erp.run(JobKind.ERP_ORDER_OPERATION)
    assert erp.store.orders[order.order_id].status is OrderStatus.CONFIRMED
    assert len(erp.store.take(JobKind.NOTIFY_ORDER)) == 1


def test_nothing_sent_goes_back_to_pending_and_the_job_retries(erp: World) -> None:
    erp.writer.mode = "refuse_before_send"
    erp.service.create(erp.staff(), erp.accepted())
    [payload] = erp.store.take(JobKind.ERP_ORDER_OPERATION)
    handler = ErpOrderOperationHandler(erp.store, erp.destinations, erp.clock)
    with pytest.raises(ErpError) as raised:
        handler(_job(JobKind.ERP_ORDER_OPERATION, payload))
    assert raised.value.retryable
    assert erp.only_operation().status is OperationStatus.PENDING
    erp.writer.mode = None
    handler(_job(JobKind.ERP_ORDER_OPERATION, payload, attempt=2))
    assert erp.only_operation().status is OperationStatus.SUCCEEDED
    assert erp.writer.orders_created == 1


def test_a_refusal_waits_for_staff_who_send_it_again(erp: World) -> None:
    erp.writer.mode = "reject"
    order = erp.service.create(erp.staff(), erp.accepted())
    erp.run(JobKind.ERP_ORDER_OPERATION)
    operation = erp.only_operation()
    assert operation.status is OperationStatus.FAILED and "invalid_request" in (
        operation.last_error or ""
    )
    erp.writer.mode = None
    erp.service.resolve(
        erp.staff(), order.order_id, operation.operation_id, Resolution.NOT_FOUND, None
    )
    erp.run(JobKind.ERP_ORDER_OPERATION)
    assert erp.only_operation().status is OperationStatus.SUCCEEDED


def test_chaos_timeout_after_commit_ends_with_exactly_one_erp_order(erp: World) -> None:
    erp.writer.mode = "timeout_after_commit"
    order = erp.service.create(erp.staff(), erp.accepted())
    erp.run(JobKind.ERP_ORDER_OPERATION)  # the ERP has it; we heard nothing
    assert erp.only_operation().status is OperationStatus.UNKNOWN
    assert erp.store.take(JobKind.ERP_ORDER_OPERATION) == []  # never resent blindly
    erp.writer.mode = None
    erp.run(JobKind.RECONCILE_ORDER_OPERATION)
    assert erp.only_operation().status is OperationStatus.SUCCEEDED
    assert erp.store.orders[order.order_id].erp_order_id == "SO-00001"
    assert erp.writer.orders_created == 1
    assert OrderEventType.RECONCILED in {e.type for e in erp.store.events(TENANT, order.order_id)}


def test_an_unknown_outcome_never_reaches_the_job_runner_as_an_error(erp: World) -> None:
    erp.writer.mode = "timeout_after_commit"
    erp.service.create(erp.staff(), erp.accepted())
    [payload] = erp.store.take(JobKind.ERP_ORDER_OPERATION)
    job = _job(JobKind.ERP_ORDER_OPERATION, payload)

    @dataclass
    class OneJob:
        done: list[str] = field(default_factory=list)

        def claim(self, lease: timedelta) -> ClaimedJob | None:
            return None if self.done else job

        def complete(self, claimed: ClaimedJob) -> None:
            self.done.append("complete")

        def retry(self, claimed: ClaimedJob, delay: timedelta, error: str) -> None:
            self.done.append("retry")

        def fail(self, claimed: ClaimedJob, error: str) -> None:
            self.done.append("fail")

    queue = OneJob()
    handler = ErpOrderOperationHandler(erp.store, erp.destinations, erp.clock)
    JobRunner(queue, {JobKind.ERP_ORDER_OPERATION: handler}).run_once()
    assert queue.done == ["complete"]  # recorded as unknown, not retried by the runner
    assert erp.writer.orders_created == 1


def test_a_worker_stopped_while_sending_leaves_the_write_unknown(erp: World) -> None:
    erp.service.create(erp.staff(), erp.accepted())
    [payload] = erp.store.take(JobKind.ERP_ORDER_OPERATION)
    erp.store.begin_send(TENANT, erp.only_operation().operation_id)  # then the worker died
    ErpOrderOperationHandler(erp.store, erp.destinations, erp.clock)(
        _job(JobKind.ERP_ORDER_OPERATION, payload, attempt=2)
    )
    assert erp.only_operation().status is OperationStatus.UNKNOWN
    assert erp.writer.orders_created == 0  # not sent again


def test_still_missing_after_the_window_needs_staff_and_is_never_resent(erp: World) -> None:
    erp.writer.mode = "refuse_before_send"
    erp.service.create(erp.staff(), erp.accepted())
    operation = erp.only_operation()
    erp.store.begin_send(TENANT, operation.operation_id)
    erp.run(JobKind.ERP_ORDER_OPERATION)  # found "sending": unknown, look it up later
    erp.writer.mode = None
    erp.run(JobKind.RECONCILE_ORDER_OPERATION)
    assert erp.only_operation().status is OperationStatus.UNKNOWN  # still inside the window
    erp.clock.now = NOW + RECONCILE_WINDOW + timedelta(minutes=1)
    erp.run(JobKind.RECONCILE_ORDER_OPERATION)
    assert erp.only_operation().status is OperationStatus.NEEDS_ATTENTION
    assert erp.writer.orders_created == 0 and erp.store.take(JobKind.ERP_ORDER_OPERATION) == []


def test_an_erp_that_refuses_duplicates_itself_is_sent_again_after_the_window(erp: World) -> None:
    erp.destinations.abilities = OrderCapabilities(
        create_order=True, reference_lookup=True, idempotency_key=True
    )
    erp.service.create(erp.staff(), erp.accepted())
    erp.store.begin_send(TENANT, erp.only_operation().operation_id)
    erp.run(JobKind.ERP_ORDER_OPERATION)
    erp.clock.now = NOW + RECONCILE_WINDOW + timedelta(minutes=1)
    erp.run(JobKind.RECONCILE_ORDER_OPERATION)
    erp.run(JobKind.ERP_ORDER_OPERATION)
    assert erp.only_operation().status is OperationStatus.SUCCEEDED
    assert erp.writer.orders_created == 1


def test_without_reference_lookup_an_unknown_write_goes_straight_to_staff(erp: World) -> None:
    erp.destinations.abilities = OrderCapabilities(create_order=True)
    erp.writer.mode = "timeout_after_commit"
    erp.service.create(erp.staff(), erp.accepted())
    erp.run(JobKind.ERP_ORDER_OPERATION)
    erp.run(JobKind.RECONCILE_ORDER_OPERATION)
    assert erp.only_operation().status is OperationStatus.NEEDS_ATTENTION


def test_staff_record_the_erp_number_they_found(erp: World) -> None:
    erp.destinations.abilities = OrderCapabilities(create_order=True)
    erp.writer.mode = "timeout_after_commit"
    order = erp.service.create(erp.staff(), erp.accepted())
    erp.run(JobKind.ERP_ORDER_OPERATION)
    erp.run(JobKind.RECONCILE_ORDER_OPERATION)
    operation = erp.only_operation()
    with pytest.raises(ValueError):
        erp.service.resolve(
            erp.staff(), order.order_id, operation.operation_id, Resolution.FOUND, " "
        )
    erp.service.resolve(
        erp.staff(), order.order_id, operation.operation_id, Resolution.FOUND, "SO-00001"
    )
    assert erp.store.orders[order.order_id].erp_order_id == "SO-00001"
    with pytest.raises(OrderNotAllowed):  # resolved once only
        erp.service.resolve(
            erp.staff(), order.order_id, operation.operation_id, Resolution.NOT_FOUND, None
        )


def test_releasing_a_held_erp_order_sends_it(erp: World) -> None:
    erp.stock.levels["PUMP-100"] = Decimal("0")
    order = erp.service.create(erp.staff(), erp.accepted())
    with pytest.raises(OrderNotAllowed):
        erp.service.move(erp.staff(), order.order_id, OrderStatus.CONFIRMED, None)
    erp.service.move(erp.staff(), order.order_id, OrderStatus.PLACED, None)
    erp.run(JobKind.ERP_ORDER_OPERATION)
    assert erp.only_operation().status is OperationStatus.SUCCEEDED


def test_cancelling_a_held_erp_order_never_sends_it(erp: World) -> None:
    erp.stock.levels["PUMP-100"] = Decimal("0")
    order = erp.service.create(erp.staff(), erp.accepted())
    with pytest.raises(ValueError):
        erp.service.move(erp.staff(), order.order_id, OrderStatus.CANCELLED, None)
    erp.service.move(erp.staff(), order.order_id, OrderStatus.CANCELLED, "Customer changed mind")
    assert erp.only_operation().status is OperationStatus.FAILED
    assert erp.store.orders[order.order_id].status is OrderStatus.CANCELLED
    assert erp.writer.orders_created == 0


# --- status, notices and tracking ------------------------------------------------------------


def test_local_orders_move_step_by_step_and_customers_hear_the_milestones(local: World) -> None:
    order = local.service.create(local.staff(), local.accepted())
    for target in (OrderStatus.CONFIRMED, OrderStatus.IN_PROGRESS, OrderStatus.SHIPPED):
        local.service.move(local.staff(Role.APPROVER), order.order_id, target, None)
    with pytest.raises(OrderNotAllowed):
        local.service.move(local.staff(), order.order_id, OrderStatus.CANCELLED, "too late")
    local.service.move(local.staff(), order.order_id, OrderStatus.DELIVERED, None)
    notices = local.store.take(JobKind.NOTIFY_ORDER)
    assert len(notices) == 3  # confirmed, shipped, delivered; not "in progress"


def test_erp_statuses_are_followed_and_a_final_one_never_reopens(erp: World) -> None:
    order = erp.service.create(erp.staff(), erp.accepted())
    erp.run(JobKind.ERP_ORDER_OPERATION)
    reference = erp.only_operation().external_reference
    erp.writer.set_state(TENANT, reference, ErpOrderState.DELIVERED)
    erp.run(JobKind.REFRESH_ORDER_STATUS)
    assert erp.store.orders[order.order_id].status is OrderStatus.DELIVERED
    assert len(erp.store.take(JobKind.NOTIFY_ORDER)) == 1
    erp.writer.set_state(TENANT, reference, ErpOrderState.CONFIRMED)
    erp.run(JobKind.REFRESH_ORDER_STATUS)  # delivered orders are no longer followed
    assert erp.store.orders[order.order_id].status is OrderStatus.DELIVERED
    assert erp.store.take(JobKind.REFRESH_ORDER_STATUS) == []  # nothing open: following stops


def test_a_notice_names_the_order_and_is_sent_once_per_event(local: World) -> None:
    order = local.service.create(local.staff(), local.accepted())
    local.service.move(local.staff(), order.order_id, OrderStatus.CONFIRMED, None)
    [payload] = local.store.take(JobKind.NOTIFY_ORDER)
    messenger = Messenger()
    OrderNotificationHandler(local.store, messenger)(_job(JobKind.NOTIFY_ORDER, payload))  # type: ignore[arg-type]
    [(template, render, key)] = messenger.sent
    assert template is TemplateKey.ORDER_CONFIRMED and render["order"] == order.order_number
    assert key == f"order:{payload['event_id']}"  # the outbox key: a retried job sends nothing new


@dataclass(frozen=True)
class Target:
    business_name: str = "Acme"


@dataclass
class Targets:
    known: set[str]

    def for_identity(self, tenant_id: UUID, connection_id: UUID, external_id: str) -> Any:
        return Target() if external_id in self.known else None


def test_track_my_order_answers_only_the_askers_own_orders(local: World) -> None:
    mine, theirs = uuid4(), uuid4()
    for contact in (mine, theirs):
        quotation_id = local.accepted()
        local.store.contacts[local.store.quotations[quotation_id].enquiry_id] = contact
        local.service.create(local.staff(), quotation_id)
    local.store.identities[(CONNECTION, "+911")] = mine
    local.store.identities[(CONNECTION, "+912")] = uuid4()  # a customer with no orders
    messenger = Messenger()
    route = OrderStatusRoute(local.store, Targets({"+911", "+912"}), messenger)  # type: ignore[arg-type]
    conn = Connection(
        CONNECTION, TENANT, "chat", "Chat", frozenset({Purpose.CUSTOMER_MESSAGES}), {}, {}, None,
        ConnectionStatus.ACTIVE, None, "t", NOW, NOW,
    )  # fmt: skip
    assert route(conn, OrderStatusRequest("e1", "+911"), uuid4()) == "1 orders"
    template, render, _ = messenger.sent[-1]
    listed = render["orders"]
    assert isinstance(listed, list)
    [line] = listed
    assert template is TemplateKey.ORDER_STATUS and str(line).endswith("total 10030.00 INR")
    mine_order = next(o for o in local.store.orders.values() if o.contact_id == mine)
    assert mine_order.order_number in str(line)
    assert route(conn, OrderStatusRequest("e2", "+912"), uuid4()) == "no orders"
    assert messenger.sent[-1][0] is TemplateKey.ORDER_NONE
    other = next(o for o in local.store.orders.values() if o.contact_id == theirs)
    asked = OrderStatusRequest("e3", "+911", other.order_number)  # someone else's number
    assert route(conn, asked, uuid4()) == "no orders"
    assert route(conn, OrderStatusRequest("e4", "+999"), uuid4()) == "unknown sender"
