# Testing and AI evaluation
## Test layout
```text
tests/
  conftest.py          Shared options, fixtures and in-memory fakes for every test
  unit/                Offline; folders mirror src/sales_platform
    conftest.py        M4/M5 in-memory quotation/approval/channel/session/document store and harness
    domain/  application/  ports/  workflows/  api/  persistence/
    adapters/erp/  adapters/storage/  adapters/documents/  adapters/interpreters/
    adapters/identity/  (covered through application/api tests)
    adapters/providers/simulated/  providers/ (provider contract suite for every provider)
    test_config.py  test_architecture.py
  integration/         Real PostgreSQL (sales_test only); auto-marked `integration`
    conftest.py        Database settings, engines, cleanup, API-key and storage fixtures
    api/  persistence/  workflows/

portal/
  src/                 React source only (no test files); see portal-architecture.md
  tests/               Vitest tests; folders mirror portal/src
    setup.ts           Shared test setup (cleanup, mocks)
    support/           renderWithProviders, test session, JSON responses (helpers, not mirrors)
    architecture.test.ts   enforces layers, feature boundaries and "no tests in src"
    shared/api/  shared/lib/
    features/<feature>/<layer>/...   e.g. features/quotations/ui/components/DecisionPanel.test.tsx
```
Rules:
- A unit test for `src/sales_platform/<path>/<module>.py` lives in `tests/unit/<path>/test_<module>.py`. Integration tests follow the same mirroring under `tests/integration/`.
- The same applies to the portal: a test for `portal/src/<path>/<Name>.tsx` lives in `portal/tests/<path>/<Name>.test.tsx`. Test files never sit next to UI code; Vitest only collects `portal/tests/**`.
- `tests/` has no `__init__.py` files. Pytest runs with `--import-mode=importlib` and mypy with `explicit_package_bases`, so names such as `domain/test_pricing.py` and `application/test_pricing.py` can coexist.
- Shared setup lives in fixtures, not copied helpers. Test modules never import `conftest.py`.
- Every test under `tests/integration/` is marked `integration` automatically. A test elsewhere can opt in with the marker, for example a future real-ERP contract case.
- **Safety:** integration tests empty the intake and job tables before each test, so every `TEST_*` URL must name a database ending in `_test`. Otherwise the run fails immediately. Development data in `sales_dev` is never touched.

Shared fixtures in [tests/conftest.py](../../tests/conftest.py):

| Fixture | Provides |
|---|---|
| `now` | Fixed timezone-aware "current time" used by test clocks |
| `industrial_tenant`, `office_tenant` | The two seeded demo tenant IDs |
| `make_context(tenant_id, role)` | A TenantContext with one role |
| `make_policy(tenant_id, currency)` | Synthetic pricing policy (2 decimals, half_up, 18% tax, 15/5 min freshness) |
| `simulated_erp` | Demo catalogues for both tenants, ERP clock fixed at `now`; `.failures` injects errors |
| `membership_reader`, `policy_reader` | In-memory fakes; tests edit `.rows` / `.policies` |
| `memory_storage` | In-memory ObjectStorage; `.objects` to inspect, `.fail_puts` to simulate disk errors |
| `intake_store` | In-memory IntakeStore; `.fail_record` and `.concurrent_winner` simulate failures and lost races |
| `api_key_reader` | In-memory ApiKeyReader; tests add records to `.records` |
| `make_pdf(lines)` | Builds a small synthetic PDF with a real text layer |

[tests/unit/conftest.py](../../tests/unit/conftest.py) adds:

| Fixture | Provides |
|---|---|
| `quotation_db` | MemoryQuotations: one in-memory store implementing every M4/M5 port. It enforces one task per version, one decision per task, unique commands, optimistic revisions, one delivery per target and compare-and-set delivery statuses. `.fail_next` injects failures; `.jobs` shows queued jobs; `.senders` sets an enquiry's channel and sender |
| `harness` | Real ApprovalService, QuotationService, ChannelActionService, quotation graph (InMemorySaver), delivery handler (real ReportLab renderer, `memory_storage`) and job handlers over `quotation_db`. `run_jobs()` drains jobs; `handler()` builds a fresh graph, as after a restart; `deliveries()` and `documents()` build the M5 handler and service; `email` is a push-channel fake whose `.outcome` injects rejected, uncertain or transient results |
| `memory_sessions`, `memory_directory` | SessionStore and MembershipDirectory views of `quotation_db` |

