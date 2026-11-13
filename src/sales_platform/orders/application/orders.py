"""Orders from accepted quotations (phase 7, roadmap M6b, ADR 0024; ADR 0027).

Purpose: an accepted quotation becomes exactly one order, kept here, whose status staff move
and the customer hears about. (ADR 0027 removed writing orders to an ERP.)

- OrderService: the portal's use cases (list, detail, create from an accepted quotation, move
  an order, release a held order) and the business's "create orders automatically" setting.
- CreateOrderHandler (create_order job, queued with every acceptance): creates the order only
  when the business chose automatic creation.
- OrderNotificationHandler: the customer's message for a status change (fixed wording).
- OrderStatusRoute: "where is my order?" from a customer: their own contact's orders only.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

from sales_platform.catalogue.ports.sources import ErpError, ErpErrorCode, ProductSource
from sales_platform.conversations.application.customer_messages import CustomerMessenger
from sales_platform.conversations.ports.conversations import CustomerTargets
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.domain.events import InboundEvent, OrderStatusRequest
from sales_platform.integrations.domain.messages import TemplateKey
from sales_platform.orders.domain.orders import (
    CUSTOMER_NOTICES,
    STATUS_WORDS,
    EventSource,
    Order,
    OrderCreation,
    OrderEvent,
    OrderEventType,
    OrderLine,
    OrderNotAllowed,
    OrderStatus,
    check_move,
    clean_reason,
    order_lines,
    stock_shortfall,
)
from sales_platform.orders.ports.orders import (
    NewOrder,
    OrderExists,
    OrderFilter,
    OrderListItem,
    OrderStore,
    StatusChange,
)
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.domain.tenancy import Permission, Role, TenantContext
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.quotations.domain.quotations import QuotationStatus, quotation_number

SYSTEM_SUBJECT = "system:orders"
_TEMPLATES: Mapping[OrderStatus, TemplateKey] = {
    OrderStatus.CONFIRMED: TemplateKey.ORDER_CONFIRMED,
    OrderStatus.SHIPPED: TemplateKey.ORDER_SHIPPED,
    OrderStatus.DELIVERED: TemplateKey.ORDER_DELIVERED,
    OrderStatus.CANCELLED: TemplateKey.ORDER_CANCELLED,
}


def system_context(tenant_id: UUID) -> TenantContext:
    return TenantContext(tenant_id, SYSTEM_SUBJECT, frozenset({Role.VIEWER}))


class OrderNotFound(LookupError):
    """No such order (or quotation) in this business."""


class OrderUnavailable(Exception):
    """The stock check could not be done right now; try again."""


@dataclass(frozen=True)
class OrderDetail:
    order: Order
    quotation_number: str
    events: tuple[OrderEvent, ...]


class OrderService:
    def __init__(self, store: OrderStore, catalogue: ProductSource) -> None:
        self._store = store
        self._catalogue = catalogue

    # --- reads -----------------------------------------------------------------------------
    def list(self, context: TenantContext, query: OrderFilter) -> Sequence[OrderListItem]:
        context.require(Permission.VIEW_ORDERS)
        if not 1 <= query.limit <= 100:
            raise ValueError("Page size must be between 1 and 100")
        return self._store.list(context.tenant_id, query)

    def detail(self, context: TenantContext, order_id: UUID) -> OrderDetail:
        context.require(Permission.VIEW_ORDERS)
        order = self._order(context, order_id)
        return OrderDetail(
            order,
            quotation_number(order.quotation_id),
            tuple(self._store.events(context.tenant_id, order_id)),
        )

    def for_quotation(self, context: TenantContext, quotation_id: UUID) -> Order | None:
        context.require(Permission.VIEW_ORDERS)
        return self._store.for_quotation(context.tenant_id, quotation_id)

    def for_enquiry(self, context: TenantContext, enquiry_id: UUID) -> Order | None:
        """The customer system's view (API key with enquiries:view): its enquiry's order."""
        context.require(Permission.VIEW_ENQUIRIES)
        return self._store.for_enquiry(context.tenant_id, enquiry_id)

    def creation(self, context: TenantContext) -> OrderCreation:
        context.require(Permission.VIEW_ORDERS)
        return self._store.order_creation(context.tenant_id)

    def set_creation(self, context: TenantContext, value: OrderCreation) -> None:
        context.require(Permission.MANAGE_TENANT)
        self._store.set_order_creation(context.tenant_id, value, context.subject)

    # --- creating --------------------------------------------------------------------------
    def create(self, context: TenantContext, quotation_id: UUID) -> Order:
        """Staff "Create order" on an accepted quotation (also older ones, Q14)."""
        context.require(Permission.MANAGE_ORDERS)
        return self._create(context.tenant_id, quotation_id, context.subject, EventSource.STAFF)

    def create_automatically(self, tenant_id: UUID, quotation_id: UUID) -> Order | None:
        """After an acceptance, when the business chose automatic creation (Q1)."""
        if self._store.order_creation(tenant_id) is not OrderCreation.AUTOMATIC:
            return None
        try:
            return self._create(tenant_id, quotation_id, SYSTEM_SUBJECT, EventSource.SYSTEM)
        except OrderExists as error:
            return self._store.get(tenant_id, error.order_id)

    def _create(
        self, tenant_id: UUID, quotation_id: UUID, actor: str, source: EventSource
    ) -> Order:
        accepted = self._store.accepted_quotation(tenant_id, quotation_id)
        if accepted is None:
            raise OrderNotFound("Not found")
        if accepted.status != QuotationStatus.ACCEPTED.value or accepted.response_id is None:
            raise OrderNotAllowed("Only a quotation the customer accepted can become an order")
        existing = self._store.for_quotation(tenant_id, quotation_id)
        if existing is not None:
            raise OrderExists(existing.order_id)
        content = accepted.content
        lines = order_lines(content)
        short = self._shortfall(tenant_id, lines)
        return self._store.create(
            tenant_id,
            NewOrder(
                order_id=uuid4(),
                quotation=accepted,
                status=OrderStatus.ON_HOLD if short else OrderStatus.PLACED,
                hold_reason=f"Not enough stock for {', '.join(short)}" if short else None,
                currency=str(content["currency"]),
                subtotal=Decimal(str(content["subtotal"])),
                tax=Decimal(str(content["tax"])),
                total=Decimal(str(content["total"])),
                lines=lines,
                actor=actor,
                source=source,
            ),
        )

    def _shortfall(self, tenant_id: UUID, lines: Sequence[OrderLine]) -> tuple[str, ...]:
        """Re-check stock (Q5); unknown stock is not a shortfall."""
        try:
            levels = self._catalogue.get_availability(
                system_context(tenant_id), sorted({line.sku for line in lines})
            )
        except ErpError as error:
            if error.retryable:
                raise OrderUnavailable("Stock could not be checked; try again") from None
            levels = {}
        available = {sku: level.available_quantity for sku, level in levels.items()}
        return stock_shortfall(lines, available)

    # --- staff changes ---------------------------------------------------------------------
    def move(
        self, context: TenantContext, order_id: UUID, target: OrderStatus, reason: str | None
    ) -> Order:
        """An order's next status (Q8); cancel and hold need a reason."""
        context.require(Permission.MANAGE_ORDERS)
        order = self._order(context, order_id)
        check_move(order, target)
        why = clean_reason(reason, required=target in {OrderStatus.CANCELLED, OrderStatus.ON_HOLD})
        changed = self._store.change_status(
            context.tenant_id,
            StatusChange(
                order_id=order.order_id,
                expected=order.status,
                target=target,
                event_type=OrderEventType.STATUS_CHANGED,
                source=EventSource.STAFF,
                actor=context.subject,
                detail={} if why is None else {"reason": why},
                hold_reason=why if target is OrderStatus.ON_HOLD else None,
                notify_customer=target in CUSTOMER_NOTICES,
            ),
        )
        if changed is None:
            raise OrderNotAllowed("The order changed meanwhile; reload it")
        return changed

    def _order(self, context: TenantContext, order_id: UUID) -> Order:
        order = self._store.get(context.tenant_id, order_id)
        if order is None:
            raise OrderNotFound("Not found")
        return order


