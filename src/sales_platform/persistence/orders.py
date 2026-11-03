"""Orders in PostgreSQL (phase 7, ADR 0024). Both the API and the worker role use SqlOrderStore.

Every write runs in one tenant transaction together with its order event and follow-up job:
- create: order, lines, the created (and held) event, and for ERP orders the operation row
  with its stable reference, plus the erp_order_operation job unless the order is on hold.
  A second order for the same quotation hits uq_orders_quotation: OrderExists.
- change_status: compare-and-set on the expected status, event, and the notify_order job
  for statuses customers hear about.
- begin_send / finish_operation: compare-and-set on the operation's status (intent before
  call); an unknown outcome queues the reconciliation; a success queues the status refresh.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, Row, and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from sales_platform.domain.jobs import JobKind, JobStatus
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
    order_number,
)
from sales_platform.domain.quotations import QuotationStatus, quotation_number
from sales_platform.persistence.jobs import queue_job
from sales_platform.persistence.models import (
    ContactIdentity,
    Enquiry,
    Job,
    OrderEventRow,
    OrderLineRow,
    OrderOperationRow,
    OrderRow,
    Quotation,
    QuotationResponse,
    QuotationVersion,
)
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.orders import (
    AcceptedQuotation,
    NewOrder,
    OperationOutcome,
    OrderExists,
    OrderFilter,
    OrderListItem,
    StatusChange,
)

_O = OrderRow
_L = OrderLineRow
_E = OrderEventRow
_OP = OrderOperationRow
_ATTENTION = (OperationStatus.FAILED.value, OperationStatus.NEEDS_ATTENTION.value)


def _order(row: Row[Any], lines: Sequence[Row[Any]]) -> Order:
    return Order(
        order_id=row.id,
        tenant_id=row.tenant_id,
        order_number=row.order_number,
        quotation_id=row.quotation_id,
        version=row.version,
        content_sha256=row.content_sha256,
        response_id=row.response_id,
        enquiry_id=row.enquiry_id,
        contact_id=row.contact_id,
        status=OrderStatus(row.status),
        fulfilment=Fulfilment(row.fulfilment),
        erp_connection_id=row.erp_connection_id,
        erp_customer=row.erp_customer,
        erp_order_id=row.erp_order_id,
        currency=row.currency,
        subtotal=row.subtotal,
        tax=row.tax,
        total=row.total,
        lines=tuple(
            OrderLine(
                line.line_index,
                line.sku,
                line.description,
                line.quantity,
                line.unit,
                line.unit_price,
                line.line_total,
            )
            for line in lines
        ),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        hold_reason=row.hold_reason,
    )


def _event(row: Row[Any]) -> OrderEvent:
    return OrderEvent(
        event_id=row.id,
        order_id=row.order_id,
        type=OrderEventType(row.type),
        from_status=None if row.from_status is None else OrderStatus(row.from_status),
        to_status=None if row.to_status is None else OrderStatus(row.to_status),
        source=EventSource(row.source),
        actor=row.actor,
        detail=dict(row.detail),
        occurred_at=row.occurred_at,
    )


def _operation(row: Row[Any]) -> OrderOperation:
    return OrderOperation(
        operation_id=row.id,
        order_id=row.order_id,
        external_reference=row.external_reference,
        connection_id=row.connection_id,
        status=OperationStatus(row.status),
        erp_order_id=row.erp_order_id,
        attempts=row.attempts,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        unknown_since=row.unknown_since,
    )


def _order_select() -> Any:
    return (
        select(_O, Quotation.enquiry_id, Enquiry.contact_id)
        .join(Quotation, and_(Quotation.tenant_id == _O.tenant_id, Quotation.id == _O.quotation_id))
        .join(Enquiry, and_(Enquiry.tenant_id == _O.tenant_id, Enquiry.id == Quotation.enquiry_id))
    )


class SqlOrderStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- settings and sources --------------------------------------------------------------
    def order_creation(self, tenant_id: UUID) -> OrderCreation:
        with tenant_transaction(self._engine, tenant_id) as connection:
            value = connection.execute(
                text("SELECT order_creation FROM tenants WHERE id = :t"), {"t": tenant_id}
            ).scalar_one()
        return OrderCreation(value)

    def set_order_creation(self, tenant_id: UUID, value: OrderCreation, actor: str) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                text("UPDATE tenants SET order_creation = :v WHERE id = :t"),
                {"v": value.value, "t": tenant_id},
            )
            connection.execute(
                text(
                    "INSERT INTO admin_events (id, tenant_id, actor_subject, action, details) "
                    "VALUES (:id, :t, :a, 'order_settings_changed', "
                    "jsonb_build_object('order_creation', CAST(:v AS text)))"
                ),
                {"id": uuid4(), "t": tenant_id, "a": actor, "v": value.value},
            )

    def accepted_quotation(self, tenant_id: UUID, quotation_id: UUID) -> AcceptedQuotation | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(
                    Quotation.id,
                    Quotation.enquiry_id,
                    Quotation.status,
                    QuotationVersion.version,
                    QuotationVersion.content,
                    QuotationVersion.content_sha256,
                    QuotationResponse.id.label("response_id"),
                )
                .join(
                    QuotationVersion,
                    and_(
                        QuotationVersion.tenant_id == Quotation.tenant_id,
                        QuotationVersion.quotation_id == Quotation.id,
                        QuotationVersion.version == Quotation.current_version,
                    ),
                )
                .outerjoin(
                    QuotationResponse,
                    and_(
                        QuotationResponse.tenant_id == Quotation.tenant_id,
                        QuotationResponse.quotation_id == Quotation.id,
                        QuotationResponse.version == QuotationVersion.version,
                        QuotationResponse.outcome == "accepted",
                    ),
                )
                .where(Quotation.tenant_id == tenant_id, Quotation.id == quotation_id)
            ).one_or_none()
        if row is None:
            return None
        return AcceptedQuotation(
            quotation_id=row.id,
            enquiry_id=row.enquiry_id,
            status=row.status,
            version=row.version,
            content=dict(row.content),
            content_sha256=row.content_sha256,
            response_id=row.response_id if row.status == QuotationStatus.ACCEPTED.value else None,
        )

    # --- orders ----------------------------------------------------------------------------
    def create(self, tenant_id: UUID, new: NewOrder) -> Order:
        quotation = new.quotation
        try:
            with tenant_transaction(self._engine, tenant_id) as connection:
                connection.execute(
                    insert(_O).values(
                        id=new.order_id,
                        tenant_id=tenant_id,
                        order_number=order_number(new.order_id),
                        quotation_id=quotation.quotation_id,
                        version=quotation.version,
                        content_sha256=quotation.content_sha256,
                        response_id=quotation.response_id,
                        status=new.status.value,
                        hold_reason=new.hold_reason,
                        fulfilment=new.fulfilment.value,
                        erp_connection_id=new.erp_connection_id,
                        erp_customer=new.erp_customer,
                        currency=new.currency,
                        subtotal=new.subtotal,
                        tax=new.tax,
                        total=new.total,
                        created_by=new.actor,
                    )
                )
                connection.execute(
                    insert(_L),
                    [
                        {
                            "tenant_id": tenant_id,
                            "order_id": new.order_id,
                            "line_index": line.line_index,
                            "sku": line.sku,
                            "description": line.description[:500],
                            "quantity": line.quantity,
                            "unit": line.unit,
                            "unit_price": line.unit_price,
                            "line_total": line.line_total,
                        }
                        for line in new.lines
                    ],
                )
                detail: dict[str, object] = {
                    "quotation": quotation_number(quotation.quotation_id),
                    "version": quotation.version,
                    "fulfilment": new.fulfilment.value,
                }
                _add_event(
                    connection, tenant_id, new.order_id, OrderEventType.CREATED, None,
                    OrderStatus.PLACED, new.source, new.actor, detail,
                )  # fmt: skip
                if new.status is OrderStatus.ON_HOLD:
                    _add_event(
                        connection, tenant_id, new.order_id, OrderEventType.HELD,
                        OrderStatus.PLACED, OrderStatus.ON_HOLD, EventSource.SYSTEM,
                        new.actor, {"reason": new.hold_reason},
                    )  # fmt: skip
                if new.external_reference is not None:
                    assert new.erp_connection_id is not None
                    operation_id = uuid4()
                    connection.execute(
                        insert(_OP).values(
                            id=operation_id,
                            tenant_id=tenant_id,
                            order_id=new.order_id,
                            kind="create_sales_order",
                            external_reference=new.external_reference,
                            connection_id=new.erp_connection_id,
                            status=OperationStatus.PENDING.value,
                        )
                    )
                    if new.send_now:
                        _queue_operation(connection, tenant_id, operation_id)
        except IntegrityError as error:
            if "uq_orders_quotation" not in str(error.orig):
                raise
            existing = self.for_quotation(tenant_id, quotation.quotation_id)
            assert existing is not None
            raise OrderExists(existing.order_id) from None
        created = self.get(tenant_id, new.order_id)
        assert created is not None
        return created

    def get(self, tenant_id: UUID, order_id: UUID) -> Order | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return _load(connection, tenant_id, _O.id == order_id)

    def for_quotation(self, tenant_id: UUID, quotation_id: UUID) -> Order | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return _load(connection, tenant_id, _O.quotation_id == quotation_id)

    def for_enquiry(self, tenant_id: UUID, enquiry_id: UUID) -> Order | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            return _load(connection, tenant_id, Quotation.enquiry_id == enquiry_id)

    def list(self, tenant_id: UUID, query: OrderFilter) -> Sequence[OrderListItem]:
        latest_operation = (
            select(_OP.status)
            .where(_OP.tenant_id == _O.tenant_id, _OP.order_id == _O.id)
            .order_by(_OP.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            _order_select()
            .add_columns(latest_operation.label("operation_status"))
            .where(_O.tenant_id == tenant_id)
        )
        if query.status is not None:
            statement = statement.where(_O.status == query.status.value)
        if query.needs_attention:
            statement = statement.where(latest_operation.in_(_ATTENTION))
        if query.before is not None:
            statement = statement.where(_O.created_at < query.before)
        statement = statement.order_by(_O.created_at.desc()).limit(query.limit)
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(statement).all()
            lines = _lines(connection, tenant_id, [row.id for row in rows])
        return [
            OrderListItem(
                _order(row, lines.get(row.id, [])),
                quotation_number(row.quotation_id),
                None if row.operation_status is None else OperationStatus(row.operation_status),
            )
            for row in rows
        ]

    def events(self, tenant_id: UUID, order_id: UUID) -> Sequence[OrderEvent]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                select(_E)
                .where(_E.tenant_id == tenant_id, _E.order_id == order_id)
                .order_by(_E.occurred_at, _E.created_at)
            ).all()
        return [_event(row) for row in rows]

    def event(self, tenant_id: UUID, event_id: UUID) -> OrderEvent | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(_E).where(_E.tenant_id == tenant_id, _E.id == event_id)
            ).one_or_none()
        return None if row is None else _event(row)

    def operations(self, tenant_id: UUID, order_id: UUID) -> Sequence[OrderOperation]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                select(_OP)
                .where(_OP.tenant_id == tenant_id, _OP.order_id == order_id)
                .order_by(_OP.created_at)
            ).all()
        return [_operation(row) for row in rows]

    def operation(self, tenant_id: UUID, operation_id: UUID) -> OrderOperation | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(_OP).where(_OP.tenant_id == tenant_id, _OP.id == operation_id)
            ).one_or_none()
        return None if row is None else _operation(row)

    def for_identity(
        self, tenant_id: UUID, connection_id: UUID, external_id: str
    ) -> Sequence[Order]:
        identity_contact = (
            select(ContactIdentity.contact_id)
            .where(
                ContactIdentity.tenant_id == tenant_id,
                ContactIdentity.connection_ref == connection_id,
                ContactIdentity.external_id == external_id,
            )
            .scalar_subquery()
        )
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                _order_select()
                .where(_O.tenant_id == tenant_id, Enquiry.contact_id == identity_contact)
                .order_by(_O.created_at.desc())
                .limit(20)
            ).all()
            lines = _lines(connection, tenant_id, [row.id for row in rows])
        return [_order(row, lines.get(row.id, [])) for row in rows]

    def open_erp_orders(self, tenant_id: UUID, connection_id: UUID) -> Sequence[Order]:
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(
                _order_select().where(
                    _O.tenant_id == tenant_id,
                    _O.erp_connection_id == connection_id,
                    _O.erp_order_id.is_not(None),
                    _O.status.not_in([OrderStatus.DELIVERED.value, OrderStatus.CANCELLED.value]),
                )
            ).all()
        return [_order(row, []) for row in rows]

    def change_status(self, tenant_id: UUID, change: StatusChange) -> Order | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            values: dict[str, object] = {
                "status": change.target.value,
                "hold_reason": change.hold_reason,
                "updated_at": func.now(),
            }
            if change.erp_customer is not None:
                values["erp_customer"] = change.erp_customer
            moved = connection.execute(
                update(_O)
                .where(
                    _O.tenant_id == tenant_id,
                    _O.id == change.order_id,
                    _O.status == change.expected.value,
                )
                .values(**values)
            ).rowcount
            if not moved:
                return None
            event_id = _add_event(
                connection, tenant_id, change.order_id, change.event_type, change.expected,
                change.target, change.source, change.actor, change.detail,
                change.external_event_id,
            )  # fmt: skip
            if change.notify_customer:
                queue_job(
                    connection, tenant_id, JobKind.NOTIFY_ORDER, {"event_id": str(event_id)},
                    uuid4(),
                )  # fmt: skip
            if change.send_operation:
                pending = connection.execute(
                    select(_OP.id).where(
                        _OP.tenant_id == tenant_id,
                        _OP.order_id == change.order_id,
                        _OP.status == OperationStatus.PENDING.value,
                    )
                ).scalar_one_or_none()
                if pending is not None:
                    _queue_operation(connection, tenant_id, pending)
        return self.get(tenant_id, change.order_id)

    # --- the ERP write ---------------------------------------------------------------------
    def begin_send(self, tenant_id: UUID, operation_id: UUID) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            moved = connection.execute(
                update(_OP)
                .where(
                    _OP.tenant_id == tenant_id,
                    _OP.id == operation_id,
                    _OP.status == OperationStatus.PENDING.value,
                )
                .values(
                    status=OperationStatus.SENDING.value,
                    attempts=_OP.attempts + 1,
                    updated_at=func.now(),
                )
            ).rowcount
        return bool(moved)

    def finish_operation(
        self,
        tenant_id: UUID,
        operation_id: UUID,
        expected: frozenset[OperationStatus],
        outcome: OperationOutcome,
    ) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            values: dict[str, object] = {
                "status": outcome.status.value,
                "last_error": None if outcome.error is None else outcome.error[:500],
                "updated_at": func.now(),
            }
            if outcome.erp_order_id is not None:
                values["erp_order_id"] = outcome.erp_order_id
            if outcome.status is OperationStatus.UNKNOWN:
                values["unknown_since"] = func.coalesce(_OP.unknown_since, func.now())
            row = connection.execute(
                update(_OP)
                .where(
                    _OP.tenant_id == tenant_id,
                    _OP.id == operation_id,
                    _OP.status.in_([item.value for item in expected]),
                )
                .values(**values)
                .returning(_OP.order_id, _OP.connection_id)
            ).one_or_none()
            if row is None:
                return False
            current = connection.execute(
                select(_O.status).where(_O.tenant_id == tenant_id, _O.id == row.order_id)
            ).scalar_one()
            target = outcome.order_status
            order_values: dict[str, object] = {"updated_at": func.now()}
            if outcome.erp_order_id is not None:
                order_values["erp_order_id"] = outcome.erp_order_id
            moved = target is not None and target.value != current
            if moved:
                assert target is not None
                order_values["status"] = target.value
            connection.execute(
                update(_O)
                .where(_O.tenant_id == tenant_id, _O.id == row.order_id)
                .values(**order_values)
            )
            event_id = None
            if outcome.event_type is not None:
                event_id = _add_event(
                    connection, tenant_id, row.order_id, outcome.event_type,
                    OrderStatus(current) if moved else None, target if moved else None,
                    EventSource.SYSTEM, outcome.actor,
                    {**outcome.detail, **({"error": outcome.error} if outcome.error else {})},
                )  # fmt: skip
            if outcome.send_again:
                _queue_operation(connection, tenant_id, operation_id)
            if moved and outcome.notify_customer and event_id is not None:
                queue_job(
                    connection, tenant_id, JobKind.NOTIFY_ORDER, {"event_id": str(event_id)},
                    uuid4(),
                )  # fmt: skip
            if outcome.reconcile_at is not None:
                queue_job(
                    connection, tenant_id, JobKind.RECONCILE_ORDER_OPERATION,
                    {"operation_id": str(operation_id)}, uuid4(), run_at=outcome.reconcile_at,
                )  # fmt: skip
        return True

    def retry_operation(
        self, tenant_id: UUID, operation_id: UUID, actor: str, event_type: OrderEventType
    ) -> bool:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                update(_OP)
                .where(
                    _OP.tenant_id == tenant_id,
                    _OP.id == operation_id,
                    _OP.status.in_(_ATTENTION),
                )
                .values(
                    status=OperationStatus.PENDING.value,
                    unknown_since=None,
                    updated_at=func.now(),
                )
                .returning(_OP.order_id)
            ).one_or_none()
            if row is None:
                return False
            _add_event(
                connection, tenant_id, row.order_id, event_type, None, None, EventSource.STAFF,
                actor, {"action": "send again"},
            )  # fmt: skip
            _queue_operation(connection, tenant_id, operation_id)
        return True

    def refresh_due(self, tenant_id: UUID, connection_id: UUID, run_at: datetime) -> None:
        payload = {"connection_id": str(connection_id)}
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"order-refresh:{tenant_id}:{connection_id}"},
            )
            waiting = connection.execute(
                select(func.count()).where(
                    Job.tenant_id == tenant_id,
                    Job.kind == JobKind.REFRESH_ORDER_STATUS.value,
                    Job.status == JobStatus.PENDING.value,
                    Job.payload["connection_id"].astext == str(connection_id),
                )
            ).scalar_one()
            if not waiting:
                queue_job(
                    connection, tenant_id, JobKind.REFRESH_ORDER_STATUS, payload, uuid4(),
                    run_at=run_at,
                )  # fmt: skip


def _load(connection: Connection, tenant_id: UUID, condition: Any) -> Order | None:
    row = connection.execute(
        _order_select().where(_O.tenant_id == tenant_id, condition)
    ).one_or_none()
    if row is None:
        return None
    return _order(row, _lines(connection, tenant_id, [row.id]).get(row.id, []))


def _lines(
    connection: Connection, tenant_id: UUID, order_ids: Sequence[UUID]
) -> dict[UUID, list[Row[Any]]]:
    if not order_ids:
        return {}
    result: dict[UUID, list[Row[Any]]] = {}
    for row in connection.execute(
        select(_L)
        .where(_L.tenant_id == tenant_id, _L.order_id.in_(list(order_ids)))
        .order_by(_L.order_id, _L.line_index)
    ):
        result.setdefault(row.order_id, []).append(row)
    return result


def _add_event(
    connection: Connection,
    tenant_id: UUID,
    order_id: UUID,
    event_type: OrderEventType,
    from_status: OrderStatus | None,
    to_status: OrderStatus | None,
    source: EventSource,
    actor: str,
    detail: Mapping[str, object],
    external_event_id: str | None = None,
) -> UUID:
    event_id = uuid4()
    connection.execute(
        insert(_E).values(
            id=event_id,
            tenant_id=tenant_id,
            order_id=order_id,
            type=event_type.value,
            from_status=None if from_status is None else from_status.value,
            to_status=None if to_status is None else to_status.value,
            source=source.value,
            actor=actor[:200],
            detail=dict(detail),
            external_event_id=external_event_id,
        )
    )
    return event_id


def _queue_operation(connection: Connection, tenant_id: UUID, operation_id: UUID) -> None:
    queue_job(
        connection,
        tenant_id,
        JobKind.ERP_ORDER_OPERATION,
        {"operation_id": str(operation_id)},
        uuid4(),
    )
