# Planned execution flow
The sales workflow table below remains a target. M0-M3 implement only the traces described here.

## Implemented M0 trace
1. Uvicorn calls [create_app](../../src/sales_platform/api_app.py); [Settings](../../src/sales_platform/platform/config.py) validates configuration and hides secrets.
2. [build_engine](../../src/sales_platform/platform/persistence/database.py) configures a bounded pool. Creating the app does not connect, migrate or seed the database.
3. GET /health/live returns 200 without a database query.
4. GET /health/ready reads the schema revision and checks the connected role. Missing/unreachable/unmigrated DB, unexpected revision or privileged role returns sanitized 503; a valid connection/schema/role returns 200.
5. Application shutdown disposes the engine.
6. Separately, Alembic uses migration credentials to create the tenant table and RLS. The [seed](../../src/sales_platform/seed.py) sets transaction-local tenant context and inserts fictional fixtures idempotently; it refuses production mode.
No business HTTP routes, authenticated tenant context, workflow engine or external actions exist yet. Migration, seed, RLS and readiness behaviour were executed against local PostgreSQL on 2026-10-22. See [setup](../development/setup.md).

## Implemented M1 trace (pricing a draft)
Entry point: `QuotationPricingService.price`; see [quotation pricing](../features/quotation-pricing.md). No HTTP route calls it yet.
1. [TenantContextResolver](../../src/sales_platform/platform/application/tenancy.py) asks the membership repository for the subject's roles inside a transaction whose tenant context is set. No roles: `AccessDenied`.
2. The service requires `quotations:draft`; viewers and approvers get `PermissionDenied` before any ERP call.
3. The tenant's pricing policy is read through RLS; a missing policy raises `PricingPolicyNotConfigured`.
4. The ERP port returns products, prices effective now in the policy currency, and stock, all stamped with source and fetch time. ERP errors propagate.
5. [calculate_quotation](../../src/sales_platform/quotations/domain/pricing.py) validates every line, computes rounded line totals, a subtotal, tax on the subtotal and a total, and attaches price evidence plus one of five stock statuses. Any blocking issue returns `PricingRejected` listing all issues.
Nothing is stored or sent. Persisted quotation versions arrive with approval (M4).

## Implemented M2 trace (enquiry intake)
Entry point: `POST /api/v1/enquiries`. See [enquiry intake](../features/enquiry-intake.md).
1. The bearer API key is verified ([ApiKeyAuthenticator](../../src/sales_platform/accounts/application/api_keys.py)), which yields the tenant and role. The body is size-checked and validated, and `enquiries:submit` is required.
2. An existing receipt for (tenant, api, key, Idempotency-Key) is returned as a replay (200), or rejected with 409 if the payload fingerprint differs.
3. Attachments are written to `tenants/<tenant>/quarantine/<id>`.
4. One transaction inserts the inbox event, enquiry, attachment rows and a `prepare_enquiry` job. If a concurrent duplicate wins, our files are deleted and its receipt is returned. On failure, files are deleted and the API returns 503.
5. The API returns 202 after the commit.
6. The worker claims the job with a lease (`FOR UPDATE SKIP LOCKED`) and sets the job's tenant context. It inspects each file: clean ones move to `clean/`, rejected ones stay quarantined. The enquiry becomes `ready_for_extraction` or `needs_review`, and the job completes, fenced by its lease token.
7. Failures retry with backoff, up to 5 attempts, then `dead`. A crashed worker's job is reclaimed when its lease expires.
Implemented reliability: durable receipt before acknowledgement, inbox dedupe, an atomic business change plus job intent, leased and fenced jobs, and idempotent handlers. Not yet implemented: the outbox for external sends (M5). Graph resume jobs arrived in M4.

## Implemented M3 trace (requirement extraction)
Entry point: the `extract_requirements` job, queued atomically by preparation when all attachments are clean. See [enquiry extraction](../features/enquiry-extraction.md).
1. ExtractionJobHandler gets or creates the tenant's `workflow_runs` row (thread ID), then starts the graph, or resumes it if a checkpoint shows unfinished steps.
2. **extract:** read the enquiry text and clean attachments, then call the interpreter. A verbatim quote check flags invented items; invalid output skips to review.
3. **match:** an ERP search per verified item. The interpreter may choose only among ERP candidates.
4. **assess:** deterministic outcome with template questions or review reasons.
5. **ready / clarify / review:** store the outcome in `workflow_runs` and update the enquiry status. Each step is checkpointed in PostgreSQL.
A ready outcome also queues `draft_quotation` in the same transaction (M4). Not yet implemented: sending clarification questions (with the email/Telegram channels, M7).

