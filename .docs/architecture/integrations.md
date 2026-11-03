# Integration contracts
Contracts below are design schemas unless marked implemented. Model providers are behind the M3 [EnquiryInterpreter](../../src/sales_platform/ports/interpretation.py) port (rules, or any LangChain chat model chosen in settings; ADR 0017). Implemented: the ERP read contract (M1, [ErpReader](../../src/sales_platform/ports/erp.py)), the canonical envelope ([InboundEnvelope](../../src/sales_platform/domain/intake.py), stored per inbox event) and the API channel with `/api/v1/enquiries` create/status (M2, see [enquiry intake](../features/enquiry-intake.md)). For the API channel, `connection_id` is the API key subject and `provider_event_id` is the caller's Idempotency-Key.

## Channel adapters
A channel adapter verifies provider events and maps them into a common envelope:
```json
{
  "schema_version": 1,
  "event_id": "internal-uuid",
  "tenant_id": "resolved-server-side",
  "connection_id": "configured-channel-connection",
  "channel": "telegram",
  "provider_event_id": "provider-stable-id",
  "conversation_id": "provider-thread-or-chat",
  "sender_external_id": "provider-user-id",
  "occurred_at": "2026-10-22T10:00:00Z",
  "received_at": "2026-10-22T10:00:01Z",
  "text": "Please quote 50 industrial fans for Pune",
  "attachments": [],
  "reply_to_external_id": null,
  "correlation_id": "internal-correlation-id"
}
```
The tenant comes from a verified connection or authenticated API membership, never an untrusted tenant field.
Normalize message, edit, delivery, opt-out and callback event types separately. Provider duplicate key: tenant + connection + event type + provider event ID.
Business duplicates across different channels are candidates for review, not automatic deletion based on similar text.
Maintain identities per tenant/channel; link customers across channels only through verified evidence or authorized staff action.

Required operations: verify_event, normalize_event, send_message, upload_or_link_document, normalize_delivery_event.
Capabilities include attachments, buttons, threading, message limits and permitted outbound initiation. Unsupported capability is explicit.
Provider-specific reply windows, consent, template requirements and permissions are adapter policy inputs and must be verified at implementation.
Retries preserve operation identity. Delivery status distinguishes pending, provider_accepted, delivered, failed and unknown. M5 implements quotation delivery with `pending`, `sending`, `available` (pull), `sent` (provider accepted), `failed` and `unknown` ([quotation delivery](../features/quotation-delivery.md)). Since phase 1 ([ADR 0018](../adr/0018-connector-framework.md)) everything we send goes through one outbox (`outbound_messages`, statuses add `delivered` from provider receipts), sent by the connection's provider; inbound callbacks arrive on `POST /api/v1/hooks/{connection_id}/{webhook_token}`, are verified before parsing, stored once per (connection, provider event ID, event type) and routed by event type. See [adding providers](../development/adding-providers.md).

## Manager approval adapters
Manager approval is an additional capability, not automatically granted by general send/receive support. An approval adapter renders a common review packet, verifies callback/message provenance, resolves an enrolled internal identity and maps the interaction to the shared approval decision command.
Declare support for full review detail/document access, explicit decision actions, identity verification, task correlation, expiry and optional message updates. Tenant settings select permitted manager channels independently of customer channels.
Do not route approval events through customer enquiry extraction or ask an LLM to authorize them. Uncorrelated or ambiguous replies remain undecided. Direct email reply approval is confirmed; verify enrolled sender, trusted provider evidence and exact task/version correlation, excluding quoted history and automated messages.
See [approval contracts](approvals.md) and [extension boundaries](module-boundaries.md). Provider-specific permissions/capabilities must be verified before implementing each connector.

## ERP operations
Implemented M1 signatures take a trusted TenantContext and add `get_products(skus)` so quantity rules (unit, minimum, step) come from the ERP. Implemented sources: the simulated ERP (development) and, since phase 3, our own catalogue (`LocalCatalogue`, [ADR 0020](../adr/0020-local-catalogue.md)); both pass the contract suite, in memory and (ours) on PostgreSQL, plus a shared search-quality table (`tests/data/catalogue_search_cases.json`). Customer-specific pricing (`customer_context`), requested quantities in availability and discount basis are deferred. Unknown SKUs are omitted, never zero-filled. The [contract suite](../../tests/unit/ports/test_erp_contract.py) applies to every adapter.
Required read operations:
- search_products(query, customer_context): SKU candidates with authoritative IDs, units and descriptions.
- get_prices(skus, customer, currency, effective_at): price list/version, effective dates, currency, units and discount basis.
- get_availability(skus, quantities, warehouse): on-hand/reserved/available semantics, source timestamp and availability confidence.
Optional write operations: reserve_stock, release_reservation, create_sales_order, get_order_by_external_reference.
Delivery promises require a supported business rule or delivery source; stock alone does not prove arrival by a requested date.

Results include source system, fetched_at, external version where available, and normalized errors:
not_found, unsupported, unauthenticated, forbidden, rate_limited, temporary_unavailable, invalid_request, conflict, unknown_outcome.
A tenant connector declares capabilities and whether external idempotency/reference lookup is supported.
If safe duplicate prevention/reconciliation is impossible, order creation requires a manual completion path instead of blind retry.

## Caching and mapping
Caches are tenant/connection scoped, timestamped and disposable. Staleness policy is configured per operation.
Normalize units, currency, customer-specific price lists, warehouses and tax identifiers through documented mappings.
AI cannot infer missing unit conversion, tax policy or inventory semantics.
ERP webhooks may invalidate caches; reads still revalidate material facts before order commitment.

## Planned API surface
Versioned /api/v1 routes: enquiries (create/status), quotations (read/version), approvals (decision), acceptances (submit), integrations (admin), and provider-specific webhook routes.
Create/decision APIs support idempotency keys; version conflicts return explicit conflict responses.
Webhook routes verify provider authentication; portal/API routes verify user or service identity and tenant membership.
Write OpenAPI examples and exact status codes alongside implementation; this list is not an existing API specification.
