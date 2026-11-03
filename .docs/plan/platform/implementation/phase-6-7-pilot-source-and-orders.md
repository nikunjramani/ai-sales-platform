# Implementation plan, phases 6 and 7: pilot product source and orders
**Status:** implemented on `feat/phase-6-7-erp-and-orders` (2026-10-29). D21 and Q1-Q4 were answered by the user; for the rest the user said "for this specific phase you will answer all questions, don't ask me", so Claude decided them. See [Answers](#answers-2026-10-29), [ADR 0023](../../../adr/0023-erpnext-product-source.md) and [ADR 0024](../../../adr/0024-orders.md). Where an answer differs from the provisional designs below, the answers win. Migration numbering follows contracts §4: `0014_catalogue_sync` (phase 6), `0015_orders` (phase 7).

### Answers (2026-10-29)
**Phase 6 (user):**
- D21: **ERPNext**, sandbox only (no pilot business yet), as a local Docker sandbox.
- Read mode: hybrid (a synced copy for search, live prices and stock).
- Sync interval: configurable per business (5-1440 minutes, default 15).
- "In stock": a setting, default available (on hand minus reserved).
- Tax-inclusive prices: refused.
- Warehouses: a chosen list, summed.
- Items: enabled sales items of chosen item groups (empty = all).
- Price list: one selling price list.
- Delivery: one PR for both phases.

**Phase 6 (Claude):**
- A full feed every run.
- Synced products go into our catalogue tables (source `sync`); typed products with the same SKU are adopted; products no longer offered are archived.
- Enabling a product source replaces the previous one; Disable restores it.
- Exact unit renames, then the known unit words.
- Negative stock means 0; non-stock items have unknown stock.
- Item Price days start at 00:00 UTC.
- In production only public https addresses are accepted.
- Settings change by Disable and Enable again.
- Pilot questions §5.6 stay open as settings.

**Phase 7 (user):**
- Q1: per-business setting, default staff confirm.
- Q2: operators, approvers and administrators manage orders.
- Q3: staff may enter the ERP customer; otherwise the connection default.
- Q4: one order per quotation.

**Phase 7 (Claude):**
- Q5: stock re-check only.
- Q6: no reservation by us.
- Q7: cancel local orders before they ship; ERP orders are cancelled in the ERP; changes need a new quotation.
- Q8: placed, confirmed, in_progress, shipped, delivered, cancelled, on_hold.
- Q9: customers are told about confirmed, shipped, delivered and cancelled (fixed wording).
- Q10: fixed phrases or an order number; the same contact only; number, status and total.
- Q11: a shortfall holds the order (not sent to the ERP until released).
- Q12: no partial delivery.
- Q13: no payments.
- Q14: staff may create orders for earlier acceptances; no backfill.
- Q15: look-ups every 5 minutes for 1 hour, then needs attention in the portal (no message).
- Q16: writes off until an administrator turns them on in the connection form; drafts unless "submit orders".

Binding names, tables, job kinds and migration numbers come from [00-shared-contracts.md](00-shared-contracts.md). Changes this plan needs are listed in [section 9](#9-proposed-changes-to-shared-contracts) and must be made there before work starts.

## 1. Goal
- **Phase 6:** quotations for the pilot business are priced from **their real system** (ERP or online shop), through one generic connector package, chosen and configured in the portal like any other connection.
- **Phase 7 (roadmap M6b):** an accepted quotation becomes an **order** that cannot be created twice, whose status is tracked, that customers are told about, and that is written to the ERP when the ERP supports it, with honest handling of unknown outcomes.
- **Later:** payments, our own inventory, and richer order tracking plug in without rewriting phases 6-7.

## 2. Scope
| In scope | Not in scope |
|---|---|
| Phase 6: one connector package for the pilot's system (live read, sync, or both), field/unit/currency/tax/warehouse mapping, capability declaration, credentials in `SecretStore`, contract tests against the vendor sandbox, mapping and status screens in the portal | A second ERP (it must be possible without code changes elsewhere, but is not built); spreadsheet import (D18 later); writing products **to** the ERP |
| Phase 7: `ErpWriter` port (optional capability), order operations with intent-before-call, reconciliation, purchase-level duplicate guard, local orders without ERP, append-only order events, customer notifications via the outbox, "track my order" intent, portal screens, migration `0014_orders` | Payments, our own stock ledger, carrier integrations, partial delivery, returns/refunds, credit terms (all "later", questions in section 7) |
| Forward-compatible shapes for payments, inventory and tracking | Any business rule not answered by the user |

## 3. Decisions used and open questions
| # | Decision | Effect here |
|---|---|---|
| D18 | Manual entry in the portal now; ERP/shop connectors later | Phase 6 is the "later". Our catalogue tables (phase 3) already exist and stay usable |
| D19 | We store catalogue entered by the business; a connected ERP stays the authority | A synced catalogue is a **copy** (cache) of the ERP, marked read-only for synced fields; ADR 0003 amendment (phase 3) covers it |
| D21 | **Open:** which ERP/shop the pilot uses | Blocks phase 6. Section 5.1 gives the comparison to help answer it |
| D23 | Orders after platform phases (proposal) | Phase 7 comes after phases 1-6 |
| ADR 0003 | ERP authoritative; unsupported writes need explicit exception paths | `ErpWriter` is optional; no writer means local order + staff path |
| ADR 0004 | Persist intents; at-least-once needs idempotency and reconciliation | Order operations are rows written before the remote call |
| ADR 0010 | Decimal money, exact units, no guessed conversion | Mapping config never converts units or currency by guessing |
| ADR 0015 | Honest outbox: `unknown` is never resent automatically | Same rule for ERP writes |
| ADR 0016 | M6 accepts only; no order, no reservation; no PO number collected | Phase 7 adds what follows `accepted`. A PO number is a new question |
| open-decisions.md | "Quote validity and reservations": reservation open for orders; "Offer lifecycle": partial acceptance not supported; "Human handoff": post-order change/cancellation escalation undefined | All become questions in 4.1 |

Open questions are listed per phase: [Phase 6 (5.6)](#56-questions-for-the-pilot-business-ask-before-phase-6-starts), [Phase 7 (6.11)](#611-the-question-list-ask-before-phase-7-starts), [Later (7.x)](#7-later-features-forward-compatibility).

## 4. Current-state findings (file:line)
### 4.0 What exists today
| Area | Finding | Consequence for phases 6-7 |
|---|---|---|
| ERP errors | `ports/erp.py:19-28` defines `not_found, unsupported, unauthenticated, forbidden, rate_limited, temporary_unavailable, invalid_request, conflict, unknown_outcome`. `:31` only `rate_limited` and `temporary_unavailable` are retryable. `:40-42` docstring already says writes with `unknown_outcome` need reconciliation | Codes are ready for writes. `conflict` is the natural answer for "external reference already used" |
| ERP read port | `ports/erp.py:45-70` `ErpReader`: `search_products`, `list_categories`, `get_products`, `get_prices`, `get_availability(warehouse=None)`. No writer protocol | Phase 7 adds `ErpWriter` as a **separate** protocol so read-only sources stay valid |
| **Job retries ignore `retryable`** | `application/jobs.py:59-71` retries **every** non-`PermanentJobError` exception with backoff; nothing reads `ErpError.retryable` (only definition at `ports/erp.py:40`) | A write handler must **catch** `unknown_outcome` (and other non-retryable codes) itself and record the result, exactly like delivery does. Letting it escape would retry a write blindly. A test must prove this |
| Honest unknown pattern | `application/delivery.py:94-102` a row left in `sending` becomes `unknown`; `:126-133` compare-and-set `pending -> sending`; `:152-160` uncertain answer -> `unknown`; `:161-170` other errors -> back to `pending` and re-raise for retry | Reused 1:1 for order operations (section 6.3) |
| Simulated ERP | `adapters/erp/simulated.py:55-79`: per-tenant catalogues, `failures` injection per operation name (`:66`), `unsupported` when no catalogue (`:78`); `source = "simulated-erp"` (`:56`); one warehouse per tenant (`:37`) | Extend with a simulated writer that can fail **after** committing (chaos), for contract tests |
| Contract tests | `tests/unit/ports/test_erp_contract.py:45` one adapter (`simulated`); `:4-6` real adapters register with `pytest.mark.integration`; only 5 tests (`:54-91`): evidence, unknown SKU absent, tenant isolation x2, naive date | Phase 6 must add tests for error mapping, `list_categories`, `get_availability` (unknown vs zero, warehouse filter) and rate limits before a real adapter is trusted |
| Hardcoded source | `worker.py:69` and `api/app.py:92` build `SimulatedErp` directly | Phase 1 (`application/catalogue_sources.py`) replaces this; phase 6 depends on it |
| Snapshot | `domain/quotations.py:164-209` version content: currency, policy, lines (`sku`, `description`, `quantity`, `unit`, `unit_price`, `line_total`, `price_list_id/version`, `price_source`, `stock_*`), `subtotal`, `tax`, `total`. Hashed at `:212-215` | An order copies these exact values. There is **no** customer ERP ID, address, delivery date or PO number in it |
| Acceptance outcome | `domain/acceptance.py:9` "creates no ERP order and reserves no stock"; `:37` `ACCEPTED`; `:44-50` maps to `QuotationStatus.ACCEPTED` | Phase 7 starts from a recorded `accepted` response |
| Re-pricing on accept | `application/acceptance.py:210-243` re-prices as the system identity (`:218`); any difference refuses; stock ignored (`domain/acceptance.py:145`) | Prices are fresh at acceptance. Stock is **not** checked; a stock shortfall at order time is a new question |
| Recording under lock | `persistence/acceptance.py:170-191` `SELECT ... FOR UPDATE` on the quotation, then re-check; `:242-262` queues `resume_quotation` and `notify_response` jobs **in the same transaction** | The natural, atomic place to also queue order creation (option A in 6.1) |
| One closing answer | `migrations/versions/0007_acceptance.py:104-110` unique index: one non-`refused_changed` response per quotation; `0005_approvals.py:96` one quotation per enquiry | At most one acceptance per quotation, across all its revisions. This **is** the purchase guard inside one enquiry; across enquiries it is a question (4.1 Q4) |
| Workflow end | `workflows/quotation.py:170-178` `apply_customer` calls `store.finish` for closing outcomes; `:216-219` accepted -> `END`; `:54-55` graph stays `quotation-approval-v1` because M6 only added steps after `approved` | Adding an order node after `apply_customer` is possible without renaming the graph, but see 6.1 for why a separate order lifecycle is recommended |
| Jobs | `domain/jobs.py:19-27` eight kinds; handlers must be idempotent (`application/jobs.py:3-4`) | New kinds in section 9 |
| Append-only | `0005_approvals.py:249-255` `reject_change()` trigger; `0007_acceptance.py:117-120` used on responses; column-level grants `0005_approvals.py:286-304`, `0006_documents.py:116-118` | `order_events` uses the same trigger; orders get column-level UPDATE grants |
| Permissions | `domain/tenancy.py:25-33` no order permissions | New permissions (section 9) |
| Docs | `.docs/architecture/integrations.md:46` optional writes `reserve_stock, release_reservation, create_sales_order, get_order_by_external_reference`; `:51-52` capabilities + manual path if reconciliation impossible. `data-model.md:80,90` order operation entity and purchase guard. `execution-flow.md:107-108,125-133` order partial failures. `operations/release.md:24` unknown ERP write runbook | Phase 7 implements these as written. `06-future-commerce.md:18-21` uses different names (`get_order_by_reference`, no `release_reservation`); this plan uses the `integrations.md` names plus `get_order_status` |

## 5. Phase 6: real product source for the pilot
### 5.1 Choosing the first system (D21 is open)
Facts below come from official documentation, **checked 2026-10-23** by search; limits change, so the phase 6 spike re-verifies each against the pilot's actual plan and version. "Not verified" means the official page did not confirm it.

| System | API type | Sandbox / test | Auth | Stock semantics | Rate limits | Difficulty |
|---|---|---|---|---|---|---|
| **Tally (TallyPrime)** | XML over HTTP to the desktop app (default port 9000) ([Tally XML integration](https://help.tallysolutions.com/xml-integration/), [prerequisites](https://help.tallysolutions.com/pre-requisites-for-integrations/)) | No hosted sandbox; a test company on a local install (licence needed; educational mode not verified) | None at the HTTP port; network access to the PC is the control | Stock item closing balance per godown; no reservation concept (not verified) | None documented; limited by the desktop machine | **High:** runs on the business's PC, so our cloud cannot reach it without an on-premises agent or tunnel; XML envelopes |
| **Zoho Inventory** | REST/JSON, regional domains, `organization_id` per call ([API](https://www.zoho.com/inventory/api/v1/)) | Free plan / trial organisation | OAuth 2.0 | Stock on hand, available for sale, committed (per warehouse) | Daily cap by plan (Free 1,000/day upward) ([limits FAQ](https://www.zoho.com/inventory/kb/automation/per-day-api-limit.html)); shared by all integrations | **Low-medium** |
| **Zoho Books** | REST/JSON ([API v3](https://www.zoho.com/books/api/v3/)) | Free plan / trial | OAuth 2.0 | Items with stock only if inventory tracking is on | 100 requests/minute per organisation; daily cap by plan (1,000-10,000); 5-10 concurrent | **Low-medium** |
| **Odoo** | XML-RPC / JSON-RPC over models ([External API 18.0](https://www.odoo.com/documentation/18.0/developer/reference/external_api.html)) | Free trial database, or self-hosted Community | User + password or API key | `stock.quant` on-hand and reserved per location; sale orders reserve on confirmation | Not documented for self-hosted; Odoo Online plans differ. External API only on Custom plans for Odoo Online | **Medium:** generic model API, many versions |
| **ERPNext (Frappe)** | REST `/api/resource/{doctype}` ([Frappe REST](https://docs.frappe.io/framework/user/en/api/rest), [rate limiting](https://docs.frappe.io/framework-git/rest-api/rate-limiting.md)) | Self-host free (open source) or trial | `token api_key:api_secret` header, or OAuth 2.0 | `Bin`: actual, reserved, projected quantity per warehouse | Off unless configured; HTTP 429 when hit | **Low-medium:** open source, easy local sandbox |
| **SAP Business One** | Service Layer (REST/OData) ([SAP Help Portal](https://help.sap.com/docs/SAP_BUSINESS_ONE), page not verified) | Partner/demo database only; no free public sandbox (not verified) | Session login (`B1SESSION` cookie) | On hand, committed, ordered per warehouse (not verified) | Not documented (not verified) | **High:** access through an SAP partner, on-premises installs common |
| **Shopify** | GraphQL Admin API ([docs](https://shopify.dev/docs/api/admin-graphql), [rate limits](https://shopify.dev/docs/api/usage/limits)) | Free development stores | OAuth app or custom-app access token | Inventory levels per location with named quantities (available, on hand, committed, reserved) | Cost-based leaky bucket, e.g. 100 points/second on standard plans | **Low** (excellent sandbox); prices are retail, B2B price lists only on some plans |
| **WooCommerce** | REST/JSON ([REST API](https://developer.woocommerce.com/docs/apis/rest-api/), [auth](https://developer.woocommerce.com/docs/apis/rest-api/authentication/)) | Any local WordPress install | Consumer key + secret (Basic auth over HTTPS) | One `stock_quantity` per product; no warehouses in core | None in core; hosting/plugins may throttle | **Low**, but data quality varies by shop |

**How to choose:** ask the pilot (5.6), then run a **one-day spike** on that system's sandbox answering five checks: (1) can we read products, prices and stock by SKU; (2) what does "available" mean; (3) is there a field where we can store our external order reference and search by it (needed in phase 7); (4) idempotency or duplicate protection on order creation; (5) real rate limits on the pilot's plan. Record the result in an ADR. If the pilot has no system, phase 6 is skipped and phase 3's local catalogue stays the source.

### 5.2 Live read (C) or sync (D)
| | C. Live read adapter | D. Sync into our catalogue tables |
|---|---|---|
| How | Implements `ErpReader` by calling the vendor on each request | A `sync_catalogue` job copies products/prices/stock into phase 3 tables with `source = <connection>`; `LocalCatalogue` serves reads |
| Freshness | Live | As of the last sync (stock marked stale by existing rules) |
| Search quality | Vendor's search (often poor) | Our full-text + trigram search (phase 3) |
| Rate limits | One enquiry = several calls; daily caps (Zoho) can run out | Predictable, scheduled |
| Reachability | ERP must be reachable at quote time | Works if the ERP is down briefly |
| Fits | Shopify, ERPNext, Odoo with good APIs | Tally (agent pushes), Zoho (daily caps), large catalogues |

**Recommendation (technical, to confirm with the user):** a **hybrid** per connection, chosen by capability: sync for search and catalogue, **live re-check** of price and stock for the final lines (`get_prices`, `get_availability`) when the source supports it. The `ErpReader` contract is the same either way, so the core does not change. Which mode the pilot uses depends on 5.6 answers (freshness need, rate plan).

### 5.3 Work items
| # | Item | Notes |
|---|---|---|
| P6.0 | Ask the questions in 5.6 and D21; run the spike (5.1); write the ADR | No code before answers |
| P6.1 | Connector package `adapters/providers/<vendor>/` registered by entry point (`sales_platform.providers`), `purposes = {catalogue}` (later also `orders`) | Shared contracts §3. Vendor SDK only inside the package. No `if vendor == ...` anywhere else |
| P6.2 | `config_model` (Pydantic) = connection settings + **mapping** (P6.3); portal renders it from JSON schema | Base URL/region, company/organisation ID, which price list, which warehouses |
| P6.3 | Mapping (domain, standard library): `CatalogueMapping` | SKU field; unit map (ERP unit code -> our unit text, **exact rename only, never a conversion**); currency must equal the tenant policy currency, otherwise prices are omitted (ADR 0010); tax basis flag (prices with or without tax; see 5.6); warehouse selection (one, list, or "sum of listed"); category field; active/inactive filter. Unmapped values are reported, not guessed |
| P6.4 | Capability declaration | Generic `Capabilities` plus catalogue/order flags (section 9): `live_prices`, `live_stock`, `stock_semantics` (`on_hand`/`available`/`unknown`), `warehouses`, `webhooks`, later `create_order`, `reference_lookup`, `idempotency_key`, `reserve_stock` |
| P6.5 | Credentials in `SecretStore`; `connections.secret_refs` holds references only | OAuth refresh handled inside the package; `test()` performs one harmless read |
| P6.6 | Live adapter: implements `ErpReader`; vendor errors mapped to `ErpErrorCode` (401 -> `unauthenticated`, 403 -> `forbidden`, 429 -> `rate_limited`, 5xx/timeout on reads -> `temporary_unavailable`); timeouts set; results stamped with `source` and `fetched_at` | Unknown SKUs omitted, never zero |
| P6.7 | Sync adapter (if chosen): `sync_catalogue` job, full then incremental (webhook or modified-since); synced rows read-only in the portal for mapped fields; sync run record (counts, errors, finished_at) | Needs a small table (section 9, migration numbering) |
| P6.8 | Contract suite extension (section 8) and a sandbox parameter `pytest.param(..., marks=pytest.mark.integration)` | Sandbox credentials from environment, never committed |
| P6.9 | Portal (`connections` feature): mapping form, "Test", sync status and last error, unmapped units/currencies list | Uses existing connection screens from phase 1/5 |
| P6.10 | Docs: feature doc, configuration reference, ADR (chosen system, mode), handoff | AGENTS.md |

**Adding a second system later:** a new package with its own entry point and config model; the registry, mapping screen, contract suite and core are unchanged.

### 5.4 Failure behaviour
- ERP down during drafting: existing behaviour (`workflows/quotation.py:104`, error propagates, job retries).
- ERP down at acceptance: nothing recorded, customer told to retry (ADR 0016).
- Sync failing: last good copy stays, stock becomes stale under existing rules, portal shows the error.
- Credentials revoked: `unauthenticated`; connection status shows "needs attention"; no fabricated data.

### 5.5 Risks specific to phase 6
On-premises systems (Tally, SAP B1) need an agent we would have to build and support; rate caps shared with other integrations; ERP "available" meaning differs per vendor (we show the declared semantics, never reinterpret); tax-inclusive prices silently doubling tax if mis-mapped (a contract test with a known product guards it).

### 5.6 Questions for the pilot business (ask before phase 6 starts)
1. Which system and version, cloud or installed on a PC/server? Who administers it?
2. Can we get a test/sandbox company or a read-only API user? Which plan (for rate limits)?
3. How many products? Is the SKU/item code unique and stable?
4. Which price list(s) apply to quotations? Customer-specific prices: needed now?
5. Are prices stored with or without tax? Which tax rates exist?
6. Which units are used, and are any products sold in a different unit than stocked?
7. Which warehouses/godowns count for quoting: one, several summed, or per customer location?
8. What should "in stock" mean: on hand, or on hand minus already committed?
9. How fresh must stock be: live, or minutes/hours old is fine?
10. Will staff still edit products in our portal, or only in the ERP?
11. Which categories/groups should customers see; should inactive items be hidden?
12. Any data-location or privacy rules for their ERP data?

## 6. Phase 7: orders (M6b), provisional
### 6.1 Where order creation hooks in
| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Separate order lifecycle (recommended)** | `persistence/acceptance.py:record` queues a `create_order` job in the same transaction as an `accepted` response (next to `notify_response`, `:255-262`). The approval graph ends as today (`workflows/quotation.py:216-219`) | No graph change; orders last weeks and have their own events; staff can also create an order for older accepted quotations; failures in ERP writes never block the approval graph | A second state machine to keep consistent (the order row is the truth) |
| B. New graph node after `apply_customer` | `accepted` -> `create_order` node instead of `END` | One place for the whole story | Graph holds runs for weeks; reconciliation and staff status updates do not fit an interrupt model; changes `GRAPH_VERSION` policy if any existing step changes |

If the answer to Q1 is "staff confirm first", option A queues nothing automatically; the portal shows "Create order" on accepted quotations instead. Quotations accepted before phase 7 ships get no order automatically; whether to backfill is Q14.

### 6.2 Domain shape (names provisional)
- `Order`: tenant, ID, `order_number` (human, e.g. `O-1A2B3C4D`, derived like `quotation_number`, `domain/quotations.py:230-232`), quotation ID + version + content hash, response ID, `purchase_key` (6.4), contact/conversation (phase 4), currency and totals copied from the version, `status`, `fulfilment` (`erp` or `local`), `erp_order_id` (nullable), created_at/by.
- `OrderLine`: copied from version content lines (SKU, description, quantity, unit, unit price, line total). Copying keeps the order valid if later features (partial delivery, changes) need per-line state; the version content stays the signed evidence.
- `OrderEvent` (append-only): type (`created`, `status_changed`, `erp_write_succeeded`, `erp_write_unknown`, `reconciled`, `note`, `customer_notified`...), from/to status, source (`system`, `staff`, `erp`, later `carrier`, `inventory`, `payment`), actor, detail, occurred_at.
- `OrderOperation` (the "order operation" entity of `data-model.md:80`): kind (`reserve_stock`, `create_sales_order`, `release_reservation`), idempotency key, external reference, connection, status (`pending`, `sending`, `succeeded`, `failed`, `unknown`, `needs_attention`), ERP IDs returned (order ID, reservation ID), attempts, last error, timestamps.

### 6.3 ERP write: `ErpWriter` port (optional capability)
```python
class ErpWriter(Protocol):  # ports/erp.py, next to ErpReader
    def reserve_stock(
        self, context, reference: str, lines: Sequence[OrderLineRequest]
    ) -> Reservation: ...
    def release_reservation(self, context, reservation_id: str) -> None: ...
    def create_sales_order(self, context, reference: str, order: SalesOrderRequest) -> ErpOrder: ...
    def get_order_by_external_reference(self, context, reference: str) -> ErpOrder | None: ...
    def get_order_status(self, context, erp_order_id: str) -> ErpOrderStatus: ...
```
- A provider offers a writer only if it declares `create_order` and has purpose `orders`; otherwise the order is `local` (6.6). Resolution per tenant, like the reader (`application/catalogue_sources.py`).
- **Stable external reference:** generated once from the order ID (e.g. `QO-<order id>`), stored on the operation **before** any call, sent in the ERP's reference field (vendor field chosen in the mapping, verified in the spike).
- **Intent before call** (same as `application/delivery.py`):
  1. In one transaction: insert order, order lines, `created` event, `OrderOperation(pending)` and its job.
  2. Job: operation already final -> stop. Found in `sending` -> `unknown` (worker died mid-call; `delivery.py:94-102`).
  3. Compare-and-set `pending -> sending` (`delivery.py:126-133`).
  4. Call the ERP. Success -> `succeeded` + ERP IDs + event. `conflict` (reference already exists) -> look it up; if it matches, `succeeded`. `invalid_request`/`forbidden`/`unauthenticated`/`unsupported` -> `failed` with reason (staff action). `unknown_outcome` or timeout after send -> `unknown`. `rate_limited`/`temporary_unavailable` **before** sending (connection refused) -> back to `pending`, re-raise for job retry.
  5. The handler **never** lets `unknown_outcome` escape to `JobRunner` (finding: `application/jobs.py:64-71` would retry it).
- **Reconciliation job** (`reconcile_order_operation`): for each `unknown` operation, call `get_order_by_external_reference`. Found -> `succeeded` (store ERP ID, event `reconciled`). Not found -> stays `unknown` and is retried on a schedule for a window (ERPs can be eventually consistent); after the window, `needs_attention` for staff. A new create is sent **only** if the ERP declares `idempotency_key` support **or** staff confirm "not in ERP" in the portal. If the ERP has no reference lookup at all, every unknown goes straight to `needs_attention` (`integrations.md:52`).
- **Reservation (only if Q6 says so and the ERP declares `reserve_stock`):** order of steps per Q6 answer. If reservation succeeds but order creation fails **conclusively**, release it; if creation is **unknown**, reconcile first and never release stock belonging to an order that exists (`execution-flow.md:127-128`).
- **Re-validation before commit** (`integrations.md:58`): which facts must be re-checked at order time (price was checked at acceptance; stock was not) is Q5.

### 6.4 Duplicate guards (three layers)
| Layer | Guard | Where |
|---|---|---|
| 1. One order per acceptance | Unique (tenant, quotation_id) on `orders`. Since only one closing answer exists per quotation (`0007_acceptance.py:104-110`), this also covers every revision of the same quotation | Database |
| 2. Purchase level across quotations | `purchase_key` unique per tenant. Default = the quotation ID. Options per Q4: customer PO number (needs a new field, ADR 0016 did not collect one), staff-linked "same purchase", or a candidate warning when the same contact orders the same lines within N days (warning only, never automatic blocking) | Database + portal |
| 3. ERP level | External reference unique in our table and searched in the ERP before any second create | `order_operations` + reconciliation |

A real repeat purchase always needs an explicit new purchase identity (`data-model.md:90`).

### 6.5 Status model: options (Q8 decides)
| Option | Statuses | Fits |
|---|---|---|
| S1 minimal | `placed -> confirmed -> completed`, plus `cancelled` | Businesses that only need "we got it / done" |
| S2 from 06-future-commerce | `placed -> confirmed -> in_progress -> ready -> shipped -> delivered`, plus `cancelled`, `on_hold` | Physical goods with dispatch |
| S3 fixed stages + business labels | Internal stages fixed in code (as S2); each business renames or hides stages and maps ERP statuses to them | Several businesses with different words; ERP status mapping |

Transitions are a table in the domain (standard library), tested. The ERP status mapping (S3) lives in the connector's config model. Which statuses customers see and which trigger a message is Q9.

### 6.6 Orders without an ERP
Same tables, `fulfilment = local`. Staff with `orders:manage` change status in the portal with a reason; each change is an `order_events` row. No operation rows are created. If a writer is connected later, existing local orders stay local (no silent push).

### 6.7 Customer notifications and "track my order"
- **Notifications:** a status change writes, in the same transaction, an `outbound_messages` row (shared contracts §1, phase 1 outbox) with a new `TemplateKey` (e.g. `order_confirmed`, `order_status_changed`) **only** for statuses the business enabled (Q9). Recipient: the customer's conversation (phase 4); for API customers, a pull endpoint `GET /api/v1/enquiries/{id}/order` (and optionally a webhook later). Unknown outcomes from the ERP are **never** announced as success (`roadmap.md:15`).
- **"Track my order":** a new handler registered in `IntentRouter` (`application/conversations.py`, phase 4). It may only read orders linked to the **same contact** (verified identity per D12/D13) and replies with a template; no AI-written status text. Several open orders -> list with numbers and ask which. No order -> template "no order found". Questions in Q10.

### 6.8 Portal (`portal/src/features/orders/`, per portal architecture)
- Orders list (filter by status, "needs attention"), order detail with lines, timeline from `order_events`, ERP reference/ID, operation status.
- Actions (by permission): create order from an accepted quotation (if Q1 = staff), change status (local orders), cancel (per Q7), resolve `needs_attention` ("found in ERP: enter ERP number" or "not in ERP: retry create", both with confirmation and recorded actor).
- Quotation detail shows its order link.

### 6.9 Migration `0014_orders` (provisional sketch, decided after the questions)
| Table | Key columns | Rules |
|---|---|---|
| `orders` | id, tenant_id, quotation_id, version, content_sha256, response_id, purchase_key, order_number, status, fulfilment, erp_connection_id, erp_order_id, currency, subtotal, tax, total, created_by, created_at, updated_at | RLS (`_TENANT_FILTER`); unique (tenant, quotation_id), (tenant, purchase_key), (tenant, order_number); FK to `quotation_versions` (tenant, quotation, version) and `quotation_responses`; status check constraint per Q8; UPDATE grant only on (status, erp_order_id, updated_at) |
| `order_lines` | order_id, line_index, sku, description, quantity, unit, unit_price, line_total | RLS; immutable (`reject_change()`) in phase 7 |
| `order_events` | id, tenant_id, order_id, type, from_status, to_status, source, actor, detail (JSONB), external_event_id, occurred_at, created_at | RLS; append-only (`reject_change()`); unique (tenant, source, external_event_id) where not null |
| `order_operations` | id, tenant_id, order_id, kind, idempotency_key, external_reference, connection_id, status, erp_order_id, reservation_id, attempts, last_error, created_at, updated_at | RLS; unique (tenant, idempotency_key), (tenant, connection_id, external_reference); UPDATE grant only on progress columns (as `0006_documents.py:118`) |
Grants: API role reads all, inserts orders/lines/events (staff actions); worker inserts and updates progress columns. Job kinds added; downgrade removes order job kinds (as `0007_acceptance.py:135`) and tables; round trip tested with an order present.

### 6.10 Work items (in order)
| # | Item |
|---|---|
| P7.0 | Ask Q1-Q16 (clarify-before-building); record answers in an ADR and open-decisions; update this plan |
| P7.1 | Domain: order, lines, events, status transitions, purchase key, operation states (standard library) + unit tests |
| P7.2 | Ports: `OrderStore`, `ErpWriter`; simulated writer with fault injection (fail before send, fail after commit, slow lookup, conflict) |
| P7.3 | Migration `0014_orders` + PostgreSQL tests (RLS, grants, append-only, uniqueness races) |
| P7.4 | `OrderService` (create from acceptance, staff create, status change, cancel per answers) + `create_order` hook (option A) |
| P7.5 | `ErpOrderOperationHandler` (6.3 steps) and `ReconcileOrderOperationHandler` |
| P7.6 | Templates and outbox wiring; API pull endpoint |
| P7.7 | `IntentRouter` handler "track my order" + evaluation cases |
| P7.8 | Portal `orders` feature |
| P7.9 | Connector: implement `ErpWriter` in the phase 6 package if the spike showed reference lookup; otherwise local orders only |
| P7.10 | Docs: feature doc, ADR, amend ADR 0003 consequence (orders kept by us when no ERP), runbook for `needs_attention`, handoff |

### 6.11 The question list (ask before phase 7 starts)
| # | Question | Options (what changes) |
|---|---|---|
| Q1 | When is an order created? | Automatically on acceptance (job queued in `record`) / staff confirm first (button, no job) / per-business setting (both paths) |
| Q2 | Who may create, confirm, change and cancel orders? | Sales operator / approver / administrator; drives the `orders:*` permissions |
| Q3 | Does the order need extra data the quotation lacks: customer PO number, delivery address, requested date, contact person, ERP customer account? | None (order from quotation only) / collected at acceptance (API change, ADR 0016 amendment) / entered by staff |
| Q4 | What counts as the "same purchase"? | Quotation (default) / customer PO number / staff link / warning on similar repeat orders |
| Q5 | Re-check at order time? | Nothing more (accepted prices stand) / stock check / full re-price (then what if it changed after acceptance?) |
| Q6 | Reserve stock? When and for how long? | No reservation / reserve at acceptance / reserve at order creation; expiry; who releases |
| Q7 | Cancellation and changes after the order exists | Not allowed in our system (ERP/manual only) / staff cancel before a given status / changes need a new quotation version and approval |
| Q8 | Status model | S1 / S2 / S3 (6.5), and allowed transitions |
| Q9 | Which statuses do customers see, and which send a message? | Per status on/off per business; wording is a fixed template (editable?) |
| Q10 | "Track my order": who may ask and what is shown? | Same contact only / any verified identity of the customer company; show status only / plus lines and dates |
| Q11 | Stock shortfall at order time | Create anyway and flag / hold (`on_hold`) for staff / partial order (needs partial support) / refuse and revise |
| Q12 | Partial acceptance or partial delivery | Not supported (today's rule) / later feature |
| Q13 | Payment terms link | None now / show terms on the order / payment required before `confirmed` (needs 7.1 first) |
| Q14 | Quotations accepted before phase 7 | No orders / staff can create them / automatic backfill |
| Q15 | ERP unknown outcome: how long to keep checking before asking staff, and who gets the "needs attention" message? | Window length; recipients (approvers? operators?) |
| Q16 | ERP write permission | Write to the live ERP only after sandbox validation and explicit activation per business (`roadmap.md:26`): who activates? |

## 7. Later features (forward compatibility)
No tables or code for these now (06-future-commerce). Each section lists the shape to keep possible and the questions to ask when it starts.

### 7.1 Payments
- **Port:** `PaymentProvider` as a provider with purpose `payments` (shared contracts §1): `create_payment_link(order, amount, currency, reference)`, `get_status(reference)`, `refund(...)` later. Unsupported operations raise `OperationUnsupported`.
- **Inbound:** provider webhooks go through phase 1's generic route: `verify_inbound` (signature) -> `parse_inbound` -> new event type `PaymentEvent` (`succeeded`, `failed`, `refunded`) -> dedupe -> `process_inbound`.
- **Rule from the start:** an order is marked paid **only** from a verified provider event or a verified `get_status` read, never from a customer message ("I paid") or an AI reading. "I paid" becomes an intent that triggers a status check and tells staff.
- **Sketch (not a migration):** `payments` (tenant, order, amount, currency, provider connection, provider reference unique, status, created/updated), `payment_events` (append-only, raw verified event reference). Payment links are sent via the outbox with new template keys.
- **Questions:** which provider(s) and countries; payment before or after order confirmation; full, advance or partial payments; credit terms per customer; who pays fees; link expiry; refunds and who approves them; reconciliation with the ERP's accounts; receipts/invoices (who issues them); what happens to an order if payment fails or never arrives.

### 7.2 Our own inventory
- **Grows from phase 3 tables:** `catalogue_stock` becomes a **projection** of a ledger. New: `warehouses`, `stock_movements` (append-only: receipt, sale, adjustment, transfer, reservation-release; quantity with unit, reason, actor, source document), `stock_reservations` (order, SKU, quantity, warehouse, expires_at, status).
- **Same ports:** `LocalCatalogue` keeps implementing `ErpReader`, and a `LocalOrderWriter` implements `ErpWriter` (reserve = reservation row; create order = sale movements). Quotation and order code do not change.
- **Concurrency:** reservations take a row lock per (warehouse, SKU) or use a serialisable check, so stock cannot go below zero unless the business allows it.
- **Questions:** may stock go negative; how stock is counted (receipts from suppliers? adjustments by whom?); batch/serial numbers or expiry dates; multiple warehouses and transfers; reservation length; valuation/cost (probably not ours); migrating from an ERP to ours or back.

### 7.3 Order tracking and status updates
- **Sources:** ERP sync (status read by `reconcile`/poll or ERP webhook mapped to an `OrderStatusChanged` inbound event), staff in the portal, our inventory (later), a carrier (later: a provider purpose such as `shipments`, not in shared contracts yet).
- Every change is an `order_events` row with `source`, `actor`, `occurred_at`, `received_at`; the order's current status is derived from it (or kept in a column updated in the same transaction).
- Out-of-order or duplicate external events are deduped by (connection, external event ID) and a status can only move along allowed transitions, otherwise it is recorded as "ignored: not allowed from X" for staff.
- **Questions:** which source wins when ERP and staff disagree; which carriers; tracking numbers/links shown to customers; how often to poll the ERP; whether the customer can see a delivery date (it must come from a supported source, `integrations.md:47`).

## 8. Test strategy
All tests in the mirrored `tests/` tree.
| Area | Tests |
|---|---|
| Read contract (phase 6) | Extend `tests/unit/ports/test_erp_contract.py`: error mapping per code; `list_categories` sorted/empty; `get_availability` unknown is `None` not 0, warehouse filter; currency mismatch omits price; unit not renamed when unmapped; `fetched_at` aware. Vendor sandbox registered with `pytest.mark.integration` (`:4-6`) |
| Mapping | Unit tests for `CatalogueMapping`: exact rename only, unmapped reported, tax basis flag, warehouse sum |
| Sync | Full + incremental sync idempotent; failure keeps last good copy; stale stock flagged; tenant isolation of synced rows (PostgreSQL) |
| Write contract (phase 7) | New `tests/unit/ports/test_erp_writer_contract.py` for every writer: create with reference then lookup returns it; second create with same reference -> `conflict` or same order (never two); lookup of unknown reference -> `None`; release of unknown reservation explicit; tenant isolation |
| Chaos | Simulated writer modes: **timeout after commit** (order exists, call raises `unknown_outcome`) -> operation `unknown` -> reconcile finds it -> exactly one ERP order; worker killed while `sending` -> `unknown`; not found after window -> `needs_attention`, no automatic resend without `idempotency_key` capability |
| Job runner guard | A test that an `ErpError(unknown_outcome)` from the writer never reaches `JobRunner` retry (handler records `unknown`) |
| Duplicate guard | PostgreSQL races: two `create_order` jobs for one acceptance; staff create racing automatic create; accept of version 2 after version 1 accepted is refused (existing) and creates no second order; same `purchase_key` refused |
| Reservation | Reserve ok + create fails conclusively -> released; create unknown -> not released until reconciled |
| Notifications | Status change writes exactly one outbox row per enabled status; disabled status writes none; unknown ERP outcome sends no confirmation |
| Track my order | Intent tests over fakes: other contact's order never shown; several orders -> asks; evaluation cases for the router |
| Migrations | Full suite twice around a downgrade to empty; `0014 -> 0013 -> 0014` with orders present; RLS and grants (append-only events, column-limited updates) |
| Live run | Real API + worker + sandbox ERP: accept -> order -> ERP order visible -> status change -> customer message |

## 9. Proposed changes to shared contracts
**Merged (2026-10-26):** these proposals are now part of [00-shared-contracts.md](00-shared-contracts.md) revision 2; where they differed, the contracts file wins (e.g. one name, `catalogue_reader`, for the catalogue factory method). Points needing the user's decision are listed there in §8.

These must be applied in [00-shared-contracts.md](00-shared-contracts.md) before phases 6/7 start.
| # | Section | Proposal | Why |
|---|---|---|---|
| P1 | §3 Provider | Add optional factory methods `catalogue_source(conn, secrets) -> ErpReader` and `order_writer(conn, secrets) -> ErpWriter` (raise `OperationUnsupported` when not offered) | The Provider protocol only has messaging operations; catalogue/order providers need a way to hand out the port implementation |
| P2 | §1 Capabilities | Allow purpose-specific flags: `live_prices`, `live_stock`, `stock_semantics`, `warehouses`, `webhooks`, `create_order`, `reference_lookup`, `idempotency_key`, `reserve_stock` (sub-dataclass or extra field) | `integrations.md:51` requires declaring reference lookup/idempotency |
| P3 | §4 Migrations | If phase 6 uses sync and needs a sync-run table/source columns: either add `source`, `external_id`, `synced_at` columns to phase 3's `0011_catalogue` now (cheap, recommended) and a sync-run table, **or** renumber to `0014_catalogue_sync` (phase 6) and `0015_orders` (phase 7). Live-only needs no migration and `0014_orders` stays | `0014` is reserved for orders; phase 6 has no number |
| P4 | §5 Job kinds | Add `sync_catalogue` (phase 6, or reuse `poll_connection`), `create_order`, `erp_order_operation`, `reconcile_order_operation` (phase 7) | New background work |
| P5 | §6 Permissions | Add `orders:view` (all staff roles) and `orders:manage` (roles per Q2) | No order permissions exist (`domain/tenancy.py:25-33`) |
| P6 | §7 Portal | Add feature `orders` (phase 7) | New screens |
| P7 | §1 Inbound events | Later add `OrderStatusChanged` and `PaymentEvent` to the `InboundEvent` union | ERP/payment webhooks |
| P8 | §1 Vocabulary | Fix ERP write names to `integrations.md` (`reserve_stock`, `release_reservation`, `create_sales_order`, `get_order_by_external_reference`) plus `get_order_status`; update `06-future-commerce.md:18-21` | Two documents use different names |
| P9 | §1 Purpose | Later purpose for carriers (e.g. `shipments`) when tracking from carriers is wanted | Not in the current list |

## 10. Risks
| Risk | Mitigation |
|---|---|
| Duplicate ERP orders after a timeout | Intent before call, stable reference, reconciliation, no blind retry, chaos tests |
| `JobRunner` retrying a write that may have succeeded | Handler catches and records; dedicated test |
| ERP has no reference field or lookup | Detected in the spike; orders stay local or need manual completion |
| Pilot system is on-premises (Tally, SAP B1) | Requires an agent/tunnel: scope and security reviewed before committing; sync mode preferred |
| Rate caps shared with the business's other tools | Sync mode, caching, backoff on `rate_limited`, alert near cap |
| Stock semantics misread | Declared per connection, shown to staff, never reinterpreted by AI |
| Order status disagreement (ERP vs staff) | One source of truth per order (`fulfilment`), conflicts recorded as events for staff |
| Building rules nobody decided | Phase 7 does not start until Q1-Q16 are answered |
| Customer told "confirmed" when the ERP outcome is unknown | Templates only from `succeeded` operations or staff action |

## 11. Definition of done
**Phase 6**
- D21 answered, spike done, ADR written; 5.6 answers recorded.
- Connector package registered by entry point, passes the extended read contract suite with the simulated source **and** the vendor sandbox (integration marker); no vendor names outside its package.
- Mapping configured in the portal; unmapped values listed; credentials only in `SecretStore`.
- A quotation for a sandbox product is priced from the sandbox end to end (live run); stale stock flagged; ERP outage behaves as today.
- All existing tests, evaluation and migration round trips green; docs and handoff updated.

**Phase 7**
- Q1-Q16 answered and recorded (ADR + open-decisions); this plan updated to match.
- Migration `0014_orders` (or renumbered per P3) with RLS, grants, append-only events and round-trip tests.
- One order per acceptance under concurrency; purchase guard per Q4; chaos test "timeout after commit" ends with exactly one ERP order; unknown outcomes reach `needs_attention`, never auto-resent without idempotency support.
- Local orders work without an ERP; status changes recorded as events; customer messages sent only for enabled statuses and never for unknown outcomes.
- "Track my order" answers only the asking contact's orders.
- Portal orders screens; runbook for `needs_attention`; live run with sandbox ERP; all tests, evaluation and docs green.
