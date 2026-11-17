# Orders
[ADR 0024](../adr/0024-orders.md); local only since [ADR 0027](../adr/0027-remove-erp-integration.md).

When a customer accepts a quotation, the business gets exactly one order: a copy of the accepted
version's lines and totals (number `O-1A2B3C4D`). Staff move its status in the portal, and the
customer is told about the milestones.

| What | Where |
|---|---|
| Domain | `orders/domain/orders.py` (`OrderStatus`, `LOCAL_TRANSITIONS`, `stock_shortfall`, `check_move`, `order_status_command`) |
| Store | `orders/ports/orders.py`, `orders/persistence/orders.py` (`SqlOrderStore`) |
| Service and jobs | `orders/application/orders.py` (`OrderService`, `CreateOrderHandler`, `OrderNotificationHandler`, `OrderStatusRoute`) |
| API | `orders/api/orders.py` (portal); `GET /api/v1/enquiries/{id}/order` (API customers) |
| Portal | `portal/src/features/orders/` |

**Creating:** automatic on acceptance (when the business chose it, `tenants.order_creation`) or
"Create order" on an accepted quotation. Stock is re-checked; a shortfall puts the order
`on_hold` with the reason. Needs `orders:manage`; the setting needs `tenant:manage`.

**Status:** `placed → confirmed → in_progress → shipped → delivered`; cancel until shipped; hold
and release. Cancelling and holding need a reason.

**Customers:** confirmed, shipped, delivered and cancelled queue `notify_order` (outbox key
`order:<event>`). "Where is my order?" or an order number answers only the asking contact's orders.

**Boundaries:** forced RLS on all three tables; one order per quotation (database); lines and
events are immutable for every role.

**Tests:** `tests/unit/orders/`, `tests/integration/orders/workflows/test_orders.py`.

**Limits:** no PO number, address, delivery date, reservation, partial delivery or payments.
