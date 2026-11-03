"""Orders from accepted quotations (phase 7, roadmap M6b, ADR 0024).

Purpose: an accepted quotation becomes exactly one order, kept by us (local) or written once
to the business's ERP, whose status is followed and announced to the customer.

- OrderService: the portal's use cases (list, detail, create from an accepted quotation, move
  a local order, release a held order, resolve an ERP write that needs attention) and the
  business's "create orders automatically" setting.
- CreateOrderHandler (create_order job, queued with every acceptance): creates the order only
  when the business chose automatic creation.
- ErpOrderOperationHandler (erp_order_operation): intent before call. The operation row (with
  the stable reference) exists before the ERP is called; pending -> sending is compare-and-set,
  so only one worker calls. Outcomes: succeeded; failed (the ERP certainly refused: staff fix
  and send again); unknown (it may exist: looked up, never resent blindly). A temporary failure
  before anything was sent goes back to pending and the job retries. An unknown outcome never
  reaches the job runner as an error, so it is never retried blindly.
- ReconcileOrderOperationHandler: looks an unknown write up by its reference every 5 minutes
  for an hour; found -> succeeded; still missing -> needs_attention for staff (or, only when
  the ERP refuses duplicates itself, sent again).
- RefreshOrderStatusHandler: follows ERP orders' statuses every 15 minutes while any is open.
- OrderNotificationHandler: the customer's message for a status change (fixed wording).
- OrderStatusRoute: "where is my order?" from a customer: their own contact's orders only.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from sales_platform.catalogue.ports.erp import (
    ErpError,
    ErpErrorCode,
    ErpOrder,
    OrderLineRequest,
    OrderWriter,
    ProductSource,
    SalesOrderRequest,
)
from sales_platform.conversations.application.customer_messages import CustomerMessenger
from sales_platform.conversations.ports.conversations import CustomerTargets
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection, Purpose
from sales_platform.integrations.domain.events import InboundEvent, OrderStatusRequest
from sales_platform.integrations.domain.messages import TemplateKey
from sales_platform.integrations.ports.connections import ConnectionStore
from sales_platform.integrations.ports.providers import OperationUnsupported, Provider
from sales_platform.orders.domain.orders import (
    CUSTOMER_NOTICES,
    RECONCILE_EVERY,
    RECONCILE_WINDOW,
    STATUS_REFRESH,
    STATUS_WORDS,
    EventSource,
    Fulfilment,
    OperationStatus,
    Order,
    OrderCreation,
    OrderEvent,
    OrderEventType,
    OrderLine,
    OrderNotAllowed,
    OrderOperation,
    OrderStatus,
    Resolution,
    check_local_move,
    clean_customer_account,
    clean_reason,
    erp_move_allowed,
    erp_status,
    external_reference,
    order_lines,
    stock_shortfall,
)
from sales_platform.orders.ports.orders import (
    NewOrder,
    OperationOutcome,
    OrderExists,
    OrderFilter,
    OrderListItem,
    OrderStore,
    StatusChange,
)
from sales_platform.orders.ports.writers import order_writer_of
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.domain.tenancy import Permission, Role, TenantContext
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.platform.ports.secrets import SecretStore
from sales_platform.quotations.domain.quotations import QuotationStatus, quotation_number

logger = logging.getLogger(__name__)
SYSTEM_SUBJECT = "system:orders"
ERP_ACTOR = "erp"
_SENDING = frozenset({OperationStatus.SENDING})
_UNKNOWN = frozenset({OperationStatus.UNKNOWN})
_BEFORE_SEND = frozenset({OperationStatus.PENDING})
_RESOLVABLE = frozenset(
    {OperationStatus.UNKNOWN, OperationStatus.NEEDS_ATTENTION, OperationStatus.FAILED}
)
_TEMPLATES: Mapping[OrderStatus, TemplateKey] = {
    OrderStatus.CONFIRMED: TemplateKey.ORDER_CONFIRMED,
    OrderStatus.SHIPPED: TemplateKey.ORDER_SHIPPED,
    OrderStatus.DELIVERED: TemplateKey.ORDER_DELIVERED,
    OrderStatus.CANCELLED: TemplateKey.ORDER_CANCELLED,
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def system_context(tenant_id: UUID) -> TenantContext:
    return TenantContext(tenant_id, SYSTEM_SUBJECT, frozenset({Role.VIEWER}))


class OrderNotFound(LookupError):
    """No such order (or quotation) in this business."""


class OrderUnavailable(Exception):
    """The stock check could not be done right now; try again."""


@dataclass(frozen=True)
class Destination:
    """Where a business's orders are written: its connection, provider and writer."""

    connection: Connection
    provider: Provider
    writer: OrderWriter