[tests/integration/conftest.py](../../tests/integration/conftest.py) adds:

| Fixture | Provides |
|---|---|
| `integration_settings` | Settings built only from `TEST_*` URLs (checked for `_test`), with storage in a temporary folder |
| `owner_engine`, `runtime_engine`, `worker_engine` | One engine per database role; `runtime_engine` also seeds demo data |
| `clean_intake_tables` (automatic) | Truncates intake and job tables before each test |
| `make_api_key(tenant_id, role)` | Creates a key with owner credentials and returns its token |
| `fresh_accounts` | For tests that create people and businesses: afterwards truncates tenants and users (CASCADE) and seeds the demo again |
| `storage` | LocalObjectStorage under the temporary storage folder |

## Current checks (M0-M6)
Run uv run ruff check ., uv run ruff format --check ., uv run mypy and uv run pytest from the root; `uv run pytest tests/unit` runs only the unit tests.

Result on 2026-10-23 (M6):
- **Offline:** 327 unit tests passed and 44 integration tests were skipped.
- **PostgreSQL** (`check_database.py --cycle`): all 371 passed, then passed again after a downgrade to empty and re-upgrade.
- **Portal:** `cd portal; npm run typecheck; npm test; npm run build` passed (41 tests).

M6 adds:
- **Acceptance tests:** price-change revision through re-approval, expiry with a controllable harness clock (`harness.advance`; delayed jobs wait for their `run_at`), withdrawal roles, and replay/conflict.
- **PostgreSQL tests:** an accept/withdraw race, expiry run by the worker role, and append-only answers.

M5 results (same day): 296 unit, 335 with PostgreSQL, 36 portal.

M5 adds:
- **PDF content checks:** pypdf extracts the text and compares it with the approved version.
- **Delivery outcome tests:** rejected, uncertain, transient, crash while sending, no adapter, tampered file and unrenderable content.
- **PostgreSQL tests:** append-only documents and delivery column grants. Run `uv sync --locked --all-extras` first so the optional AI packages are installed.

