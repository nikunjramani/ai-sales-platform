# 0024: Orders: one per accepted quotation, written once to the ERP with intent before call
**Date:** 2026-10-29. **Status:** accepted (phase 7, roadmap M6b); implemented on `feat/phase-6-7-erp-and-orders`, verified with fakes, PostgreSQL and the ERPNext sandbox (synthetic data). Extends ADR 0003 (ERP authority: an ERP order's status follows the ERP; without an ERP we keep the order), ADR 0004 (persisted intents, reconciliation), ADR 0015 (unknown is never resent automatically) and ADR 0016 (acceptance; this ADR is what follows `accepted`).

## Context
The plan's question list (Q1–Q16, phase 6-7 plan §6.11) had to be answered first.

The user answered Q1–Q4 on 2026-10-29:
- **Q1:** creation is a per-business setting (staff confirm by default, or automatic on acceptance).
- **Q2:** operators, approvers and administrators manage orders.
- **Q3:** staff may enter the ERP customer; otherwise the connection's default is used.
- **Q4:** one order per quotation.

For the rest the user said "for this specific phase you will answer all questions, don't ask me". Those answers are Claude's, marked **(Claude)**, and open to change in review.

## Decision
| # | Answer |
|---|---|
| Q1 | Per business (`tenants.order_creation`): `staff` (default, "Create order" on an accepted quotation) or `automatic`. A `create_order` job is queued with **every** acceptance in the same transaction and creates the order only when automatic. |
| Q2 | `orders:view` for every staff role; `orders:manage` for administrators, sales operators and approvers. Changing the creation setting needs `tenant:manage`. |
| Q3 | The order comes from the accepted version only. ERP orders need an ERP customer: staff may type one, otherwise the connection's default customer. **(Claude)** No PO number, address or date is collected. |
| Q4 | One order per quotation (`uq_orders_quotation`), across all its versions. A real repeat purchase needs a new quotation. |
| Q5 | **(Claude)** Prices stand as accepted (they were re-checked at acceptance). **Stock is re-checked** when the order is created; unknown stock is not a shortfall. |
| Q6 | **(Claude)** No reservation by us. A submitted ERPNext sales order reserves its own stock (seen in the spike). |
| Q7 | **(Claude)** Staff may cancel a **local** order, with a reason, until it ships. An ERP order is cancelled **in the ERP**, and its status follows. A held ERP order that was never sent may be cancelled here. Changes need a new quotation. |
| Q8 | **(Claude)** Statuses `placed → confirmed → in_progress → shipped → delivered`, plus `cancelled` and `on_hold` (`domain/orders.py`, `LOCAL_TRANSITIONS`). ERP states map to these: draft → placed, partly delivered → in progress, and so on. A final status never reopens. |
| Q9 | **(Claude)** Customers hear about confirmed, shipped, delivered and cancelled, in fixed wording (`domain/messages.py`, not editable). Push tools get a message; API customers pull `GET /api/v1/enquiries/{id}/order`. |
| Q10 | **(Claude)** "Track my order": a whole message such as "where is my order?" or an order number (fixed phrases, like STOP/START; no AI). Only the asking contact's orders are answered (number, status, total). Anything else is an ordinary message. |
| Q11 | **(Claude)** Stock short at creation: the order is created **on hold** with the reason and is not written to the ERP until staff release it. |
| Q12 | **(Claude)** No partial acceptance or delivery handling (ERP partial delivery shows as "in progress"). |
| Q13 | **(Claude)** No payment link (later, 06-future-commerce). |
| Q14 | **(Claude)** Staff can create orders for quotations accepted before orders existed; there is no automatic backfill. |
| Q15 | **(Claude)** An unknown ERP write is looked up by its reference every 5 minutes for 1 hour, then **needs attention**. It shows in the portal's "Needs attention" tab; no message is sent. |
| Q16 | **(Claude)** Orders are written to the ERP only after an administrator turns on "Write orders to ERPNext" in the connection form (off by default; the form says to try a test company first). Orders arrive as **drafts** unless "Submit orders" is on. |

Mechanics:
1. **Migration `0015_orders`:**
   - `orders`: copies the accepted version's totals and points at that version and its hash. Only status, hold reason, ERP customer, ERP order ID and `updated_at` are updatable.
   - `order_lines`: immutable copies of the version's lines.
   - `order_events`: append-only.
   - `order_operations`: the ERP write, with its stable reference unique per business and per connection.
   - Also `tenants.order_creation`, one active order destination per business, `order_status_request` inbox events, and forced RLS on every new table.
2. **ERP write, intent before call** (`ErpOrderOperationHandler`):
   - The operation row and its reference `QO-<order id>` exist before any call. `pending → sending` is compare-and-set, so only its winner calls.
   - Success → `succeeded` (ERP ID, status from the ERP's state).
   - A certain refusal → `failed` (staff fix the cause and send again).
   - `unknown_outcome`, a 5xx while writing, any unexpected error after sending, or finding the row still `sending` (worker died) → `unknown` plus a reconciliation job. Unknown never escapes to the job runner, so it is never retried blindly.
   - Failing before anything was sent (connection refused, 429) → back to `pending`, and the job retries.
   - `conflict` → look the reference up; if the order is ours, the write succeeded.
3. **Reconciliation** (`ReconcileOrderOperationHandler`):
   - Found → `succeeded`.
   - Still missing inside the window → looked up again later.
   - After the window → `needs_attention`. Only an ERP that declares `idempotency_key` gets the order sent again automatically. Without `reference_lookup`, an unknown write goes straight to staff.
   - Staff resolve it, each choice confirmed in the UI: "it is in the ERP" (type its number) or "it is not: send again" (same reference, searched first).
4. **Status refresh:** `refresh_order_status` reads open ERP orders every 15 minutes while any is open, and stops when none is.
5. **Notices:** a status change customers hear about queues `notify_order` in the same transaction. Its outbox key `order:<event>` means a retry sends nothing new.

## Alternatives considered
- **An order node in the approval graph** (plan option B): it would hold graph runs for weeks and does not fit staff status changes.
- **Reserving stock ourselves:** a second failure path (reserve, then release), for no gain while ERPNext reserves on submit.
- **Automatic resend after the window:** risks duplicate ERP orders when the ERP cannot refuse duplicates.

## Consequences
- A second ERP that takes orders is a package implementing `OrderWriter` with `OrderCapabilities`; the core is unchanged.
- Businesses without an ERP keep orders with us (local), following ADR 0003's exception path.
- Open: editable customer wording, per-status notice choices, payments, carriers and tracking numbers, partial delivery, PO numbers, who gets "needs attention" messages.

## Verification
- Unit tests: domain rules; service and handlers over fakes, including chaos (timeout after commit gives exactly one ERP order, worker stopped while sending, the window, no resend) and the job-runner guard.
- Writer contract: simulated writer and the ERPNext sandbox.
- PostgreSQL workflow tests: automatic and staff creation, the customer's pull view, the one-order race, compare-and-set steps, append-only history, grants, isolation.
