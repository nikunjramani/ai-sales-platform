"""Orders: what an accepted quotation becomes (phase 7, roadmap M6b, ADR 0024).

An order copies the accepted quotation version's lines and totals exactly (the version stays
the signed evidence). One order per quotation, ever (the purchase guard, Q4). Its status says
what the business knows, never more:

  placed -> confirmed -> in_progress -> shipped -> delivered      plus cancelled and on_hold

Where an order is kept (fulfilment):
- local: the business has no ERP that takes orders; staff move the status in the portal;
- erp:   the order is written to the business's ERP once, by an order operation; from then on
         its status follows the ERP (the ERP is the authority, ADR 0003). Staff do not move it.

Order operations (the ERP write) record intent before the call, like the outbox (ADR 0015):
pending -> sending -> succeeded | failed | unknown -> (reconciled) succeeded | needs_attention.
An unknown outcome is never sent again automatically unless the ERP itself refuses
duplicates (OrderCapabilities.idempotency_key); otherwise the reference is looked up for a
while, then staff decide.

Decisions (user, 2026-10-29; Claude where the user delegated, ADR 0024): created automatically
or by staff per business (default staff); stock is re-checked when the order is created and a
shortfall holds the order for staff; no stock reservation; staff may cancel a local order before
it ships (an ERP order is cancelled in the ERP); customers hear about confirmed, shipped,
delivered and cancelled; "track my order" shows the asking contact's own orders only.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sales_platform.platform.domain.validation import require_text

MAX_REASON = 500
MAX_CUSTOMER_ACCOUNT = 140
# An uncertain ERP write is looked up every RECONCILE_EVERY for RECONCILE_WINDOW (Q15).
RECONCILE_EVERY = timedelta(minutes=5)
RECONCILE_WINDOW = timedelta(hours=1)
STATUS_REFRESH = timedelta(minutes=15)  # how often open ERP orders are asked for their status


class OrderStatus(StrEnum):
    PLACED = "placed"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    ON_HOLD = "on_hold"


FINAL_STATUSES = frozenset({OrderStatus.DELIVERED, OrderStatus.CANCELLED})

# Staff moves of a local order (Q8). Cancelling is allowed until the order ships (Q7).
LOCAL_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLACED: frozenset(
        {OrderStatus.CONFIRMED, OrderStatus.ON_HOLD, OrderStatus.CANCELLED}
    ),
    OrderStatus.CONFIRMED: frozenset(
        {OrderStatus.IN_PROGRESS, OrderStatus.SHIPPED, OrderStatus.ON_HOLD, OrderStatus.CANCELLED}
    ),
    OrderStatus.IN_PROGRESS: frozenset(
        {OrderStatus.SHIPPED, OrderStatus.ON_HOLD, OrderStatus.CANCELLED}
    ),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.ON_HOLD: frozenset(
        {
            OrderStatus.PLACED,
            OrderStatus.CONFIRMED,
            OrderStatus.IN_PROGRESS,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

# The statuses customers are told about (Q9); wording in domain.messages.
CUSTOMER_NOTICES = frozenset(
    {
        OrderStatus.CONFIRMED,
        OrderStatus.SHIPPED,
        OrderStatus.DELIVERED,
        OrderStatus.CANCELLED,
    }
)


class Fulfilment(StrEnum):
    LOCAL = "local"
    ERP = "erp"


class OrderCreation(StrEnum):
    """Per business (Q1): staff press "Create order" (default), or it happens on acceptance."""

    STAFF = "staff"
    AUTOMATIC = "automatic"


class OperationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"  # a crash here means the ERP may or may not have the order
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # the ERP certainly refused; staff fix the cause and retry
    UNKNOWN = "unknown"  # may exist: looked up, never resent blindly
    NEEDS_ATTENTION = "needs_attention"  # still unknown after the window: staff decide


FINAL_OPERATION_STATUSES = frozenset(
    {OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.NEEDS_ATTENTION}
)


class OrderEventType(StrEnum):
    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    HELD = "held"  # stock short at creation (Q11)
    ERP_WRITE_SUCCEEDED = "erp_write_succeeded"
    ERP_WRITE_FAILED = "erp_write_failed"
    ERP_WRITE_UNKNOWN = "erp_write_unknown"
    RECONCILED = "reconciled"
    NEEDS_ATTENTION = "needs_attention"
    RESOLVED = "resolved"  # staff: "found in the ERP" or "not in the ERP, send again"
    RETRIED = "retried"
    IGNORED = "ignored"  # an ERP status that is not allowed from the current one


class EventSource(StrEnum):
    SYSTEM = "system"
    STAFF = "staff"
    ERP = "erp"


class Resolution(StrEnum):
    """Staff answer to an order operation that needs attention (both confirmed in the UI)."""

    FOUND = "found"  # it is in the ERP: staff type its ERP order number
    NOT_FOUND = "not_found"  # it is not in the ERP: send it again (same reference)


class OrderNotAllowed(Exception):
    """The order cannot do that now (wrong status, ERP order, already exists...)."""


@dataclass(frozen=True)
class OrderLine:
    line_index: int
    sku: str
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True)
class Order:
    order_id: UUID
    tenant_id: UUID
    order_number: str
    quotation_id: UUID
    version: int
    content_sha256: str
    response_id: UUID | None  # the customer's acceptance
    enquiry_id: UUID
    contact_id: UUID | None  # the customer who accepted, when known (track my order)
    status: OrderStatus
    fulfilment: Fulfilment
    erp_connection_id: UUID | None
    erp_customer: str | None
    erp_order_id: str | None
    currency: str
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    lines: tuple[OrderLine, ...]
    created_by: str
    created_at: datetime
    updated_at: datetime
    hold_reason: str | None = None

    @property
    def open(self) -> bool:
        return self.status not in FINAL_STATUSES


@dataclass(frozen=True)
class OrderEvent:
    event_id: UUID
    order_id: UUID
    type: OrderEventType
    from_status: OrderStatus | None
    to_status: OrderStatus | None
    source: EventSource
    actor: str
    detail: Mapping[str, object]
    occurred_at: datetime


@dataclass(frozen=True)
class OrderOperation:
    operation_id: UUID
    order_id: UUID
    external_reference: str
    connection_id: UUID
    status: OperationStatus
    erp_order_id: str | None
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    unknown_since: datetime | None = None


def order_number(order_id: UUID) -> str:
    """Short human reference derived from the ID, e.g. O-1A2B3C4D (like Q- numbers)."""
    return f"O-{order_id.hex[:8].upper()}"


def external_reference(order_id: UUID) -> str:
    """The stable reference sent to the ERP; generated once, stored before any call."""
    return f"QO-{order_id.hex}"


def order_lines(content: Mapping[str, object]) -> tuple[OrderLine, ...]:
    """The accepted version's lines, copied exactly (strings to Decimal, no rounding)."""
    raw = content.get("lines")
    if not isinstance(raw, list) or not raw:
        raise OrderNotAllowed("The quotation has no lines")
    lines = []
    for item in raw:
        assert isinstance(item, dict)
        lines.append(
            OrderLine(
                line_index=int(item["line_index"]),
                sku=str(item["sku"]),
                description=str(item["description"]),
                quantity=Decimal(str(item["quantity"])),
                unit=str(item["unit"]),
                unit_price=Decimal(str(item["unit_price"])),
                line_total=Decimal(str(item["line_total"])),
            )
        )
    return tuple(lines)


