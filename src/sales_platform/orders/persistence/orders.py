"""Orders in PostgreSQL (phase 7, ADR 0024). Both the API and the worker role use SqlOrderStore.

Every write runs in one tenant transaction together with its order event and follow-up job:
- create: order, lines and the created (and held) event. A second order for the same
  quotation hits uq_orders_quotation: OrderExists.
- change_status: compare-and-set on the expected status, event, and the notify_order job
  for statuses customers hear about.
"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, Row, and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from sales_platform.conversations.persistence.tables import ContactIdentity
from sales_platform.enquiries.persistence.tables import Enquiry
from sales_platform.orders.domain.orders import (
    EventSource,
    Order,
    OrderCreation,
    OrderEvent,
    OrderEventType,
    OrderLine,
    OrderStatus,
    order_number,
)
from sales_platform.orders.persistence.tables import (
    OrderEventRow,
    OrderLineRow,
    OrderRow,
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
from sales_platform.platform.persistence.jobs import queue_job
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.quotations.domain.quotations import QuotationStatus, quotation_number
from sales_platform.quotations.persistence.tables import (
    Quotation,
    QuotationResponse,
    QuotationVersion,
)

_O = OrderRow
_L = OrderLineRow
_E = OrderEventRow


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
        statement = _order_select().where(_O.tenant_id == tenant_id)
        if query.status is not None:
            statement = statement.where(_O.status == query.status.value)
        if query.before is not None:
            statement = statement.where(_O.created_at < query.before)
        statement = statement.order_by(_O.created_at.desc()).limit(query.limit)
        with tenant_transaction(self._engine, tenant_id) as connection:
            rows = connection.execute(statement).all()
            lines = _lines(connection, tenant_id, [row.id for row in rows])
        return [
            OrderListItem(_order(row, lines.get(row.id, [])), quotation_number(row.quotation_id))
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

    def change_status(self, tenant_id: UUID, change: StatusChange) -> Order | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            values: dict[str, object] = {
                "status": change.target.value,
                "hold_reason": change.hold_reason,
                "updated_at": func.now(),
            }
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
            )  # fmt: skip
            if change.notify_customer:
                queue_job(
                    connection, tenant_id, JobKind.NOTIFY_ORDER, {"event_id": str(event_id)},
                    uuid4(),
                )  # fmt: skip
        return self.get(tenant_id, change.order_id)


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
        )
    )
    return event_id