## Implemented M4 trace (drafting and approval)
Entry points:
- the `draft_quotation`, `resume_quotation` and `notify_approvers` jobs;
- the portal API;
- the simulated channel webhook.

See [quotation approval](../features/quotation-approval.md).
1. QuotationJobHandler takes a per-run advisory lock. It then starts the `quotation-approval-v1` graph, or recovers a crashed step.
2. **draft:** price the extracted lines with the M1 calculator, using a system sales identity. Store version 1, or `pricing_failed` with the issues.
3. **request_approval:** create the version's task once and queue `notify_approvers`. **await_decision** then interrupts; its state is kept in PostgreSQL.
4. **notify_approvers:** store a reference hash for each bound current approver, then send the review packet (simulated outbox).
5. **Decision:** a portal request (session + CSRF) or a signed channel event becomes a DecisionCommand. `ApprovalService.decide` locks the task and re-checks:
   - membership and permission;
   - task status;
   - content hash and current version;
   - self-approval.

   It then atomically records the decision, the task and quotation status, the reference consumption and a `resume_quotation` job.
6. **Resume:** a resume job continues the pause only if it matches it (the task, or a newer version); otherwise it is ignored. Approve and reject end the run; request_revision pauses again for a revision.
7. **Revision:** the sales revision is re-priced and stored as version N+1 (optimistic check) with a resume job. The graph then requests approval for it.

Not yet implemented: real channel adapters (M7), and deadlines/escalation.

## Implemented M5 trace (approved PDF and delivery)
See [quotation delivery](../features/quotation-delivery.md).
1. **approved** (graph step):
   - Only if the version is the quotation's approved, current version.
   - Insert one `quotation_deliveries` row (`pending`) for the enquiry's channel and sender, and queue `deliver_quotation`, in one transaction. Then end the run.
2. **deliver_quotation:**
   - Nothing to do if final. A leftover `sending` becomes `unknown` and is never resent.
   - Get the document: reuse the stored one (hash re-checked), or check the approve decision for this exact version and hash, render deterministically, store it and record it.
   - Compare-and-set `pending` -> `sending`, then call the channel adapter.
   - Record `available`/`sent`, `failed` (with reason), `unknown`, or `pending` again for a retry (`failed` after the last attempt).
3. **Customer (API channel):** `GET /api/v1/enquiries/{id}/quotation(.pdf)` returns only delivered approved versions and records the first download.

## Implemented M6 trace (customer answers)
See [customer acceptance](../features/customer-acceptance.md).
1. **Delivery:**
   - Prints "valid until" on the PDF (preparation time + tenant days).
   - On `available`/`sent`, queues `expire_quotation` for that moment.
   - The run waits at `await_customer`.
2. **Answer** (API key, Idempotency-Key, exact version and hash):
   - A decline is recorded.
   - An accept re-prices from the ERP. Unchanged: `accepted`. Any difference: `refused_changed` plus version N+1 (or `pricing_failed`). ERP down: nothing recorded.
   - Recording takes a row lock on the quotation, re-checks that the offer is open, and writes the answer, status, revision, `resume_quotation` and `notify_response` atomically.
3. **Resume:**
   - Closing answers end the run with their status.
   - A price change continues to `request_approval` (or `await_revision`); approval -> delivery -> a new offer.
4. **Expiry job / withdrawal:** the same atomic recording; the run ends `expired` / `withdrawn`.

## Implemented phase 1 trace (connector framework, ADR 0018)
1. A provider calls `POST /api/v1/hooks/{connection}/{token}`. The token, the size and the provider's signature are checked, then the body is parsed. Each event goes into `inbox_events` once, with a `process_inbound` job, in one transaction; the answer is 200.
2. The worker runs `process_inbound`: a manager action goes through `ChannelActionService` and `ApprovalService` (as in the portal). The outcome is stored on the inbox event.
3. `notify_approvers`, `deliver_quotation` and `notify_response` queue `outbound_messages` (idempotency key per message) and `send_message` jobs. `send_message` runs the template's send hook (approval token at send time; verified PDF), compare-and-sets pending to sending, and calls the connection's provider. A linked delivery mirrors the status, and expiry is queued as before.
4. Product reads go to the business's own catalogue connection (`TenantCatalogue`). With no source, the enquiry goes to review at once.

## Planned sales workflow