# --- jobs ----------------------------------------------------------------------------------


def _uuid(job: ClaimedJob, name: str) -> UUID:
    try:
        return UUID(job.payload[name])
    except (KeyError, ValueError):
        raise PermanentJobError(f"Job payload has no valid {name}") from None


class CreateOrderHandler:
    """create_order, payload {"quotation_id"}: queued with every acceptance (atomically)."""

    def __init__(self, service: OrderService) -> None:
        self._service = service

    def __call__(self, job: ClaimedJob) -> None:
        tenant_id = job.require_tenant()
        try:
            self._service.create_automatically(tenant_id, _uuid(job, "quotation_id"))
        except OrderNotFound:
            raise PermanentJobError("Quotation not found") from None
        except OrderNotAllowed as error:
            raise PermanentJobError(str(error)) from None
        except OrderUnavailable as error:
            # The stock check could not run: the job runner retries it later.
            raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, str(error)) from None


class OrderNotificationHandler:
    """notify_order, payload {"event_id"}: the customer's message about a status change."""

    def __init__(self, store: OrderStore, customers: CustomerMessenger) -> None:
        self._store = store
        self._customers = customers

    def __call__(self, job: ClaimedJob) -> None:
        tenant_id = job.require_tenant()
        event = self._store.event(tenant_id, _uuid(job, "event_id"))
        if event is None:
            raise PermanentJobError("Order event not found")
        template = None if event.to_status is None else _TEMPLATES.get(event.to_status)
        if template is None:
            return
        order = self._store.get(tenant_id, event.order_id)
        if order is None:
            raise PermanentJobError("Order not found")
        self._customers.send(
            tenant_id,
            order.enquiry_id,
            template,
            {"order": order.order_number, "number": quotation_number(order.quotation_id)},
            f"order:{event.event_id}",
            job.correlation_id,
        )