def stock_shortfall(
    lines: Sequence[OrderLine], available: Mapping[str, Decimal | None]
) -> tuple[str, ...]:
    """SKUs whose known stock is below the ordered quantity (Q5, Q11).

    Unknown stock (not counted, or the source did not say) is not a shortfall: the quotation
    was approved knowing it, and the hold is for numbers that say "not enough".
    """
    needed: dict[str, Decimal] = {}
    for line in lines:
        needed[line.sku] = needed.get(line.sku, Decimal(0)) + line.quantity
    return tuple(
        sku
        for sku, quantity in needed.items()
        if (count := available.get(sku)) is not None and count < quantity
    )


def check_local_move(order: Order, target: OrderStatus) -> None:
    if order.fulfilment is not Fulfilment.LOCAL:
        raise OrderNotAllowed("This order's status follows the ERP; change it there")
    if target not in LOCAL_TRANSITIONS[order.status]:
        raise OrderNotAllowed(f"An order that is {order.status.value} cannot become {target.value}")


def erp_status(state: str) -> OrderStatus:
    """The ERP's state (ports.erp.ErpOrderState values) as our status."""
    return {
        "draft": OrderStatus.PLACED,
        "confirmed": OrderStatus.CONFIRMED,
        "in_progress": OrderStatus.IN_PROGRESS,
        "delivered": OrderStatus.DELIVERED,
        "on_hold": OrderStatus.ON_HOLD,
        "cancelled": OrderStatus.CANCELLED,
    }[state]


def erp_move_allowed(current: OrderStatus, target: OrderStatus) -> bool:
    """The ERP decides an ERP order's status, but a final status never reopens."""
    return current != target and current not in FINAL_STATUSES


def clean_reason(reason: str | None, *, required: bool) -> str | None:
    text = " ".join(reason.split()) if isinstance(reason, str) else ""
    if not text:
        if required:
            raise ValueError("Please give a reason")
        return None
    if len(text) > MAX_REASON or not text.isprintable():
        raise ValueError(f"A reason is at most {MAX_REASON} printable characters")
    return text


def clean_customer_account(value: str | None) -> str | None:
    """The ERP customer staff chose (Q3); None means the connection's default."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    require_text(text, "erp_customer", MAX_CUSTOMER_ACCOUNT)
    return text


# --- "track my order" (Q10) -----------------------------------------------------------------

_TRACK_WORDS = frozenset(
    {
        "order status",
        "orders",
        "my order",
        "my orders",
        "track order",
        "track my order",
        "where is my order",
        "where is my order?",
        "status of my order",
    }
)
_ORDER_NUMBER = re.compile(r"^(?:(?:status|track)\s+)?(o-[0-9a-f]{8})\??$")


@dataclass(frozen=True)
class OrderQuery:
    """A customer asked about their orders: all of them, or one by number."""

    order_number: str | None = None


def order_status_command(text: str) -> OrderQuery | None:
    """The whole message is a fixed "where is my order" phrase or an order number.

    Fixed words only (like STOP/START, D15): no AI reads it, so nothing else is mistaken for
    it, and ordinary messages (new enquiries) go their usual way.
    """
    plain = " ".join(text.split()).casefold()
    if plain in _TRACK_WORDS:
        return OrderQuery()
    match = _ORDER_NUMBER.fullmatch(plain)
    if match:
        return OrderQuery(match.group(1).upper())
    return None


STATUS_WORDS: Mapping[OrderStatus, str] = {
    OrderStatus.PLACED: "received",
    OrderStatus.CONFIRMED: "confirmed",
    OrderStatus.IN_PROGRESS: "being prepared",
    OrderStatus.SHIPPED: "shipped",
    OrderStatus.DELIVERED: "delivered",
    OrderStatus.CANCELLED: "cancelled",
    OrderStatus.ON_HOLD: "on hold",
}
