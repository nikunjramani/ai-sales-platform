"""Storing orders, their timeline and their ERP write (phase 7, ADR 0024).

Rules for implementations:
- One order per quotation: create raises OrderExists (with the existing order) when the
  quotation already has one, also when two creates race.
- create, change_status and finish_operation each run in one transaction that also writes the
  order event and queues the follow-up job (the ERP write, a reconciliation, a customer notice,
  the status refresh), so nothing is half done after a crash.
- change_status is compare-and-set on the expected status: None when the order moved meanwhile.
- begin_send is compare-and-set pending -> sending; only its winner may call the ERP.
- Order lines and events are never changed; every call is scoped to one business.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sales_platform.domain.orders import (
    EventSource,
    Fulfilment,
    OperationStatus,
    Order,
    OrderCreation,
    OrderEvent,
    OrderEventType,
    OrderLine,
    OrderOperation,
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
    fulfilment: Fulfilment
    erp_connection_id: UUID | None
    erp_customer: str | None
    currency: str
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    lines: tuple[OrderLine, ...]
    actor: str
    source: EventSource
    external_reference: str | None  # erp orders: the operation is recorded with the order
    send_now: bool  # queue the ERP write at once (not while on hold)


@dataclass(frozen=True)
class OrderFilter:
    status: OrderStatus | None = None
    needs_attention: bool = False  # orders whose ERP write needs staff
    limit: int = 50
    before: datetime | None = None  # paging: created_at of the last order of the last page


@dataclass(frozen=True)
class OrderListItem:
    order: Order
    quotation_number: str
    operation_status: OperationStatus | None


@dataclass(frozen=True)
class StatusChange:
    order_id: UUID
    expected: OrderStatus
    target: OrderStatus
    event_type: OrderEventType
    source: EventSource
    actor: str
    detail: Mapping[str, object] = field(default_factory=dict)
    external_event_id: str | None = None
    hold_reason: str | None = None
    erp_customer: str | None = None  # staff chose another ERP customer when releasing
    notify_customer: bool = False  # queue notify_order for this event
    send_operation: bool = False  # releasing a held ERP order: queue its ERP write now


@dataclass(frozen=True)
class OperationOutcome:
    status: OperationStatus
    event_type: OrderEventType | None  # None: no timeline entry (a retry, a later look-up)
    actor: str
    erp_order_id: str | None = None
    error: str | None = None
    order_status: OrderStatus | None = None  # the ERP's state on success
    reconcile_at: datetime | None = None  # unknown: when to look the reference up
    send_again: bool = False  # back to pending: queue the ERP write again
    detail: Mapping[str, object] = field(default_factory=dict)
    notify_customer: bool = False


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

    def operations(self, tenant_id: UUID, order_id: UUID) -> Sequence[OrderOperation]: ...

    def operation(self, tenant_id: UUID, operation_id: UUID) -> OrderOperation | None: ...

    def for_identity(
        self, tenant_id: UUID, connection_id: UUID, external_id: str
    ) -> Sequence[Order]:
        """Orders of the contact behind this identity (track my order, Q10), newest first."""
        ...

    def open_erp_orders(self, tenant_id: UUID, connection_id: UUID) -> Sequence[Order]: ...

    def change_status(self, tenant_id: UUID, change: StatusChange) -> Order | None: ...

    def begin_send(self, tenant_id: UUID, operation_id: UUID) -> bool: ...

    def finish_operation(
        self,
        tenant_id: UUID,
        operation_id: UUID,
        expected: frozenset[OperationStatus],
        outcome: OperationOutcome,
    ) -> bool:
        """Apply an outcome if the operation is still in an expected status."""
        ...

    def retry_operation(
        self, tenant_id: UUID, operation_id: UUID, actor: str, event_type: OrderEventType
    ) -> bool:
        """failed or needs_attention -> pending, with its job (staff "send again")."""
        ...

    def refresh_due(self, tenant_id: UUID, connection_id: UUID, run_at: datetime) -> None:
        """Queue refresh_order_status for the connection unless one already waits."""
        ...
