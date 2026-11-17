"""Storing orders and their timeline (phase 7, ADR 0024; ADR 0027 removed the ERP write).

Rules for implementations:
- One order per quotation: create raises OrderExists (with the existing order) when the
  quotation already has one, also when two creates race.
- create and change_status each run in one transaction that also writes the order event and
  queues the customer notice, so nothing is half done after a crash.
- change_status is compare-and-set on the expected status: None when the order moved meanwhile.
- Order lines and events are never changed; every call is scoped to one business.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sales_platform.orders.domain.orders import (
    EventSource,
    Order,
    OrderCreation,
    OrderEvent,
    OrderEventType,
    OrderLine,
    OrderStatus,
)


class OrderExists(Exception):
    def __init__(self, order_id: UUID) -> None:
        super().__init__("This quotation already has an order")
        self.order_id = order_id


@dataclass(frozen=True)
class AcceptedQuotation:
    """A quotation and its accepted version, as an order needs it."""

    quotation_id: UUID
    enquiry_id: UUID
    status: str  # the quotation's status; orders only come from "accepted"
    version: int
    content: Mapping[str, object]
    content_sha256: str
    response_id: UUID | None  # the acceptance (None if the quotation is not accepted)


@dataclass(frozen=True)
class NewOrder:
    order_id: UUID
    quotation: AcceptedQuotation
    status: OrderStatus  # placed, or on_hold when stock is short (Q11)
    hold_reason: str | None
    currency: str
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    lines: tuple[OrderLine, ...]
    actor: str
    source: EventSource


@dataclass(frozen=True)
class OrderFilter:
    status: OrderStatus | None = None
    limit: int = 50
    before: datetime | None = None  # paging: created_at of the last order of the last page


@dataclass(frozen=True)
class OrderListItem:
    order: Order
    quotation_number: str


@dataclass(frozen=True)
class StatusChange:
    order_id: UUID
    expected: OrderStatus
    target: OrderStatus
    event_type: OrderEventType
    source: EventSource
    actor: str
    detail: Mapping[str, object] = field(default_factory=dict)
    hold_reason: str | None = None
    notify_customer: bool = False  # queue notify_order for this event


class OrderStore(Protocol):
    def order_creation(self, tenant_id: UUID) -> OrderCreation: ...

    def set_order_creation(self, tenant_id: UUID, value: OrderCreation, actor: str) -> None: ...

    def accepted_quotation(self, tenant_id: UUID, quotation_id: UUID) -> AcceptedQuotation | None:
        """The quotation with its current version and closing acceptance, if any."""
        ...

    def create(self, tenant_id: UUID, new: NewOrder) -> Order: ...

    def get(self, tenant_id: UUID, order_id: UUID) -> Order | None: ...

    def for_quotation(self, tenant_id: UUID, quotation_id: UUID) -> Order | None: ...

    def for_enquiry(self, tenant_id: UUID, enquiry_id: UUID) -> Order | None: ...

    def list(self, tenant_id: UUID, query: OrderFilter) -> Sequence[OrderListItem]: ...

    def events(self, tenant_id: UUID, order_id: UUID) -> Sequence[OrderEvent]: ...

    def event(self, tenant_id: UUID, event_id: UUID) -> OrderEvent | None: ...

    def for_identity(
        self, tenant_id: UUID, connection_id: UUID, external_id: str
    ) -> Sequence[Order]:
        """Orders of the contact behind this identity (track my order, Q10), newest first."""
        ...

    def change_status(self, tenant_id: UUID, change: StatusChange) -> Order | None: ...