| Suite | Checks |
|---|---|
| [unit/domain/test_pricing.py](../../tests/unit/quotations/domain/test_pricing.py) | Exact totals, tenant rounding mode, absent price never zero, quantity/minimum/step/unit rules, stale/not-effective/wrong-currency/wrong-unit prices, five stock statuses, all issues reported together |
| [unit/domain/test_money.py](../../tests/unit/platform/domain/test_money.py) / [unit/domain/test_tenancy.py](../../tests/unit/platform/domain/test_tenancy.py) | Float rejection, rounding, precision-loss trap, role permissions |
| [unit/domain/test_intake.py](../../tests/unit/enquiries/domain/test_intake.py), [test_attachments.py](../../tests/unit/enquiries/domain/test_attachments.py), [test_jobs.py](../../tests/unit/platform/domain/test_jobs.py) | Intake limits, fingerprint sensitivity, idempotency-key rules, envelope; type sniffing, renamed/binary/encrypted/active-content files rejected; backoff doubling, cap and jitter |
| [unit/application/test_pricing.py](../../tests/unit/quotations/application/test_pricing.py) | Permission checked before ERP calls, cross-tenant SKU invisible, ERP errors propagate, stale ERP data, missing/foreign policy refused |
| [unit/application/test_tenancy.py](../../tests/unit/platform/application/test_tenancy.py) | Membership-based tenant resolution |
| [unit/application/test_intake.py](../../tests/unit/enquiries/application/test_intake.py) | Quarantine write, replay returns original, conflict, cleanup after database/storage failure, lost race returns winner, permission, tenant-scoped status |
| [unit/application/test_api_keys.py](../../tests/unit/accounts/application/test_api_keys.py) | Context from key, only hash stored, identical failure for wrong/revoked/unknown/malformed keys |
| [unit/application/test_preparation.py](../../tests/unit/enquiries/application/test_preparation.py) | Clean files released, rejected files stay quarantined, needs_review, crash-between-move-and-record recovery, permanent errors |
| [unit/application/test_jobs.py](../../tests/unit/platform/application/test_jobs.py) | Complete, retry with backoff (error type only), dead after last attempt, permanent error, unknown kind, lost lease tolerated |
| [unit/ports/test_erp_contract.py](../../tests/unit/catalogue/ports/test_erp_contract.py) | Shared ERP contract; register new adapters in `ADAPTERS` |
| [unit/adapters/erp/test_simulated.py](../../tests/unit/catalogue/adapters/erp/test_simulated.py) | Simulated-ERP specifics: unconfigured tenant, failure injection, unknown stock, warehouse, invalid search |
| [unit/adapters/storage/test_local.py](../../tests/unit/platform/adapters/storage/test_local.py) | Put/get/move/delete, no leftover temp files, path traversal and non-generated keys rejected |
| [unit/api/test_enquiries.py](../../tests/unit/customer_channels/api/test_enquiries.py) | 202/200/409 replay contract, tenant-scoped 404, 401 variants, `tenant_id` in body rejected, 422/403, missing key, 413, 503 stores nothing |
| [unit/api/test_app.py](../../tests/unit/test_api_app.py), [unit/test_config.py](../../tests/unit/test_config.py), [unit/persistence/test_seed.py](../../tests/unit/test_seed.py) | Health endpoints, settings validation, production seed refusal |
| [unit/test_architecture.py](../../tests/unit/test_architecture.py) | Domain/ports/application stay stdlib-only; providers only in adapters; LangGraph only in workflows; persistence and api layers checked too, with named exceptions; only the wiring (`composition.py`, `worker.py`, `api/app.py`, `persistence/seed.py`) builds SQL stores (ADR 0026 step 1) |
| [unit/domain/test_requirements.py](../../tests/unit/enquiries/domain/test_requirements.py) | Unit aliases, invented quotes/products/numbers flagged, ERP-only matching, ready/clarify/review rules and template questions |
| [unit/adapters/interpreters/](../../tests/unit/enquiries/adapters/interpreters/test_rules.py) | Rules phrasing and verbatim quotes; LangChain adapter with a scripted fake model (valid JSON, price/discount fields rejected, tag break-out neutralised); provider factory |
| [unit/adapters/documents/test_text.py](../../tests/unit/enquiries/adapters/documents/test_text.py) | PDF text read; scanned, encrypted, broken PDFs, images and oversized text reported, never emptied |
| [unit/workflows/test_enquiry.py](../../tests/unit/enquiries/workflows/test_enquiry.py) | Graph branches, PDF attachment reading, invented item and invalid answer go to review, crash resumes without a second model call, finished runs not repeated, other tenant's products and enquiries invisible |
| [unit/persistence/test_checkpoint_schema.py](../../tests/unit/platform/persistence/test_checkpoint_schema.py) | Frozen LangGraph checkpoint schema equals the installed library's |
| [unit/domain/test_quotations.py](../../tests/unit/quotations/domain/test_quotations.py) | Exact version content, stable content hash, decision command validation, review text |
| [unit/workflows/test_quotation.py](../../tests/unit/quotations/workflows/test_quotation.py) | Draft, interrupt, approve and reject; revision loop; pricing failure waits for a revision; duplicate and stale resumes ignored; early resume and run lock retried; crash resumes without re-pricing; ERP outage retried |
| [unit/application/test_approvals.py](../../tests/unit/quotations/application/test_approvals.py) | Replay, conflict, reused command, only approvers, other tenants, removed membership, stale hash, self-approval, revision rules and isolation |
| [unit/application/test_channel_actions.py](../../tests/unit/quotations/application/test_channel_actions.py) | Enrollment code rules, channel approval, forwarded or other connection refused, revoked binding, expired reference, lost role, only current approvers notified, failed delivery retried with a fresh token |
| [unit/application/test_sessions.py](../../tests/unit/platform/application/test_sessions.py) | Hashed tokens, expiry, logout, CSRF |
| [unit/api/test_portal.py](../../tests/unit/quotations/api/test_portal.py) | Cookie flags, dev-login rules, CSRF on changes, non-member 404, decision replay/conflict/stale, revision 201/200/409/422, signed webhook (unsigned or tampered gets 401), binding revocation |
| [portal/tests/](../../portal/tests/architecture.test.ts) | Architecture rules; HTTP client (CSRF only on changes, query strings, error mapping); exact formatting; quotation service rules; decision panel (same command ID on retry, reason required, refresh on conflict); current tenant (fallback, remembered choice, removed membership); route guard |
| portal `tests/features/home/`, `tests/app/layout/navigation.test.ts` | Home cards per permission, 50+ cap, never a false 0, All done only when every card is 0, no request for lists the role may not open; menu per role and Get started while setting up (ADR 0025) |
| portal `tests/app/layout/settingsNavigation.test.ts`, `SettingsLayout.test.tsx`, `tests/features/connections/services/integrations.test.ts` | Settings sections per role, first-section redirect, no-settings message; tools grouped by kind with switched-off ones apart; Coming soon hidden once installed or connected |
| [integration/persistence/test_tenancy.py](../../tests/integration/platform/persistence/test_tenancy.py) | Runtime role: cross-tenant membership denied, rows invisible without/across context, cannot add memberships or change policies, per-tenant policies, end-to-end pricing |
| [integration/api/test_enquiries.py](../../tests/integration/customer_channels/api/test_enquiries.py) | API to worker to status end to end; replay/conflict store one row; 8 concurrent duplicates create exactly one enquiry and one file; failed transaction leaves no rows or files; cross-tenant 404, revoked key 401; API role cannot read jobs |
| [integration/persistence/test_jobs.py](../../tests/integration/platform/persistence/test_jobs.py) | Cross-tenant claims with SKIP LOCKED, crashed worker reclaimed and stale completion rejected, backoff delays claims, expired final attempt becomes dead, worker role sees only the queue |
| [integration/persistence/test_api_keys.py](../../tests/integration/accounts/persistence/test_api_keys.py) | API role sees no keys, or only the presented one |
| [integration/workflows/test_enquiry.py](../../tests/integration/enquiries/workflows/test_enquiry.py) | API to worker to LangGraph (PostgreSQL checkpoints) to extracted lines (incl. a PDF attachment) priced by the M1 calculator; clarification path; resume after a simulated worker restart without a second model call; API role cannot read checkpoints; runs isolated per tenant |
| [integration/workflows/test_quotation.py](../../tests/integration/quotations/workflows/test_quotation.py) | Enquiry, draft, portal approval, resume; manager/sales revision loop; simulated channel enroll/deliver/approve with forwarded refusal and replay; two concurrent decisions record one; resume after worker restart; pricing failure revised; other tenant 404; versions and decisions append-only (grants and trigger); session and membership RLS |
| [integration/persistence/test_database.py](../../tests/integration/platform/persistence/test_database.py), [test_seed.py](../../tests/integration/test_seed.py), [integration/api/test_app.py](../../tests/integration/test_api_app.py) | Readiness and owner-role rejection, idempotent seed, tenant visibility, context cleared on pooled connections, denied runtime writes |

