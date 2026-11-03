"""Portal routes for orders (phase 7, ADR 0024), session cookie + CSRF as usual.

GET  /api/v1/tenants/{t}/orders?status=&needs_attention=&before=&limit=   newest first
GET  /api/v1/tenants/{t}/orders/{id}                 lines, timeline, ERP write
POST /api/v1/tenants/{t}/orders/{id}/status          {status, reason?} local orders; a held
                                                     ERP order: placed (release) or cancelled
POST /api/v1/tenants/{t}/orders/{id}/operations/{op}/resolve
                                                     {resolution: found|not_found, erp_order_id?}
GET  /api/v1/tenants/{t}/quotations/{id}/order       the quotation's order (404: none yet)
POST /api/v1/tenants/{t}/quotations/{id}/order       201 {erp_customer?}; 409 exists/not allowed
GET|PUT /api/v1/tenants/{t}/order-settings           {order_creation: staff|automatic}
Decimals travel as strings. A non-member gets 404, like an unknown business.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.api.auth import PortalAuth
from sales_platform.application.orders import (
    OrderDetail,
    OrderNotFound,
    OrderService,
    OrderUnavailable,
)
from sales_platform.domain.orders import (
    Order,
    OrderCreation,
    OrderEvent,
    OrderNotAllowed,
    OrderOperation,
    OrderStatus,
    Resolution,
)
from sales_platform.domain.quotations import quotation_number
from sales_platform.domain.tenancy import PermissionDenied, TenantContext
from sales_platform.ports.orders import OrderExists, OrderFilter, OrderListItem


class OrderLineOut(BaseModel):
    line_index: int
    sku: str
    description: str
    quantity: str
    unit: str
    unit_price: str
    line_total: str


class OrderOut(BaseModel):
    order_id: UUID
    order_number: str
    quotation_id: UUID
    quotation_number: str
    version: int
    status: OrderStatus
    hold_reason: str | None
    fulfilment: Literal["local", "erp"]
    erp_customer: str | None
    erp_order_id: str | None
    currency: str
    subtotal: str
    tax: str
    total: str
    lines: list[OrderLineOut]
    created_by: str
    created_at: datetime
    updated_at: datetime
    operation_status: str | None = None


class OrderEventOut(BaseModel):
    event_id: UUID
    type: str
    from_status: str | None
    to_status: str | None
    source: str
    actor: str
    detail: dict[str, object]
    occurred_at: datetime


class OrderOperationOut(BaseModel):
    operation_id: UUID
    external_reference: str
    status: str
    erp_order_id: str | None
    attempts: int
    last_error: str | None
    unknown_since: datetime | None
    updated_at: datetime


class OrderDetailOut(OrderOut):
    events: list[OrderEventOut]
    operations: list[OrderOperationOut]


class CreateOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    erp_customer: str | None = Field(default=None, max_length=140)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: OrderStatus
    reason: str | None = Field(default=None, max_length=500)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Resolution
    erp_order_id: str | None = Field(default=None, max_length=140)


class OrderSettingsIO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_creation: OrderCreation


def order_out(order: Order, operation_status: str | None = None) -> OrderOut:
    return OrderOut(
        order_id=order.order_id,
        order_number=order.order_number,
        quotation_id=order.quotation_id,
        quotation_number=quotation_number(order.quotation_id),
        version=order.version,
        status=order.status,
        hold_reason=order.hold_reason,
        fulfilment=order.fulfilment.value,
        erp_customer=order.erp_customer,
        erp_order_id=order.erp_order_id,
        currency=order.currency,
        subtotal=str(order.subtotal),
        tax=str(order.tax),
        total=str(order.total),
        lines=[
            OrderLineOut(
                line_index=line.line_index,
                sku=line.sku,
                description=line.description,
                quantity=str(line.quantity),
                unit=line.unit,
                unit_price=str(line.unit_price),
                line_total=str(line.line_total),
            )
            for line in order.lines
        ],
        created_by=order.created_by,
        created_at=order.created_at,
        updated_at=order.updated_at,
        operation_status=operation_status,
    )


def _event(event: OrderEvent) -> OrderEventOut:
    return OrderEventOut(
        event_id=event.event_id,
        type=event.type.value,
        from_status=None if event.from_status is None else event.from_status.value,
        to_status=None if event.to_status is None else event.to_status.value,
        source=event.source.value,
        actor=event.actor,
        detail=dict(event.detail),
        occurred_at=event.occurred_at,
    )


def _operation(operation: OrderOperation) -> OrderOperationOut:
    return OrderOperationOut(
        operation_id=operation.operation_id,
        external_reference=operation.external_reference,
        status=operation.status.value,
        erp_order_id=operation.erp_order_id,
        attempts=operation.attempts,
        last_error=operation.last_error,
        unknown_since=operation.unknown_since,
        updated_at=operation.updated_at,
    )


def _detail(detail: OrderDetail) -> OrderDetailOut:
    latest = detail.operations[-1].status.value if detail.operations else None
    return OrderDetailOut(
        **order_out(detail.order, latest).model_dump(),
        events=[_event(event) for event in detail.events],
        operations=[_operation(operation) for operation in detail.operations],
    )


def _item(item: OrderListItem) -> OrderOut:
    return order_out(
        item.order, None if item.operation_status is None else item.operation_status.value
    )


def guarded[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except PermissionDenied as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error) or "Not permitted") from None
    except OrderNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    except OrderExists as error:
        detail = {"code": "order_exists", "message": str(error), "order_id": str(error.order_id)}
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from None
    except OrderNotAllowed as error:
        detail = {"code": "not_allowed", "message": str(error)}
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from None
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except (OrderUnavailable, SQLAlchemyError):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Try again later") from None


def build_order_router(auth: PortalAuth, orders: OrderService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["portal orders"])
    Context = Annotated[TenantContext, Depends(auth.tenant_context())]

    @router.get("/orders", response_model=list[OrderOut])
    def list_orders(
        context: Context,
        status_filter: Annotated[OrderStatus | None, Query(alias="status")] = None,
        needs_attention: bool = False,
        before: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[OrderOut]:
        query = OrderFilter(status_filter, needs_attention, limit, before)
        return [_item(item) for item in guarded(lambda: orders.list(context, query))]

    @router.get("/orders/{order_id}", response_model=OrderDetailOut, responses={404: {}})
    def order_detail(order_id: UUID, context: Context) -> OrderDetailOut:
        return _detail(guarded(lambda: orders.detail(context, order_id)))

    @router.post(
        "/orders/{order_id}/status",
        response_model=OrderOut,
        responses={403: {}, 404: {}, 409: {}, 422: {}},
    )
    def move(order_id: UUID, body: StatusIn, context: Context) -> OrderOut:
        return order_out(guarded(lambda: orders.move(context, order_id, body.status, body.reason)))

    @router.post(
        "/orders/{order_id}/operations/{operation_id}/resolve",
        response_model=OrderOut,
        responses={403: {}, 404: {}, 409: {}, 422: {}},
    )
    def resolve(order_id: UUID, operation_id: UUID, body: ResolveIn, context: Context) -> OrderOut:
        return order_out(
            guarded(
                lambda: orders.resolve(
                    context, order_id, operation_id, body.resolution, body.erp_order_id
                )
            )
        )

    @router.get("/quotations/{quotation_id}/order", response_model=OrderOut, responses={404: {}})
    def quotation_order(quotation_id: UUID, context: Context) -> OrderOut:
        found = guarded(lambda: orders.for_quotation(context, quotation_id))
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No order yet")
        return order_out(found)

    @router.post(
        "/quotations/{quotation_id}/order",
        response_model=OrderOut,
        status_code=status.HTTP_201_CREATED,
        responses={403: {}, 404: {}, 409: {}, 503: {}},
    )
    def create_order(quotation_id: UUID, body: CreateOrderIn, context: Context) -> OrderOut:
        return order_out(guarded(lambda: orders.create(context, quotation_id, body.erp_customer)))

    @router.get("/order-settings", response_model=OrderSettingsIO)
    def settings(context: Context) -> OrderSettingsIO:
        return OrderSettingsIO(order_creation=guarded(lambda: orders.creation(context)))

    @router.put("/order-settings", response_model=OrderSettingsIO, responses={403: {}})
    def set_settings(body: OrderSettingsIO, context: Context) -> OrderSettingsIO:
        guarded(lambda: orders.set_creation(context, body.order_creation))
        return body

    return router
