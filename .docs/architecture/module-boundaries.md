# Module boundaries and extension rules
Status: implemented as business modules (ADR 0026, 2026-11-03), enforced by [test_architecture.py](../../tests/unit/test_architecture.py). The modules and what each owns are in the [code map](../development/code-map.md#business-modules-adr-0026).

## The rules (ADR 0026)
1. **Module order:** `platform` → `integrations` → `accounts` → `catalogue` → `conversations` → `enquiries` → `quotations` → `orders` → `customer_channels`. A module imports only modules before it, so no two modules depend on each other.
2. **Layers inside a module:** domain uses only domain; ports use domain and ports; application uses domain, ports and application; adapters, persistence and api follow the same table as before (frameworks only at the edges). An import of an earlier module must respect the same layer table.
3. **Provider packages** (`providers/`) may use any module; nothing imports them except the wiring, by entry point. Provider names and HTTP clients stay inside them (named exceptions in the test).
4. **When a lower module needs something from a higher one, the dependency is inverted:**
   - inbound routes are registered by the wiring (`routes_for`);
   - the outbox calls `OutboxListener` hooks (conversation thread, quotation deliveries) in its own transaction;
   - the catalogue's delete guard calls `ProductInUse` checks (quotations: "was this SKU quoted?");
   - plug-in contracts live with their module (`catalogue/ports/sources.py`, `orders/ports/writers.py`);
   - the job runner retries any `platform` `ServiceError` (catalogue's `ErpError` is one).
5. **One documented database-level read:** follow-up routing (`enquiries/persistence/follow_ups.py`) checks, inside its query, whether a quotation exists, by naming the quotations table read-only, without importing quotations' code.
6. **Only the wiring builds SQL stores** (`composition.py`, `api_app.py`, `worker.py`, `seed.py`).

Historical sections follow.

## What independent means
Modules have explicit inputs, outputs and responsibilities, and can be tested with fake dependencies. They remain in one application initially. Independence does not mean every module needs its own server, database or AI agent.

| Module | Owns | Calls through contracts |
|---|---|---|
| Channel transport | Provider verification, message rendering and event mapping | Identity resolver, enquiry intake or approval service |
| Identity/authorization | Tenant membership, channel identity binding and permissions | Membership/binding repositories |
| Enquiry | Requirements and clarification lifecycle | Model extraction and catalogue ports |
| Quotation | Immutable versions and commercial validation | Pricing/ERP ports |
| Approval | Review task, authorized decision and audit | Identity/policy, notification port, durable event/outbox storage |
| Delivery | Approved document rendering and dispatch status | Storage and channel ports |
| Order | Verified acceptance, revalidation and external order operation | ERP ports and quotation service |
| Negotiation (later) | Counteroffer history and proposal lifecycle | Existing quotation, approval and delivery services |
| Manager commands (later) | Verified stock enquiries and scoped price/discount requests | Identity, ERP read ports, quotation/pricing and approval services |
| Workflow orchestration | Connect stages and resume on durable events | Application services, not provider SDKs |

## Shared contracts
An approval service exposes request_review(quote_version, approval_policy) and decide(verified_actor, decision_command). A notification port sends the review packet. An inbound adapter maps verified provider actions to decision_command.
The approval service returns recorded decision/conflict/validation failure and persists ApprovalDecided plus a durable resume intent. It does not directly send provider messages or call LangGraph inside a DB transaction.
Quotation services create a draft from validated commercial inputs; callers cannot directly mutate a sent/approved version. Future negotiation uses this same boundary.
Events carry schema version, tenant, event/operation ID, aggregate ID/version and correlation ID. Consumers deduplicate events and use repositories scoped to trusted tenant context. These are logical contracts, not a requirement to introduce an event-broker product.

## Example: adding WhatsApp approval
1. Verify provider capabilities and account access.
2. Implement/reuse transport verification and manager identity binding.
3. Render the common review packet and map actions to the existing decision command.
4. Register connector capabilities and tenant configuration.
5. Run approval contract tests and provider sandbox tests; document setup.
Expected changes are confined to adapter, composition/configuration, tests and documentation. The same quotation rules and graph resume path remain unchanged.
If a provider cannot express the contract, return unsupported or propose a versioned contract extension with an ADR; never weaken authorization to make an adapter fit.

## Dependency checks
Domain modules cannot import provider SDKs, portal routes or other modules' persistence internals. Provider registration belongs in the composition root. Introduce architecture/import checks once source modules exist, plus fake-port unit tests and shared adapter suites.