Run [check_database.py](../../scripts/check_database.py) only after local PostgreSQL starts. It migrates and checks `sales_dev` and `sales_test`, then runs every test against `sales_test`. With integration tests enabled, missing or non-`_test` URLs make the run fail; by default they are skipped visibly.

CI runs `check_database.py --cycle` on a fresh environment. Remote GitHub Actions [run 39734937763](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39734937763) passed the M0 sequence; [CI run 39738532222](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39738532222) and [CI run 39741408875](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39741408875) passed for M1 and M2, and [CI run 39747434193](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39747434193) passed for M3. [CI run 39900219005](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39900219005) passed for M4, in both the `verify` and the new `portal` job. [CI run 39903715921](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39903715921) passed for M5 (both jobs). [CI run 39907493342](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39907493342) passed for M6 (both jobs).

### Phase 3 product catalogue (ADR 0020)
| Suite | Checks |
|---|---|
| [unit/domain/test_catalogue_search.py](../../tests/unit/catalogue/domain/test_catalogue_search.py) | The shared word rule: hyphen compounds, plurals (not `ss`), two-letter minimum, best matches in code-point order |
| [unit/domain/test_catalogue_admin.py](../../tests/unit/catalogue/domain/test_catalogue_admin.py) | Every editing rule (SKU, units, decimals, minimum multiple of step, unit lock, price > 0 and not in the past, stock as of not in the future); price selection with scheduled prices and same-start ties; latest count; staleness; change lists |
| [unit/ports/test_erp_contract.py](../../tests/unit/catalogue/ports/test_erp_contract.py) | Also `local-memory`; now also invalid search, sorted own categories and the shared search cases ([tests/data/catalogue_search_cases.json](../../tests/data/catalogue_search_cases.json)) for every source |
| [unit/adapters/catalogue/test_local.py](../../tests/unit/catalogue/adapters/catalogue/test_local.py) | Evidence (standard list, revision, read time; stock as of), scheduled price ends the current one, archived products and categories disappear, staff notes never searched, store failure is a retryable ERP error, the provider |
| [unit/application/test_catalogue_admin.py](../../tests/unit/catalogue/application/test_catalogue_admin.py) | Who may read and edit, read-only sources, suspended business, currency and minor units, replays, staleness, unit lock, archive/restore/delete, list with prices and stock, categories |
| [unit/providers/test_provider_contract.py](../../tests/unit/providers/test_provider_contract.py) | Also `local_catalogue` (no webhooks, no sending, a working reader) |
| [integration/persistence/test_catalogue.py](../../tests/integration/catalogue/persistence/test_catalogue.py) | Seeded local demo reads like the simulated ERP; RLS on every table and WITH CHECK; cross-business category refused; worker read-only; prices and stock cannot change (grants and trigger); create/update/archive and audit; unit lock and delete rule; concurrent prices get revisions 1 and 2; replays; decimals kept exactly; counts; limit; category rename/archive refresh search words |
| [integration/adapters/catalogue/test_local_catalogue.py](../../tests/integration/catalogue/adapters/catalogue/test_local_catalogue.py) | Search cases and contract checks on PostgreSQL with both roles; each business routed to its own source |
| [integration/api/test_catalogue.py](../../tests/integration/catalogue/api/test_catalogue.py) | Real session and CSRF; products, prices (replay 200), stock, unit lock, version conflict, delete refused, archive, changes; paging and search; other businesses 404; read-only source 409; viewer 403; checklist step |
| [integration/workflows/test_local_catalogue_quotation.py](../../tests/integration/quotations/workflows/test_local_catalogue_quotation.py) | Enquiry priced from our list (evidence standard/2), approval, a portal price change makes acceptance revise to version 2 at the new price; an 8-day-old count is stale with stock review; an archived product is never quoted or offered |
| portal `tests/features/catalogue/` | Form checks and payloads (multiples without float errors), price and stock text, midnight in the business time zone, list page (badges, editors only, read-only banner), product form (local and server problems) |

