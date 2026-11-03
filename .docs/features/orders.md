# Orders (phase 7, roadmap M6b)
**Status and milestone:** implemented on `feat/phase-6-7-erp-and-orders`; verified with fakes, PostgreSQL and the ERPNext sandbox (synthetic data). [ADR 0024](../adr/0024-orders.md).

**Business purpose:** when a customer accepts a quotation, the business gets exactly one order. The order is kept here or written once to its ERP, its status is followed, and the customer is told about the milestones.

**Terms in simple language**
- *Order:* a copy of the accepted quotation version's lines and totals, with a status. Its number looks like `O-1A2B3C4D`.
- *Local order:* kept by us; staff move its status in the portal.
- *ERP order:* written to the business's ERP (ERPNext) once; from then on its status follows the ERP.
- *Order operation:* the record of that one ERP write, made **before** calling the ERP, with a stable reference `QO-<order id>` that the ERP stores (`po_no` in ERPNext).
- *Unknown outcome:* we sent the order but did not hear back (a timeout). The ERP may or may not have it, so we look it up instead of sending it again.
- *Needs attention:* still not found after an hour of looking. A person checks the ERP and decides.

**Actual source files/symbols**

| What | Where |
|---|---|
| Domain | `domain/orders.py` (`OrderStatus`, `LOCAL_TRANSITIONS`, `stock_shortfall`, `order_status_command`, `erp_status`) |
| Writer port | `ports/erp.py` (`OrderWriter`, `SalesOrderRequest`, `ErpOrder`) |
| Store port and SQL store | `ports/orders.py`, `persistence/orders.py` (`SqlOrderStore`) |
| Service, job handlers and route | `application/orders.py` (`OrderService`, `CreateOrderHandler`, `ErpOrderOperationHandler`, `ReconcileOrderOperationHandler`, `RefreshOrderStatusHandler`, `OrderNotificationHandler`, `OrderStatusRoute`, `OrderDestinations`) |
| Writers | `adapters/providers/erpnext/orders.py`; simulated `adapters/erp/simulated_orders.py` |
| API | `api/orders.py` (portal); `GET /api/v1/enquiries/{id}/order` in `api/enquiries.py` |
| Portal | `portal/src/features/orders/` |
| Hooks | `persistence/acceptance.py` queues `create_order`; `application/customer_inbox.py` and `application/chat.py` recognise order questions |
| Migration | `0015_orders` |

**Entry point and caller:**
- Automatic: an acceptance through the API, chat or a button queues `create_order` in the same transaction. The order is created if the business chose automatic.
- Staff: "Create order" on an accepted quotation (`POST /quotations/{id}/order`), also for quotations accepted earlier.
- Track my order: a customer message that is only "where is my order?" (or similar) or an order number.

**Inputs and validation:**
- Only an accepted quotation, and only once.
- Creating, moving and resolving need `orders:manage`; the creation setting needs `tenant:manage`.
- A reason is required to cancel or hold.
- The ERP customer is optional; when empty, the connection's default is used.

**Execution steps and state changes**
1. Create:
   - read the accepted version;
   - re-check stock (shortfall → `on_hold` with the reason);
   - choose the destination (the active orders connection whose provider can create orders and is set to write them; otherwise local);
   - in one transaction: order, lines, events, and for ERP orders the operation and its job (not while on hold).
2. ERP write (worker), with intent before call: see ADR 0024 for every outcome.
3. Status: local orders move by staff along `placed → confirmed → in_progress → shipped → delivered` (cancel until shipped; hold and release). ERP orders follow the ERP every 15 minutes.
4. Customer notices: confirmed, shipped, delivered and cancelled queue `notify_order`. The message goes to the customer's conversation on a push tool; API customers pull.

**Outputs:**
- Portal screens: Orders (filters for status and "Needs attention"), order detail (lines, ERP panel, next steps, timeline), the order panel on quotations, and the creation setting.
- The customer's order view in the API.
- Customer messages.

**Tenant/auth boundary:**
- Forced RLS on all four tables.
- Staff screens are per business.
- "Track my order" answers only orders whose enquiry belongs to the contact behind the asking identity.
- The API view shows number, status and total, never ERP or staff details.

**External effects and idempotency:**
- One order per quotation (database).
- One ERP write per order, with a reference unique per business and connection. The writer looks the reference up before creating.
- Unknown outcomes are never resent automatically, unless the ERP declares that it refuses duplicates itself.
- Notices use outbox keys `order:<event>`; "track my order" answers use `orders:<connection>:<event>`.

**Failures/recovery:**
- The stock source is down at creation: try again (automatic creation retries).
- The ERP is unreachable before sending: retried.
- The ERP refuses: `failed`; staff fix the cause (for example the customer name) and press send again.
- An unknown outcome is reconciled; after an hour it needs attention. See [the runbook](../operations/release.md#order-needs-attention).

**Configuration and secrets (names only):** none of its own. The ERP connection's `write_orders`, `default_customer`, `submit_orders` and `delivery_days` ([ERPNext](erpnext.md)), and `tenants.order_creation`.

**Tests and reproducible example:**
- Unit: `tests/unit/domain/test_orders.py`, `tests/unit/application/test_orders.py` (chaos and guards).
- Writer contract: `tests/unit/ports/test_erp_writer_contract.py` (simulated and sandbox).
- PostgreSQL: `tests/integration/workflows/test_orders.py`.

**Known limitations:**
- No PO number, address or delivery date from the customer.
- No reservation, partial delivery or payments.
- Wording and the statuses that send a notice are fixed.
- "Needs attention" sends no message.
- ERP cancellations happen in the ERP.
- The ERP delivery date is today + `delivery_days`, never shown to customers.

**Related ADRs:** 0003, 0004, 0015, 0016, 0023, 0024.