class DestinationLookup(Protocol):
    def for_business(self, tenant_id: UUID) -> Destination | None: ...

    def for_connection(self, tenant_id: UUID, connection_id: UUID) -> Destination | None: ...


class OrderDestinations:
    """The business's active connection with Purpose.ORDERS whose provider can create orders
    and is set to write them; None means orders stay with us (local)."""

    def __init__(
        self, connections: ConnectionStore, registry: ProviderRegistry, secrets: SecretStore
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._secrets = secrets

    def for_business(self, tenant_id: UUID) -> Destination | None:
        for connection in self._connections.list_for(tenant_id, Purpose.ORDERS):
            if connection.active:
                destination = self._open(connection)
                if destination is not None:
                    return destination
        return None

    def for_connection(self, tenant_id: UUID, connection_id: UUID) -> Destination | None:
        connection = self._connections.get(tenant_id, connection_id)
        if connection is None or not connection.active:
            return None
        return self._open(connection)

    def _open(self, connection: Connection) -> Destination | None:
        provider = self._registry.find(connection.provider_key)
        abilities = None if provider is None else provider.capabilities.orders
        if provider is None or abilities is None or not abilities.create_order:
            return None
        try:
            writer = order_writer_of(provider).order_writer(connection, self._secrets)
        except OperationUnsupported:
            return None
        return Destination(connection, provider, writer)


@dataclass(frozen=True)
class OrderDetail:
    order: Order
    quotation_number: str
    events: tuple[OrderEvent, ...]
    operations: tuple[OrderOperation, ...]


class OrderService:
    def __init__(
        self,
        store: OrderStore,
        catalogue: ProductSource,
        destinations: DestinationLookup,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._catalogue = catalogue
        self._destinations = destinations
        self._clock = clock

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
            tuple(self._store.operations(context.tenant_id, order_id)),
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
    def create(
        self, context: TenantContext, quotation_id: UUID, erp_customer: str | None = None
    ) -> Order:
        """Staff "Create order" on an accepted quotation (also older ones, Q14)."""
        context.require(Permission.MANAGE_ORDERS)
        return self._create(
            context.tenant_id,
            quotation_id,
            context.subject,
            EventSource.STAFF,
            clean_customer_account(erp_customer),
        )

    def create_automatically(self, tenant_id: UUID, quotation_id: UUID) -> Order | None:
        """After an acceptance, when the business chose automatic creation (Q1)."""
        if self._store.order_creation(tenant_id) is not OrderCreation.AUTOMATIC:
            return None
        try:
            return self._create(tenant_id, quotation_id, SYSTEM_SUBJECT, EventSource.SYSTEM, None)
        except OrderExists as error:
            return self._store.get(tenant_id, error.order_id)

    def _create(
        self,
        tenant_id: UUID,
        quotation_id: UUID,
        actor: str,
        source: EventSource,
        erp_customer: str | None,
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
        destination = self._destinations.for_business(tenant_id)
        order_id = uuid4()
        held = bool(short)
        return self._store.create(
            tenant_id,
            NewOrder(
                order_id=order_id,
                quotation=accepted,
                status=OrderStatus.ON_HOLD if held else OrderStatus.PLACED,
                hold_reason=f"Not enough stock for {', '.join(short)}" if held else None,
                fulfilment=Fulfilment.LOCAL if destination is None else Fulfilment.ERP,
                erp_connection_id=None
                if destination is None
                else destination.connection.connection_id,
                erp_customer=erp_customer if destination is not None else None,
                currency=str(content["currency"]),
                subtotal=Decimal(str(content["subtotal"])),
                tax=Decimal(str(content["tax"])),
                total=Decimal(str(content["total"])),
                lines=lines,
                actor=actor,
                source=source,
                external_reference=None if destination is None else external_reference(order_id),
                send_now=not held,
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
        """A local order's next status (Q8); cancel and hold need a reason."""
        context.require(Permission.MANAGE_ORDERS)
        order = self._order(context, order_id)
        if order.fulfilment is Fulfilment.ERP and order.erp_order_id is None:
            return self._held_erp_order(context, order, target, reason)
        check_local_move(order, target)
        why = clean_reason(reason, required=target in {OrderStatus.CANCELLED, OrderStatus.ON_HOLD})
        return self._changed(
            context,
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

    def _held_erp_order(
        self, context: TenantContext, order: Order, target: OrderStatus, reason: str | None
    ) -> Order:
        """An ERP order not written yet: staff release it (it is sent) or cancel it."""
        if order.status is not OrderStatus.ON_HOLD:
            raise OrderNotAllowed("This order is being written to the ERP; wait for the result")
        if target is OrderStatus.PLACED:
            return self._changed(
                context,
                StatusChange(
                    order_id=order.order_id,
                    expected=OrderStatus.ON_HOLD,
                    target=OrderStatus.PLACED,
                    event_type=OrderEventType.STATUS_CHANGED,
                    source=EventSource.STAFF,
                    actor=context.subject,
                    detail={"action": "released"},
                    send_operation=True,
                ),
            )
        if target is OrderStatus.CANCELLED:
            why = clean_reason(reason, required=True)
            for operation in self._store.operations(context.tenant_id, order.order_id):
                self._store.finish_operation(
                    context.tenant_id,
                    operation.operation_id,
                    _BEFORE_SEND,
                    OperationOutcome(
                        OperationStatus.FAILED,
                        None,
                        context.subject,
                        error="Cancelled before it was sent",
                    ),
                )
            return self._changed(
                context,
                StatusChange(
                    order_id=order.order_id,
                    expected=OrderStatus.ON_HOLD,
                    target=OrderStatus.CANCELLED,
                    event_type=OrderEventType.STATUS_CHANGED,
                    source=EventSource.STAFF,
                    actor=context.subject,
                    detail={"reason": why},
                    notify_customer=True,
                ),
            )
        raise OrderNotAllowed("A held order can be released or cancelled")

    def resolve(
        self,
        context: TenantContext,
        order_id: UUID,
        operation_id: UUID,
        resolution: Resolution,
        erp_order_id: str | None,
    ) -> Order:
        """Staff answer for an ERP write that failed or needs attention (both confirmed in
        the portal): FOUND records the ERP's order number; NOT_FOUND sends it again with the
        same reference (the ERP is searched for it first)."""
        context.require(Permission.MANAGE_ORDERS)
        order = self._order(context, order_id)
        operation = self._store.operation(context.tenant_id, operation_id)
        if operation is None or operation.order_id != order.order_id:
            raise OrderNotFound("Not found")
        if resolution is Resolution.FOUND:
            number = clean_customer_account(erp_order_id)
            if number is None:
                raise ValueError("Enter the order number shown in the ERP")
            done = self._store.finish_operation(
                context.tenant_id,
                operation_id,
                _RESOLVABLE,
                OperationOutcome(
                    OperationStatus.SUCCEEDED,
                    OrderEventType.RESOLVED,
                    context.subject,
                    erp_order_id=number,
                    detail={"resolution": "found in the ERP"},
                ),
            )
            if done:
                self._store.refresh_due(
                    context.tenant_id, operation.connection_id, self._clock() + STATUS_REFRESH
                )
        else:
            done = self._store.retry_operation(
                context.tenant_id,
                operation_id,
                context.subject,
                OrderEventType.RESOLVED
                if operation.status is not OperationStatus.FAILED
                else OrderEventType.RETRIED,
            )
        if not done:
            raise OrderNotAllowed("This ERP write does not need attention any more")
        return self._order(context, order_id)

    def _changed(self, context: TenantContext, change: StatusChange) -> Order:
        changed = self._store.change_status(context.tenant_id, change)
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
            raise ErpError(ErpErrorCode.TEMPORARY_UNAVAILABLE, str(error)) from None


def _request(order: Order, reference: str) -> SalesOrderRequest:
    return SalesOrderRequest(
        reference=reference,
        order_number=order.order_number,
        customer=order.erp_customer,
        currency=order.currency,
        lines=tuple(
            OrderLineRequest(line.sku, line.quantity, line.unit, line.unit_price)
            for line in order.lines
        ),
        ordered_on=order.created_at.date(),
    )


def _succeeded(found: ErpOrder, event: OrderEventType, actor: str) -> OperationOutcome:
    target = erp_status(found.state.value)
    return OperationOutcome(
        OperationStatus.SUCCEEDED,
        event,
        actor,
        erp_order_id=found.erp_order_id,
        order_status=target,
        detail={"erp_order_id": found.erp_order_id, "erp_state": found.state.value},
        notify_customer=target in CUSTOMER_NOTICES,
    )


class ErpOrderOperationHandler:
    """erp_order_operation, payload {"operation_id"}: write one order to the ERP."""

    def __init__(
        self,
        store: OrderStore,
        destinations: DestinationLookup,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._destinations = destinations
        self._clock = clock

    def __call__(self, job: ClaimedJob) -> None:
        tenant_id = job.require_tenant()
        operation = self._store.operation(tenant_id, _uuid(job, "operation_id"))
        if operation is None:
            raise PermanentJobError("Order operation not found")
        if operation.status is OperationStatus.SENDING:
            # A worker stopped between sending and recording: the ERP may have the order.
            self._unknown(tenant_id, operation, "The worker stopped while sending")
            return
        if operation.status is not OperationStatus.PENDING:
            return
        order = self._store.get(tenant_id, operation.order_id)
        if order is None or order.status in {OrderStatus.ON_HOLD, OrderStatus.CANCELLED}:
            return  # held orders are sent when staff release them
        destination = self._destinations.for_connection(tenant_id, operation.connection_id)
        if destination is None:
            self._store.finish_operation(
                tenant_id,
                operation.operation_id,
                _BEFORE_SEND,
                OperationOutcome(
                    OperationStatus.FAILED,
                    OrderEventType.ERP_WRITE_FAILED,
                    SYSTEM_SUBJECT,
                    error="The ERP connection is not active or no longer writes orders",
                ),
            )
            return
        if not self._store.begin_send(tenant_id, operation.operation_id):
            return  # another worker is sending it
        try:
            found = destination.writer.create_sales_order(
                system_context(tenant_id), _request(order, operation.external_reference)
            )
        except ErpError as error:
            self._failed(tenant_id, operation, destination, error)
            return
        except Exception as error:  # noqa: BLE001 - anything unexpected after sending
            logger.exception("Order operation %s: unexpected error", operation.operation_id)
            self._unknown(tenant_id, operation, f"Unexpected {type(error).__name__}")
            return
        self._store.finish_operation(
            tenant_id,
            operation.operation_id,
            _SENDING,
            _succeeded(found, OrderEventType.ERP_WRITE_SUCCEEDED, SYSTEM_SUBJECT),
        )
        self._store.refresh_due(tenant_id, operation.connection_id, self._clock() + STATUS_REFRESH)

    def _failed(
        self,
        tenant_id: UUID,
        operation: OrderOperation,
        destination: Destination,
        error: ErpError,
    ) -> None:
        if error.retryable:
            # Nothing reached the ERP (refused connection, rate limit): back to pending, retry.
            self._store.finish_operation(
                tenant_id,
                operation.operation_id,
                _SENDING,
                OperationOutcome(OperationStatus.PENDING, None, SYSTEM_SUBJECT, error=str(error)),
            )
            raise error
        if error.code is ErpErrorCode.UNKNOWN_OUTCOME:
            self._unknown(tenant_id, operation, str(error))
            return
        if error.code is ErpErrorCode.CONFLICT:
            # The reference is already used: if that order is ours, the write succeeded.
            try:
                found = destination.writer.get_order_by_external_reference(
                    system_context(tenant_id), operation.external_reference
                )
            except ErpError:
                found = None
            if found is not None:
                self._store.finish_operation(
                    tenant_id,
                    operation.operation_id,
                    _SENDING,
                    _succeeded(found, OrderEventType.RECONCILED, SYSTEM_SUBJECT),
                )
                return
            self._unknown(tenant_id, operation, str(error))
            return
        self._store.finish_operation(
            tenant_id,
            operation.operation_id,
            _SENDING,
            OperationOutcome(
                OperationStatus.FAILED,
                OrderEventType.ERP_WRITE_FAILED,
                SYSTEM_SUBJECT,
                error=f"{error.code.value}: {error}",
            ),
        )

    def _unknown(self, tenant_id: UUID, operation: OrderOperation, reason: str) -> None:
        self._store.finish_operation(
            tenant_id,
            operation.operation_id,
            _SENDING,
            OperationOutcome(
                OperationStatus.UNKNOWN,
                OrderEventType.ERP_WRITE_UNKNOWN,
                SYSTEM_SUBJECT,
                error=reason,
                reconcile_at=self._clock() + RECONCILE_EVERY,
            ),
        )


class ReconcileOrderOperationHandler:
    """reconcile_order_operation, payload {"operation_id"}: is the uncertain order there?"""

    def __init__(
        self,
        store: OrderStore,
        destinations: DestinationLookup,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._destinations = destinations
        self._clock = clock

    def __call__(self, job: ClaimedJob) -> None:
        tenant_id = job.require_tenant()
        operation = self._store.operation(tenant_id, _uuid(job, "operation_id"))
        if operation is None:
            raise PermanentJobError("Order operation not found")
        if operation.status is not OperationStatus.UNKNOWN:
            return
        destination = self._destinations.for_connection(tenant_id, operation.connection_id)
        abilities = None if destination is None else destination.provider.capabilities.orders
        if destination is None or abilities is None or not abilities.reference_lookup:
            self._attention(tenant_id, operation, "The ERP cannot be searched for this order")
            return
        now = self._clock()
        try:
            found = destination.writer.get_order_by_external_reference(
                system_context(tenant_id), operation.external_reference
            )
        except ErpError as error:
            self._later(tenant_id, operation, now, f"Look-up failed: {error}")
            return
        if found is not None:
            self._store.finish_operation(
                tenant_id,
                operation.operation_id,
                _UNKNOWN,
                _succeeded(found, OrderEventType.RECONCILED, SYSTEM_SUBJECT),
            )
            self._store.refresh_due(tenant_id, operation.connection_id, now + STATUS_REFRESH)
            return
        self._later(tenant_id, operation, now, "Not found in the ERP yet")

    def _later(self, tenant_id: UUID, operation: OrderOperation, now: datetime, why: str) -> None:
        since = operation.unknown_since or operation.updated_at
        if now - since < RECONCILE_WINDOW:
            self._store.finish_operation(
                tenant_id,
                operation.operation_id,
                _UNKNOWN,
                OperationOutcome(
                    OperationStatus.UNKNOWN,
                    None,
                    SYSTEM_SUBJECT,
                    error=why,
                    reconcile_at=now + RECONCILE_EVERY,
                ),
            )
            return
        destination = self._destinations.for_connection(tenant_id, operation.connection_id)
        abilities = None if destination is None else destination.provider.capabilities.orders
        if abilities is not None and abilities.idempotency_key:
            # The ERP itself refuses a second order with this reference: safe to send again.
            self._store.finish_operation(
                tenant_id,
                operation.operation_id,
                _UNKNOWN,
                OperationOutcome(
                    OperationStatus.PENDING,
                    OrderEventType.RETRIED,
                    SYSTEM_SUBJECT,
                    error=why,
                    send_again=True,
                ),
            )
            return
        self._attention(tenant_id, operation, f"{why} after {RECONCILE_WINDOW}")

    def _attention(self, tenant_id: UUID, operation: OrderOperation, why: str) -> None:
        self._store.finish_operation(
            tenant_id,
            operation.operation_id,
            _UNKNOWN,
            OperationOutcome(
                OperationStatus.NEEDS_ATTENTION,
                OrderEventType.NEEDS_ATTENTION,
                SYSTEM_SUBJECT,
                error=why,
            ),
        )


class RefreshOrderStatusHandler:
    """refresh_order_status, payload {"connection_id"}: follow open ERP orders' statuses."""

    def __init__(
        self,
        store: OrderStore,
        destinations: DestinationLookup,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._destinations = destinations
        self._clock = clock

    def __call__(self, job: ClaimedJob) -> None:
        tenant_id = job.require_tenant()
        connection_id = _uuid(job, "connection_id")
        destination = self._destinations.for_connection(tenant_id, connection_id)
        abilities = None if destination is None else destination.provider.capabilities.orders
        if destination is None or abilities is None or not abilities.order_status:
            return  # the connection is gone or does not report statuses: stop following
        context = system_context(tenant_id)
        orders = self._store.open_erp_orders(tenant_id, connection_id)
        for order in orders:
            assert order.erp_order_id is not None
            try:
                found = destination.writer.get_order_status(context, order.erp_order_id)
            except ErpError as error:
                if error.retryable:
                    break  # try them all again next time
                logger.warning("Order %s: ERP status not read (%s)", order.order_number, error.code)
                continue
            target = erp_status(found.state.value)
            if not erp_move_allowed(order.status, target):
                continue
            self._store.change_status(
                tenant_id,
                StatusChange(
                    order_id=order.order_id,
                    expected=order.status,
                    target=target,
                    event_type=OrderEventType.STATUS_CHANGED,
                    source=EventSource.ERP,
                    actor=ERP_ACTOR,
                    detail={"erp_state": found.state.value},
                    notify_customer=target in CUSTOMER_NOTICES,
                ),
            )
        if orders:
            self._store.refresh_due(tenant_id, connection_id, self._clock() + STATUS_REFRESH)


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