### Phase 4 customers and conversations (ADR 0021)
- **Unit:** `tests/unit/domain/test_conversations.py` (follow-up rule, 7-day boundary), `test_contacts.py` (consent words, masking), `test_requirements.py` (`quantity_quote`), `test_messages.py` (confirmed wording); `tests/unit/application/test_conversations.py`, `test_customer_messages.py`, `test_customer_actions.py`, `test_contacts.py`, `test_intake.py` (follow-ups); interpreter tests (markers, binding).
- **PostgreSQL:**
  - `test_conversations.py` (races: answers at once reopen once; one contact for simultaneous first messages);
  - `tests/integration/workflows/test_enquiry.py` (round 2, re-read when a message arrives mid-round);
  - `tests/integration/api/test_public_chat.py` (chat end to end, refusals, daily cap, secure link, staff inbox/merge/acknowledgement, hosted page, old-envelope delivery regression).
- **Portal:** `portal/tests/features/conversations`, `portal/tests/widget` (text-only rendering, start form, blocked storage); the architecture test keeps the widget separate.
- **Live run (2026-10-28, Gemini):** see the handoff.

### Phase 5 one-click tools and approval by message (ADR 0022)
- **Unit:**
  - `tests/unit/application/test_lifecycle.py` (Enable, cost tick, one-time code, re-enable, setup events, `RouteTaken` rollback with secret clean-up, poll, disable, test, isolation);
  - `test_platform_inbound.py` (secret first, unrouted, setup-only while setting up, keys per business, replay);
  - `test_media.py`, `test_resend.py`;
  - `tests/unit/api/test_hooks_security.py` (HTTP answers of the platform route);
  - `tests/unit/domain/test_manager_replies.py`: the 80-case [evaluation set](../../evals/manager_replies.jsonl). The guards alone must block every `guard_blocks` case against a reader that always says "approve", and let every clear approval through.
  - `tests/unit/adapters/providers/telegram/test_telegram.py`: documented updates in `tests/data/providers/telegram/updates.json`, a fake Bot API through `httpx.MockTransport`, send outcome mapping, linking, and the whole link flow through the core.
  - `tests/unit/adapters/providers/email_common/`, `tests/unit/test_ops.py`; the provider contract test covers platform-webhook providers.
