"""Orders over in-memory fakes: creation rules, stock holds, status moves, customer notices and
"track my order". The PostgreSQL store is tested in tests/integration/orders/.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.catalogue.domain.catalogue import StockLevel
from sales_platform.catalogue.ports.erp import ErpError, ErpErrorCode
from sales_platform.integrations.domain.connections import (
    Connection,
    ConnectionStatus,
    Purpose,
)
from sales_platform.integrations.domain.events import OrderStatusRequest
from sales_platform.integrations.domain.messages import TemplateKey
from sales_platform.orders.application.orders import (
    CreateOrderHandler,
    OrderNotificationHandler,
    OrderService,
    OrderStatusRoute,
)
from sales_platform.orders.domain.orders import (
    EventSource,
    Order,
    OrderCreation,
    OrderEvent,
    OrderEventType,
    OrderNotAllowed,
    OrderStatus,
    order_number,
)
from sales_platform.orders.ports.orders import (
    AcceptedQuotation,
    NewOrder,
    OrderExists,
    OrderFilter,
    OrderListItem,
    StatusChange,
)
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.platform.ports.jobs import ClaimedJob

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
            new.currency, new.subtotal, new.tax, new.total, new.lines, new.actor, self.clock(),
            self.clock(), new.hold_reason,
        )  # fmt: skip
        self.orders[order.order_id] = order
        self._event(order.order_id, OrderEventType.CREATED, None, OrderStatus.PLACED, new.source)
        if new.status is OrderStatus.ON_HOLD:
            self._event(order.order_id, OrderEventType.HELD, OrderStatus.PLACED, new.status)
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

    def for_identity(
        self, tenant_id: UUID, connection_id: UUID, external_id: str
    ) -> Sequence[Order]:
        contact = self.identities.get((connection_id, external_id))
        return [o for o in self.orders.values() if contact and o.contact_id == contact]

    def change_status(self, tenant_id: UUID, change: StatusChange) -> Order | None:
        order = self.orders[change.order_id]
        if order.status is not change.expected:
            return None
        updated = replace(order, status=change.target, hold_reason=change.hold_reason)
        self.orders[order.order_id] = updated
        event = self._event(
            order.order_id, change.event_type, change.expected, change.target, change.source
        )
        if change.notify_customer:
            self.jobs.append((JobKind.NOTIFY_ORDER, {"event_id": str(event.event_id)}, None))
        return updated

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
    service: OrderService

    def accepted(self) -> UUID:
        quotation_id = uuid4()
        self.store.quotations[quotation_id] = AcceptedQuotation(
            quotation_id, uuid4(), "accepted", 1, _content(), "a" * 64, uuid4()
        )
        return quotation_id

    def staff(self, role: Role = Role.SALES_OPERATOR) -> TenantContext:
        return TenantContext(TENANT, "sam@example.com", frozenset({role}))


def _job(kind: JobKind, payload: dict[str, str], attempt: int = 1) -> ClaimedJob:
    return ClaimedJob(uuid4(), TENANT, kind, payload, attempt, 5, uuid4(), uuid4())


def _world() -> World:
    clock = Clock()
    store = MemoryOrders(clock)
    stock = Stock({"PUMP-100": Decimal("12")})
    return World(clock, store, stock, OrderService(store, stock))  # type: ignore[arg-type]


@pytest.fixture
def local() -> World:
    return _world()


# --- creating --------------------------------------------------------------------------------


def test_an_accepted_quotation_becomes_one_local_order_copying_its_totals(local: World) -> None:
    order = local.service.create(local.staff(), local.accepted())
    assert (order.status, str(order.total)) == (OrderStatus.PLACED, "10030.00")
    assert [str(line.unit_price) for line in order.lines] == ["4250.00"]
    with pytest.raises(OrderExists):
        local.service.create(local.staff(), order.quotation_id)


def test_only_accepted_quotations_and_order_managers_may_create(local: World) -> None:
    quotation_id = local.accepted()
    local.store.quotations[quotation_id] = replace(
        local.store.quotations[quotation_id], status="approved", response_id=None
    )
    with pytest.raises(OrderNotAllowed):
        local.service.create(local.staff(), quotation_id)
    from sales_platform.platform.domain.tenancy import PermissionDenied

    with pytest.raises(PermissionDenied):
        local.service.create(local.staff(Role.VIEWER), local.accepted())


def test_short_stock_holds_the_order_for_staff(local: World) -> None:
    local.stock.levels["PUMP-100"] = Decimal("1")
    order = local.service.create(local.staff(), local.accepted())
    assert order.status is OrderStatus.ON_HOLD and "PUMP-100" in (order.hold_reason or "")
    released = local.service.move(local.staff(), order.order_id, OrderStatus.PLACED, None)
    assert released.status is OrderStatus.PLACED


def test_unknown_stock_is_not_a_shortfall_and_a_down_source_means_try_again(local: World) -> None:
    local.stock.levels["PUMP-100"] = None
    assert local.service.create(local.staff(), local.accepted()).status is OrderStatus.PLACED
    local.stock.down = True
    from sales_platform.orders.application.orders import OrderUnavailable

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