| Step | Input and action | Output / failure branch |
|---|---|---|
| 1 Receive | Verify provider authentication and connection; normalize message | Reject invalid event; store inbox event and job atomically before acknowledgement |
| 2 Dispatch | Lease job; resolve tenant/run; serialize execution | Duplicate receipt returns previous status; concurrent jobs wait/retry safely |
| 3 Extract | Read bounded text/attachments; model produces validated requirements | Missing fields -> clarification; unsafe/invalid output -> review |
| 4 Resolve products | Search ERP; select supported matches with evidence | Ambiguous/no match -> ask or review; never invent SKU |
| 5 Retrieve facts | Fetch prices, units, stock and delivery inputs | Missing/stale/failed lookup -> retry or exception; no fallback invented values |
| 6 Calculate | Deterministic Decimal arithmetic and tenant policy | Draft immutable version with calculation explanation |
| 7 Request approval | Shared approval service persists task + review-packet outbox intent for configured manager channels; graph interrupt | Portal or supported in-channel review; pending state survives process restart |
| 8 Resume | Portal/channel adapter submits verified decision to the same approval service; authorized version-bound decision creates resume job | Reject -> closed/revision; approve -> validate and prepare delivery; stale/unauthorized action fails |
| 9 Deliver | Render approved version PDF; persist immutable object; send via outbox | Track acknowledgement/delivery; unknown result -> reconcile |
| 10 Await response | Persist wait and due follow-up jobs | Reply -> correlate; counteroffer -> human review initially, future negotiation module later; reject/expiry/opt-out -> stop follow-ups |
| 11 Accept | Verify customer/version; revalidate ERP facts | Changed material terms -> new quote; duplicate -> existing result |
| 12 Commit order | Reserve if supported; create with stable external reference | Store ERP ID; partial/unknown result -> reconciliation |
| 13 Finish | Persist confirmed ERP order and customer confirmation intent; optional CRM synchronization | Notification/CRM failures retry independently; never recreate successful order or announce unknown outcome as success |

## State design
Store tenant/run/enquiry IDs, normalized requirements, evidence references, quote version, approval/acceptance references, pending operation IDs, status and schema version.
Keep large documents and secrets outside graph state. Checkpoints may contain PII and require retention/access controls.
Business status and checkpoint state are related but separate; UI uses authoritative business records.
Prefer short nodes with explicit input/output contracts. Separate model reasoning from external writes.

## Resume and side effects
Interrupted nodes can run again. No non-idempotent send/order write may rely on "this node ran only once."
Commit stable operation records before calling remote systems; reconcile operation completion on replay.
A crash after decision persistence but before graph resume is recovered from the durable resume job.
A crash after remote success but before local acknowledgement requires reference/status lookup, not blind resend.
Use bounded exponential backoff with jitter, error classification, attempt limits and a visible dead-letter/exception queue.
Jobs use leases, lease renewal for long calls, expiry recovery and fencing/version checks to reject stale worker commits.
Lock/serialize per workflow, including approval and customer reply races. Recheck cancellation/version immediately before an irreversible intent is dispatched.

## Order partial failures
Before automatic order creation, enforce a business-level purchase identity across quote revisions. Repeated acceptance of another version cannot silently create a second order for the same intended purchase. Partial acceptance, quantity edits and substitutions route to reviewed revision until explicitly supported.
If reservation succeeds but order creation fails conclusively, release or expire the reservation according to ERP policy.
If creation outcome is unknown, reconcile before releasing/retrying; do not cancel stock belonging to an order that actually exists.
Record compensating actions and unresolved exceptions for an operator.

## Human ownership and cancellation
Persist manual owner, pause reason and resume authority. Human takeover suspends automated commercial writes and ordinary follow-ups; resume is authorized and audited. Pausing cannot undo a remote operation already sent, so reconcile in-flight/unknown outcomes.
Cancellation/withdrawal before order commitment stops eligible pending actions and invalidates relevant approval/acceptance actions under the configured offer policy. A completed ERP order cannot be undone by editing workflow state: route later changes/cancellation to an explicit human/ERP process.
Preserve the calculation policy snapshot and recheck current permissions at execution. Before M6, define which policy/source changes require recalculation; any material revised offer requires fresh manager approval and customer acceptance.

## Extension routing
Manager channel adapters never resume the graph directly; an accepted decision persists the durable resume intent. Changing approval channels does not change the graph's decision contract.
Negotiation is deferred: see [future negotiation](../plan/negotiation.md). When implemented, its proposals use existing quotation/version/approval/delivery services and serialize against customer acceptance/order commitment.
Every negotiated revision requires manager approval. Future [manager enquiries/commands](../plan/manager-operations.md) route verified stock questions to read-only ERP lookups and scoped price/discount requests to validated proposed revisions; they do not bypass approval or imply catalogue-write authority.

## Workflow upgrades and version compatibility
Persist graph/schema version. Keep compatible handlers for waiting runs or provide tested migrations; never assume a deployment can resume old checkpoints safely.