- **PostgreSQL:**
  - `test_provider_state.py` (both roles apply steps, rollback, message routes, staff messages, webhook customer messages with limits);
  - `test_messages.py` (Resend as the API role);
  - `tests/integration/workflows/test_manager_replies.py` (written approval only after YES, reason prompt after Reject, unclear reply asks, rules mode does not read, the signed link with sign-in and CSRF).
- **Portal:** `portal/tests/features/connections` (one-click rules, cards for an unknown tool, cost tick, steps with the code, forms with password fields), `portal/tests/features/approvals`, the conversation thread's Send again.
- **Live:** none (no accounts, user answer). `scripts/evaluate_replies.py --model ...` evaluates a model on the reply set when the user wants a live run.

### Phases 6 and 7: ERPNext and orders (ADR 0023, 0024)
- **Unit:**
  - `tests/unit/adapters/providers/erpnext/test_erpnext.py`, over a fake Frappe REST API: form and checks, production address rules, feed mapping (units, groups, tax, warehouses), live prices and stock, error mapping, writer (reference, accepted price, duplicates, unknown versus refused).
  - `tests/unit/domain/test_catalogue_sync.py` and `test_orders.py`.
  - `tests/unit/application/test_catalogue_sync.py` and `test_orders.py`: creation rules and the ERP write with chaos cases (timeout after commit gives exactly one ERP order, a worker stopped while sending, the window, no resend, and an unknown outcome never reaching the job runner as an error).
  - `test_lifecycle.py` (replace and first copy); the provider contract sample for ERPNext.