def status_line(order: Order) -> str:
    return (
        f"{order.order_number} (quotation {quotation_number(order.quotation_id)}): "
        f"{STATUS_WORDS[order.status]}, total {order.total} {order.currency}"
    )


class OrderStatusRoute:
    """ORDER_STATUS_REQUEST ("where is my order?", Q10): only the orders of the contact
    behind the asking identity, never anyone else's; fixed wording, no AI."""

    def __init__(
        self, store: OrderStore, targets: CustomerTargets, messenger: CustomerMessenger
    ) -> None:
        self._store = store
        self._targets = targets
        self._messenger = messenger

    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        assert isinstance(event, OrderStatusRequest)
        tenant_id = connection.tenant_id
        target = self._targets.for_identity(
            tenant_id, connection.connection_id, event.sender_external_id
        )
        if target is None:
            return "unknown sender"
        orders = self._store.for_identity(
            tenant_id, connection.connection_id, event.sender_external_id
        )
        if event.order_number is not None:
            orders = [order for order in orders if order.order_number == event.order_number]
        key = f"orders:{connection.connection_id}:{event.provider_event_id}"
        if not orders:
            self._messenger.send_to(
                tenant_id, target, TemplateKey.ORDER_NONE,
                {"business": target.business_name}, key, uuid4(),
            )  # fmt: skip
            return "no orders"
        self._messenger.send_to(
            tenant_id, target, TemplateKey.ORDER_STATUS,
            {"business": target.business_name, "orders": [status_line(o) for o in orders[:5]]},
            key, uuid4(),
        )  # fmt: skip
        return f"{len(orders)} orders"
