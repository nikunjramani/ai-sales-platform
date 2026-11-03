# 6. Future commerce: orders, tracking, payments, own inventory
**Not planned for building now.** This document fixes the **shapes** we build today so these features plug in later without rewrites. Every business rule for them is asked before that work starts (AGENTS.md).

## Extension points to build in now
| Built now (phases 1-4) | Why it matters later |
|---|---|
| **Generic outbound outbox + templates** | Order confirmations, status updates and payment reminders are just new templates |
| **Conversation intent step** with pluggable handlers | "Where is my order?" and "I paid" become new intents, not a new pipeline |
| **Connections with purposes** | `payments` (Razorpay, Stripe...) and `orders` (ERP write) become new purposes of the same model |
| **Catalogue source port + our catalogue tables** | Our own inventory grows from these tables; ERP connectors can sync into them |
| **Append-only event history** per business object (quotation, later order) | Tracking and timelines, audits, and webhooks to n8n/CRMs come from the same events |
| **Honest unknown outcomes** (outbox and job rules) | ERP order creation and payment calls need exactly this |

## Orders (roadmap M6b, decisions still open)
- **Shape:** an `orders` table created from an **accepted** quotation version (one order per accepted version; a purchase-level guard across revisions), plus `order_events`, append-only.
- **Status model:** `placed → confirmed → in_progress → ready → shipped → delivered`, plus `cancelled` and `on_hold`. Each change is an event with its source (ERP sync, staff in portal, our inventory, a carrier) and time.
- **ERP write (when the source supports it):** a new optional port next to the reader, `OrderWriter`:
  - `reserve_stock`
  - `release_reservation`
  - `create_sales_order`
  - `get_order_by_external_reference`
  - `get_order_status`

  These use a stable external reference so an unknown outcome can be **reconciled, never repeated blindly**. The design is already in `.docs/architecture/integrations.md`. Without an ERP, the order lives in our tables and staff update its status in the portal.
- **Customer updates:** each status change can send a template message on the customer's conversation (the business chooses which ones).
- **"Track my order":** a conversation intent that reads the order status and replies with a template.

## Payments (later)
- **Port:** `PaymentProvider`, configured as a connection with purpose `payments`:
  - `create_payment_link(order, amount)`
  - `get_status`
  - inbound webhook events: `PaymentSucceeded`, `PaymentFailed`, `Refunded`
- **Records:** `payments` (order, amount, currency, provider reference, status, events).
- **Sending:** payment links are sent through the outbox.
- **Rule from the start:** nothing is marked paid without the provider's verified event.

## Our own inventory (later)
- **Grows from the catalogue tables in [05](05-product-data.md):** adds `stock_movements` (receipts, sales, adjustments, transfers), `reservations` (held for orders, with expiry) and warehouses.
- **Stock is calculated** from movements, with a ledger, rather than typed in.
- **It becomes one more catalogue source and order writer** behind the same ports. The quotation, approval and order code does not care whether stock comes from Tally or from us.

## What we deliberately do not do now
- **No order, payment or inventory tables** before their decisions are made.
- **No assumptions** about cancellation, partial delivery, refunds or credit terms.