- **Writer contract:** `tests/unit/ports/test_erp_writer_contract.py`, with the simulated writer and the ERPNext sandbox (integration marker).
- **PostgreSQL:**
  - `tests/integration/persistence/test_catalogue_sync.py` (copy, change-only rows, currency issue, interrupted runs, "Sync now", replace and restore);
  - `test_provider_state.py` (an order question is not an enquiry);
  - `tests/integration/workflows/test_orders.py` (automatic and staff creation, the customer's pull view, moves, a create race, compare-and-set steps, append-only history, grants, isolation).
- **Sandbox (integration marker, needs `SALES_TEST_ERPNEXT_*`):** `tests/unit/adapters/providers/erpnext/test_erpnext_sandbox.py` and the writer contract. See [provider setup](../operations/provider-setup.md#erpnext).

## Planned business suites
- Domain unit tests: Decimal totals, rounding, quantity/unit validation, expiry, pricing policies, missing values.
- PostgreSQL integration tests: real migrations, tenant policies, unique constraints, transactional inbox/outbox, job leases.
- Adapter contracts: every ERP/channel implementation passes shared fixtures; provider sandbox tests validate actual APIs.
- Workflow tests: clarification, rejection, revision, restart/resume, provider failures, model failures, expiry and accepted order.
- Security tests: cross-tenant reads/writes/downloads/checkpoints, forged webhooks, stale approvals, role bypass and attachment handling.
- Concurrency/recovery: simultaneous approvals/acceptances, repeated jobs, worker crash, graph replay, timeout after remote success.
- End-to-end: two tenants and two channels through approved PDF and sandbox ERP order.
- Release tests: backup restore, migration/rollback strategy, deployment smoke tests and documented load profile.

## Additional approval and extension coverage
- Run the same approval contract cases through portal, fake channel and each supported real channel adapter.
- Verify enrolled actor/tenant/context, forwarded actions, revoked roles, expired/stale buttons and conflicting portal/chat decisions.
- Verify full quote review detail is accessible in-channel and provider limitations produce explicit fallback rather than silent approval.
- Confirm notifications across channels reference one task; retries or failed confirmations never duplicate decisions/resumes.
- Add import-boundary checks when modules exist; a new approval provider must not require financial-domain or graph-routing edits.
- Deferred negotiation tests are defined in the [negotiation plan](../plan/negotiation.md); no feature pass is claimed before implementation.
- Direct email decisions must exclude quoted approvals, automatic replies and ambiguous/conditional text, and verify sender plus exact task/version.
- Deferred manager commands must distinguish read-only stock questions from scoped changes, reject ambiguous global price writes and require approval of every negotiated revision, even if the proposed discount is within policy limits.

## Lifecycle and ingestion coverage
- Unsupported, encrypted, oversized and scanned PDFs must not silently produce empty successful requirements. Preserve extraction evidence and route to clarification/review where unsupported.
- Draft previews and final PDFs must match the exact reviewed quotation values/version; previews cannot be dispatched as approved offers.
- Test human pause versus dispatch, authorized resume, cancellation versus order commitment, expired/withdrawn offers and acceptance across multiple revisions of one purchase.
- Order confirmation requires confirmed ERP success; failed confirmation retries must never create another order.

## AI evaluation dataset
The offline extraction evaluation (dataset `evals/enquiry_extraction.jsonl`, `scripts/evaluate_extraction.py`, `test_evaluation.py`) was removed by the user's decision on 2026-11-03 (ADR 0026 step 2). The enquiry reader is covered by its unit tests (`tests/unit/workflows/test_enquiry.py`, the interpreter tests) and by trying enquiries with a model key. `scripts/evaluate_replies.py` (manager replies, model only) stays.
Planned extensions follow below.

Synthetic cases include aliases, misspellings, ambiguous SKUs, multiple items, missing quantities, conflicting dates, units, currencies, malicious instructions and unsupported attachment formats.
Track field extraction correctness, SKU match correctness, appropriate clarification, invented factual values, latency and cost.
Release blocking invariants: no model-origin price/stock accepted as authoritative; no tool authorization from model text; no bypass of approval/tenant checks.
Set measured extraction/latency targets before pilot; do not invent passing scores in advance.
Keep prompt/model/version and dataset version with results. Use deterministic stubs for routine tests and opt-in bounded live model evaluations.
A test that mirrors implementation without asserting business behaviour is insufficient.

Test doubles shared by several files live in `tests/support/` (`catalogue_store.py`, `workflow_store.py`), never in `src/` (ADR 0026 step 2).

Migrations: one baseline (`0001_baseline`, ADR 0026 step 4). The per-migration round-trip tests (`test_migrations_0008/0010/0011/0012/0013/0015.py` and the 0009 round trip in `test_quotation.py`) were removed with the old migrations; the schema rules they also checked (default product source in `create_business`, the enquiry reopen guard, exact-key platform routes, one routing key per connection, append-only reply classifications) moved to [integration/persistence/test_schema_rules.py](../../tests/integration/platform/persistence/test_schema_rules.py). `check_database.py --cycle` still downgrades the baseline to empty and upgrades again.

Since ADR 0026 the tests mirror the business modules: `tests/unit/<module>/<layer>/...` and `tests/integration/<module>/<layer>/...`; provider packages under `tests/*/providers/`; wiring tests at the top (`tests/*/test_api_app.py`, `test_seed.py`). `tests/unit/test_architecture.py` checks the module order, the layers inside each module, provider names, HTTP clients and that only the wiring builds SQL stores.
