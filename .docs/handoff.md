# Current handoff
Updated: 2026-11-02 (phases 1-7 merged through PR #11; friendly portal design merged in PR #12; backend restructure accepted in ADR 0026, in progress).

## Latest: backend restructure planned (2026-11-02, ADR 0026)
- **User decisions (2026-11-02):** regroup the backend into business modules with one shared wiring and fuller architecture tests; rename the package to `sales_platform` (portal "Sales Portal"); remove the deprecated simulated route; **plan only, no code yet**.
- **Review findings:** see the ADR's table (code spread by layer; 20 objects wired twice in `api/app.py` and `composition.py`; `persistence`/`api` not covered by the import test; 1,865-line `models.py`; empty folders, a deprecated route and an in-memory double in `src/`; names that no longer fit).
- **User answers (2026-11-03):** go ahead, one PR per step merged by Claude after CI; `SALES_` settings at once; `sales_*` database names with one fresh baseline (local data recreated); repository renamed by the user; offline evaluation removed. See ADR 0026.
- **Step 0** merged (PR #13, `1ed59ca`, CI green).
- **Step 1 (guard rails):** `composition.build_core` builds the 20 shared stores and services once for the API and the worker; the API's sign-in route depends on a new `IdentityProvider` port instead of the dev adapter; the architecture test now covers `persistence` and `api` (named exceptions listed in the test) and fails if anything but the wiring builds a SQL store. Checks: Ruff, strict mypy (194 files), 952 unit tests, `check_database.py` 1113 passed / 14 skipped. A first `check_database.py` run failed and the second passed: a flaky test is possible; its output was not kept, so watch the next runs.
- **Step 1** merged (PR #14, `89bbc0a`, CI green).
- **Step 2 (leftovers):** removed the deprecated `/api/v1/channels/simulated/events` route (the demo page and `scripts/simulated_channel.py` use the generic hook route; the demo waits for the worker to link the phone), the empty `adapters/channels` and `adapters/delivery` folders, and the whole offline extraction evaluation (`evaluation.py`, `scripts/evaluate_extraction.py`, `evals/enquiry_extraction.jsonl`, `test_evaluation.py`). The in-memory catalogue and workflow stores moved to `tests/support/` (unit tests still use them). The portal-revoke check moved into the generic-hook integration test; the two-phone regression test now uses the hook route. `scripts/evaluate_replies.py` (model only) stays.
- **Step 2** merged (PR #15, `ed11600`, CI green; `check_database.py` 1106 passed / 14 skipped).
- **Step 3 (rename):** the package is now `src/sales_platform` (every import, the entry-point groups `sales_platform.providers` / `.secret_stores` / `.mailers`, the wheel path), the project is `sales-platform`, the API title "Sales Platform" and the portal "Sales Portal". Every setting is now `SALES_...` (27 names, plus `SALES_TEST_ERPNEXT_*` for the sandbox tests); `.env.example`, the user's local `.env` (keys renamed in place, values unchanged), VS Code launch/tasks/stop script and the docs follow. Database role and database names (`quotation_app`, `quotation_owner`, `quotation_worker`, `quotation_dev`, `quotation_test`), the Compose project and the session cookie name are unchanged until step 4. **After pulling: run `uv sync --all-extras` and restart the API and the worker** (old processes still import `quotation_app`).
- **Step 3** merged (PR #16, `1f36535`, CI green).
- **Step 4 (database):** user confirmed on 2026-11-03 (go ahead; delete the old volume). The 15 migrations are replaced by `migrations/versions/0001_baseline.py` + `.sql`, generated once from `pg_dump --schema-only` at `0015_orders` (roles renamed; psql lines, session settings, schema grants, owner changes and `alembic_version` removed; LangGraph's 10 `checkpoint_migrations` rows added). Its downgrade drops everything it created. Roles and databases are now `sales_owner` / `sales_app` / `sales_worker` / `sales_dev` / `sales_test`, the Compose project `sales-local` (volume `sales_pgdata`); `init.sql`, `compose.yaml`, `.env.example`, `init_local_env.py`, `check_database.py`, CI and the user's `.env` URLs follow. New `scripts/recreate_local_database.py`. **Proof:** a fresh database from the baseline dumps to the same 4,437 schema lines as the old head; the only differences are PostgreSQL printing 84 `IN (...)` check expressions differently, and all 92 checks have the same columns and allowed values. Per-migration round-trip tests were removed; the five schema-rule tests among them moved to `tests/integration/persistence/test_schema_rules.py`. The local database was recreated and seeded.
- **Steps 4-14 in one PR (user, 2026-11-03: "just create single pr for all"):** the baseline (step 4) plus the module restructure: nine modules in a fixed order (`platform`, `integrations`, `accounts`, `catalogue`, `conversations`, `enquiries`, `quotations`, `orders`, `customer_channels`) and `providers/`; every two-way link removed (see ADR 0026 implementation notes, including the deviations); `models.py` split per module; `ErpReader`/`ErpWriter` renamed `ProductSource`/`OrderWriter`; tests mirror the modules; docs (code map, module boundaries, testing, README). The old data volume `quotation-local_quotation_pgdata` was deleted as the user asked.
- **Checks:** ruff and strict mypy (393 files) clean; 950 unit tests pass; `check_database.py --cycle` results in the PR.
- **After pulling:** `uv sync --all-extras`, then restart the API and the worker (module paths changed; VS Code launch entries updated).
- **Open:** the user renames the GitHub repository (ADR 0026 Q3), then the links are updated.

## Latest: friendly portal design (2026-10-30, ADR 0025)
- **User decisions (2026-10-30):** see [ADR 0025](adr/0025-friendly-portal-design.md). New look, a sidebar/bottom-bar menu with icons, a Home page with to-do cards, a one-line hint on each main page, plain words on screen (codes unchanged), `lucide-react` 1.60.0, "friendly and clean" style. No backend change.
- **Built:** `features/home`, `app/layout/navigation.ts` + `SideNav.tsx`, `shared/ui/PageHeader.tsx`, icon status badges, new `global.css` (dark mode fixed for chat bubbles), orders tab in the URL, list hooks with an `enabled` option, plain role names.
- **Checks:** portal typecheck passes; 38 test files, 132 tests pass (124 before, plus 8 new); build passes (JS 505 kB / 150 kB gzip, over Vite's 500 kB warning).
- **Not checked:** no visual check in a real browser yet; detail pages not reorganised (open idea).
- **Settings and Integrations (same day, ADR 0025 addendum):** one settings frame (side list on wide screens, pills on phones) with Business, Pricing, People, Integrations, API keys; Integrations groups connected tools by kind, hides switched-off ones, lists one-click tools and website chat, and shows WhatsApp/Email/SMS as Coming soon (not built). "Apps & channels" left the menu; managers link their account on the profile page. Checks: typecheck passes, 41 files / 140 tests pass, build passes (JS 510 kB / 152 kB gzip).
- **Dropped (user, 2026-10-30):** a plan to remove ERPNext and build our own inventory was discussed and dropped before any code; nothing changed.
- **Merged:** PR #12 as `fee24c5` on 2026-10-30; CI `portal` and `verify` passed on both runs (41019156323, 41019164580).

## Current state
- **M0-M6 are COMPLETE** (local and CI), all on origin/main:

  | Milestone | Commit | CI run |
  |---|---|---|
  | M1 | 5d1ae69 | [39738532222](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39738532222) |
  | M2 | d7707eb | [39741408875](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39741408875) |
  | M3 | 52cccd7 | [39747434193](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39747434193) |
  | M4 | 21f0ba5 | [39900219005](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39900219005) (`verify` and `portal` jobs) |
  | M5 | 6f951ec | [39903715921](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39903715921) (both jobs) |
  | M6 | f545201 | [39907493342](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39907493342) (both jobs) |

  For M3 the user could not install Ollama on the corporate VM, so it ships with the free rules interpreter.
- **Last milestone: M6 (customer acceptance), complete.** Next milestone not chosen yet: ask the user first. Decisions taken with the user on 2026-10-23 ([ADR 0016](adr/0016-customer-acceptance.md)): acceptance only (no ERP order or reservation); answers through the API; 7-day default validity from delivery; accept/decline (no PO number); any price change refuses and drafts a revision for approval; stock ignored; expiry closes; staff withdrawal with a reason; a portal plus linked-channel notice; a rejected revision closes.
- **Working rule (user, 2026-10-23):** never push to `main`; every change goes on a new branch with a pull request to `main` (AGENTS.md).
- **Working rule (user, 2026-10-23):** ask before assuming anything. Follow [clarify-before-building](../.claude/skills/clarify-before-building/SKILL.md), which is also in AGENTS.md.
- **User decisions (2026-10-23):**
  - **Email approval is out of scope for now:** managers approve in the portal.
  - Email and other providers must stay **easy to add**; see [adding channels](development/adding-channels.md).
  - PDFs use the existing storage decision (local disk behind `ObjectStorage`, production storage at M8).
  - This is recorded in [ADR 0015](adr/0015-quotation-documents-and-delivery.md); email approval and delivery move to M7.
- **User decisions (2026-10-22):**
  - Free models now, with an easy provider swap later (ADR 0012).
  - A provider-independent login: dev sign-in now, OIDC later.
  - A **separate React portal** (ADR 0013).
  - No paid or live calls.
  - A GitHub Pages demo is an idea only (open decisions).
- The user's `.vscode/settings.json` stays untracked; `.vscode/launch.json`, `tasks.json` and `stop-dev.ps1` (one-click "Run all" with stop-and-restart) are committed. `portal/node_modules` and `portal/dist` are ignored.
- **Not built yet:** real ERP or messaging providers (email, Telegram), orders, OIDC and deployment.

## Implemented
- **M0-M3:** see [setup](development/setup.md), [quotation pricing](features/quotation-pricing.md), [enquiry intake](features/enquiry-intake.md) and [enquiry extraction](features/enquiry-extraction.md).
- **M4:** see [quotation approval](features/quotation-approval.md), [ADR 0013](adr/0013-portal-identity-and-approval-core.md) and the [code map](development/code-map.md).
  - **Drafting:** ready enquiries queue `draft_quotation`. The M1 calculator prices them into an immutable version with a SHA-256 content hash. Unpriceable lines give `pricing_failed`, never a guessed price.
  - **Workflow:** a second LangGraph graph (`quotation-approval-v1`) runs draft -> approval task -> interrupt -> decision -> approved / rejected / revision loop. It uses PostgreSQL checkpoints, a per-run advisory lock, and stale-resume protection.
  - **`ApprovalService.decide`:** one service for the portal and channels. It checks role, pending status, content hash/current version and self-approval under a row lock. One decision per task; replays and conflicts are handled; a reason is required to reject or request a revision.
  - **Sessions:** HttpOnly SameSite=Strict cookie with only the hash stored, plus a CSRF header. The dev login is refused in production.
  - **Simulated manager channel:**
    - enrollment by one-time code;
    - revocable bindings;
    - single-use 24-hour action references per account and task;
    - an HMAC-signed webhook;
    - outbox files;
    - `scripts/simulated_channel.py`.
  - **Portal API** (`/api/v1/tenants/{t}/...`): quotations, decisions, revisions, enquiry exception list, channel linking.
  - **React portal** (`portal/`, Vite + TypeScript, mobile-first): login, lists, version review with stock and price evidence, decision panel, revision form, history, channels and enquiries. The API can serve the built files at `/portal/`.
  - **Portal structure (ADR 0014, user request 2026-10-23):**
    - It is organised by feature (`auth`, `tenant`, `quotations`, `enquiries`, `channels`) and layer (`model`, `api` repository, `services`, `store`, `hooks`, `ui/pages`, `ui/components`, `routes`, `index`), plus `shared/` and `app/`.
    - State: TanStack Query for server data, Zustand for the selected business, URL params for filters, form hooks for local state. React Router hash routes.
    - An architecture test enforces the rules, and tests mirror `src/` in `portal/tests/`. See [portal architecture](development/portal-architecture.md).
  - **Migration 0005:**
    - sessions, quotations, versions, tasks, decisions, enrollments, bindings and action references;
    - append-only triggers;
    - column-level grants;
    - narrow `app.subject` / session RLS policies.
  - **CI:** a new `portal` job (npm ci, typecheck, test, build).

- **M5:** see [quotation delivery](features/quotation-delivery.md) and [ADR 0015](adr/0015-quotation-documents-and-delivery.md).
  - **Approved step:** only the approved current version gets a `quotation_deliveries` outbox row plus a `deliver_quotation` job, in one transaction. The recipient is always the enquiry's sender (for the API, the submitting key).
  - **PDF:**
    - ReportLab 5.0.1 renders it; it is deterministic, escapes customer text, refuses unsupported characters and leaves out internal evidence.
    - It is stored once under `tenants/<t>/documents/<id>` (the ID derives from the PDF hash).
    - It is recorded in the append-only `quotation_documents` table, and its hash is re-checked on every read.
  - **Delivery statuses:** `pending`, `sending`, `available`, `sent`, `failed` (with reason), `unknown` (never resent automatically). `pending` -> `sending` is a compare-and-set.
  - **API pull channel:** `GET /api/v1/enquiries/{id}/quotation(.pdf)` with the client's API key. Only delivered approved versions are visible, and the first download is recorded.
  - **Portal:**
    - preview PDF (marked "not an offer");
    - approved PDF download;
    - delivery status with errors, polled while in progress (`DocumentPanel`, `DeliveryList`, `deliveryService`, `useQuotationDocuments`).
  - **Migration 0006:** documents and deliveries, forced RLS, column grants (worker: progress columns; API: `first_downloaded_at`). It downgrades cleanly; PDF files stay on disk.
  - **Extension points** for email and Telegram: the `DeliveryChannel` and `ApprovalNotifier` adapters plus an inbound route. No business code changes ([adding channels](development/adding-channels.md)).
  - **Dependencies:**
    - `reportlab>=5.0.1,<5.1`;
    - a `pillow>=12.3,<13` constraint (13.0.0 was only 7 days old);
    - dev `types-reportlab` 5.0.0.20261008.
    - The architecture test allows `reportlab` in adapters only.

- **M6:** see [customer acceptance](features/customer-acceptance.md).
  - **Customer API:** `POST /api/v1/enquiries/{id}/quotation/response` (accept/decline the exact version and hash, `Idempotency-Key`). `GET .../quotation` gains `status`, `valid_until`, `content_sha256` and `response_path`.
  - **Acceptance re-pricing:**
    - Accepting re-prices from the ERP. Any difference refuses it: `refused_changed` plus version N+1 created by the system, then manager approval and delivery.
    - Lines that can no longer be priced give `pricing_failed`.
    - ERP down gives 503, and nothing is recorded.
  - **Validity:** valid until is printed on the PDF, and an `expire_quotation` job runs then. An answer after valid until records `expired` immediately.
  - **Staff:** withdrawal (sales operator/approver roles, reason, CSRF) and a portal Customer box (status, validity, answers, withdraw form). List filters: Approved, Accepted, and Declined/expired/withdrawn.
  - **Manager notice:** accept/decline notices go to linked approver channels (`ApprovalNotifier.send_notice`; simulated `notice-<id>.json`).
  - **Workflow:** the graph pauses at `await_customer` after `approved`, and `apply_customer` ends the run or continues to re-approval. The name stays `quotation-approval-v1`.
  - **Migration 0007:**
    - the validity setting, plus `valid_until` (backfilled);
    - `quotation_responses` (append-only, one closing answer, unique idempotency keys);
    - the new statuses and grants.
  - **Test harness:** a controllable clock and delayed jobs.

## Decisions recorded
- **ADR 0013:**
  - Sessions follow the backend-for-frontend pattern, and identity providers stay swappable.
  - Versions are immutable and hash-bound.
  - There is one approval service.
  - Graph pauses use interrupts plus resume jobs.
  - Channel enrollment and references work as described in ADR 0013.
  - ADR 0007 is now partly implemented.
- **ADR 0015 (M5):**
  - Only approved versions get documents, and the documents are hash-checked.
  - The delivery outbox reports outcomes honestly: there are no automatic resends after uncertainty.
  - Delivery goes to the enquiry's sender.
  - Email approval is deferred to M7 by the user.
- **Earlier decisions stand:** ADRs 0010-0012, negotiation deferred (0008), every negotiated revision needs manager approval, and direct email approval stays a requirement (now M7).

## Verification (M6, 2026-10-23)
- **Static checks:** Ruff and strict mypy (155 files) pass.
- **Offline pytest:** 327 unit passed, 44 integration skipped.
- **`check_database.py --cycle`:** at `0007_acceptance` with no drift. 371 passed; then downgrade to empty, upgrade, and 371 passed again.
- **Round trip 0007 -> 0006 -> 0007 with an accepted quotation:**
  - Going down mapped the quotation and run status back to `approved`, removed the 2 M6 jobs and the answers table, and kept the other jobs.
  - Upgrading again was clean.
- **Regression found and fixed:** I had made the intake API map every IntegrityError to 409, which broke intake's "not stored, retry" 503 (caught by a PostgreSQL test). The 409 mapping is now limited to the answer route.
- **Portal:** typecheck passes; 41 tests pass; the build passes (379 kB JS, 118 kB gzip).
- **Live run:** see [setup](development/setup.md#customer-acceptance-m6) (accept, replay, notice, decline, withdraw, filters; no secrets in the logs).
- **Not run:**
  - The portal was not clicked through in a real browser.
  - A live price change (the demo ERP has fixed prices) is covered by unit and PostgreSQL tests.

## Verification (M5, 2026-10-23)
- **Static checks:** Ruff lint/format and strict mypy (148 files) pass.
- **Offline pytest:** 296 unit passed, 39 integration skipped.
- **`check_database.py --cycle`:**
  - Both databases are at `0006_documents`, and `alembic check` finds no drift.
  - 335 passed; then downgrade to empty, upgrade, and 335 passed again.
- **Round trip with M5 data present:**
  - Going down to 0005 removed both tables and the one `deliver_quotation` job, and kept the other 5 jobs and the approved quotation.
  - Upgrading again was clean.
- **Real bug found by the PostgreSQL run and fixed:** the first recipient rule used `sender_external_id`, which for API enquiries is the customer's reference label. `delivery_recipient` now picks the submitting connection for the API, and has a unit test.
- **Portal:** typecheck passes; 36 tests pass (architecture rules included); the build is 375 kB JS (117 kB gzip).
- **Live run** (uvicorn serving `/portal/`, `python -m sales_platform.worker --once` processes, httpx, `quotation_dev`):
  - **Before approval:** the customer got 404, the preview was marked, and the approved PDF gave 404.
  - **After approval and one worker pass:**
    - The delivery was `api available`.
    - The customer JSON showed v1 at 10450.38 INR.
    - The PDF's SHA-256 matched, it contained the total and reference and no preview mark, and it was identical to the staff download.
  - **Download tracking:** the first download was recorded once.
  - **Refusals:** the other tenant's key got 404, and no key got 401.
  - **Repeat runs:** an extra worker pass created nothing new.
  - **Logs:** no API keys appeared in the logs, and both temporary keys were revoked.
- **Not run:**
  - The portal was not clicked through in a real browser (it was unit-tested and built, and `/portal/` returned 200).
  - No real push channel exists, so push outcomes are tested with fakes only.

## Verification (M4, 2026-10-22/23)
- **Static checks:** Ruff lint/format and strict mypy (134 files) pass.
- **Offline pytest:** 260 unit passed, 36 integration skipped.
- **Portal:**
  - `npm run typecheck` passes.
  - `npm test` passes all 27 tests, including the architecture rules, which were shown to reject a UI-to-repository import and a deep cross-feature import.
  - `npm run build` produces 372 kB JS (116 kB gzip).
  - Dependencies are pinned versions at least 14 days old: React 19.3.0, Vite 8.3.3, Vitest 5.0.3, TypeScript 7.0.2, TanStack Query 5.104.1, Zustand 5.0.15, React Router 8.4.0, @types/node 24.19.1.
- **`check_database.py --cycle`:** both databases are at 0005_approvals with no drift. 296 passed; then downgrade to empty, upgrade, and 296 passed again.
- **Targeted round trip with M4 data present:** 0005 -> 0004 removed exactly the M4 tables plus the approval run and its checkpoints, and kept the extraction run. Upgrading again returned to head, and `alembic check` was clean.
- **One M2 test changed on purpose:** the worker role may now read memberships (to notify current approvers), still only with tenant context. The test now asserts that boundary.
- **Live run** (uvicorn serving `/portal/`, `python -m sales_platform.worker --once`, curl, `scripts/simulated_channel.py`):
  - Fan and bolts enquiry -> v1 at 10450.38 INR. The review message was delivered to the enrolled simulated phone.
  - A forwarded copy used by another account -> 403. Approval from the phone -> recorded. A second tap with a different action -> 409. Resume -> approved.
  - Pump enquiry -> `pricing_failed` (price_missing). A revision with the pump again -> 422 with issues. A revision with the 600 mm fan -> v1 at 8130.20 INR, then a new task and notification.
  - The manager's portal approval replayed with the same command -> `replayed=true`.
  - An office user on the industrial quotation -> 404. A request without CSRF -> 403. After logout the cookie gets 401.
  - The server log contained no API token, session token, CSRF token or channel secret. The API key was revoked afterwards.
- **Not run:**
  - The portal was not opened in a real browser; it was built and unit-tested, and its files were served (200).
  - Live Ollama (not installable).

## Known gaps (tracked)
- Dev login only; an OIDC adapter is needed before any non-development deployment.
- No approval deadlines, escalation, delegation or multi-level policy, and no quote validity date (open decisions).
- **M6 gaps:** no ERP orders or reservation; answers through the API only; no portal screen for the validity setting; pre-M6 finished runs do not resume for answers (dev data).
- **M5 gaps:**
  - PDFs use built-in fonts (Western European text only; other characters fail visibly).
  - There is no resend action for `failed`/`unknown` deliveries.
  - The PDF has no letterhead, terms or validity date.
  - Only API pull delivery exists.
- Only the simulated manager channel exists; email (deferred by the user) and Telegram are M7.
- Enquiry exceptions are listed in the portal but cannot be edited or answered there yet. Clarification questions are still not sent.
- Carried over: no OCR, no malware scanner, local storage only, no dead-job view.

## User decisions (2026-10-23, after M6): AI reader and "not found" replies
Implemented on `feat/gemini-and-categories` ([ADR 0017](adr/0017-gemini-reader-and-category-replies.md), [plan](plan/gemini-and-categories.md)). Verified: 339 unit tests, the PostgreSQL suite around the migration cycle, 14/14 evaluation cases (rules), and a live API/worker run; a Gemini worker with an invalid key retried. **Pending:** a live Gemini evaluation once the user has put a key in `.env`.
- **Why:** with the rules reader, "price for tata cars" got the same fixed "which products and quantities?" reply repeatedly.
- **Provider:** Google Gemini free tier, small (Flash-Lite class) model, synthetic/demo data only. Configured generically (user, 2026-10-23: "it should be generic"): `SALES_MODEL=google_genai:gemini-3.5-flash-lite` and `SALES_MODEL_API_KEY` in `.env` (the user creates the key; never in chat or Git). No provider-specific code; the Ollama code was removed.
- **AI down or rate-limited:** wait and retry (no fallback to rules).
- **Not found:** say so and list the business's product **categories** (from the ERP); asking about a category lists its products.
- **Replies:** stay fixed templates; AI only reads enquiries.

## Phase 1 foundations (complete, merged to main through PR #6, merge commit aee8988, 2026-10-27)
- **User (2026-10-27):** "start implementation as per plan". PR #5 (the plan) is merged. All phase 1 open questions were answered with the recommendations; see [phase 1 plan §3](plan/platform/implementation/phase-1-foundations.md#user-answers-2026-10-27). Claude may commit and push on this branch, one commit per work item, with a draft PR to `main` (never merging).
- **WI-0 done:** golden tests pin today's behaviour before the refactor:
  - approval request wording;
  - accepted and declined notice wording;
  - simulated outbox file names and JSON keys;
  - the old simulated route's response bodies and 409;
  - `"channel": "api"` in the enquiry status and the portal list (PostgreSQL).
  
  Checks: ruff, mypy, 392 tests on PostgreSQL.
- **WI-1 + WI-3 done:**
  - Domain types and ports: connections, the outbox and templates (wording moved word for word), inbound event types, providers, secrets, `integrations:manage` and new job kinds.
  - Secret stores `local` (AES-256-GCM) and `none`, chosen by entry point; new settings; `init_local_env.py --add-missing` adds `SALES_SECRET_STORE_KEY`. **Run it once, and `uv sync`, after pulling.**
  - The old `Channel`/`ApprovalChannel` enums go when the `api` and `simulated` providers exist (WI-5, WI-9).
- **WI-2 done:** migration `0008_connections` (tables, policies, worker grants, backfill, downgrade) and persistence. API keys create and disable their `api` connection; the seed gives each demo business a `simulated` connection (approvals and catalogue); intake and enrollment link rows to connections; disabled connections get no approval requests. Tests: `test_connections.py` and `test_migrations_0008.py` (upgrade with M6 data, downgrade, upgrade). Checks: `check_database.py --cycle` with 420 tests, twice. One earlier run failed once in `test_versions_and_decisions_are_append_only` and did not repeat in three later runs; watch for flakiness.
- **After pulling:** `uv run alembic upgrade head` and `uv run python -m sales_platform.seed` (the demo connections).
- **WI-4 + WI-5 done:**
  - **Registry and route:** the provider registry (entry points, `uv sync` registers `api` and `simulated`) and the generic hook route `POST /api/v1/hooks/{connection}/{token}`.
  - **Inbound handling:** `InboundService` (token, size, signature before parsing, stored once) and the `process_inbound` job (manager actions go through the same ChannelActionService and ApprovalService).
  - **Providers:** `api` and `simulated`. The old notifier now reuses the provider's outbox writer; it is deleted in WI-8.
  - **Old route:** now a documented deprecated alias on the new framework, with the same answers.
  - **Seed:** with the dev secret set, the seed gives the demo phone connection its signing secret (in the secret store) and a webhook token (hash only).
  - **Scripts:** `scripts/simulated_channel.py --via hooks`.
  - **Deviation from the plan, on purpose:** `ChannelEvent` keeps the provider key and the connection's legacy key (exactly one connection per business). Decision command IDs therefore stay the same across the upgrade, so the planned "replay after upgrade gets 409" edge case does not happen.
  - **Checks:** 439 tests on PostgreSQL, including approval through the generic hook. Live: demo page, enquiry, phone, approve (alias), quotation for the customer.
- **WI-6, WI-7, WI-8 and WI-11 done:**
  - **Migration `0009_outbound_messages`:** one outbox with forced RLS. Worker grants are column-level, so the worker can advance a message but never rewrite its text or recipient. Backfill gives one message per existing delivery; downgrade re-queues pending work.
  - **`SqlMessageOutbox`:** each delivery mirrors its message's status, and expiry is queued as before.
  - **`OutboundService` + `send_message` job:** honest statuses. `unknown` is never resent; a crash while sending becomes `unknown`.
  - **Send hooks:** `ApprovalRequestHook` creates the single-use token only at send time, so it is never stored. `QuotationDocumentHook` re-checks the PDF hash and attaches it.
  - **Rebuilt on the outbox:** `deliver_quotation`, `notify_approvers` and `notify_response`. Response notices are no longer repeated on retry.
  - **Removed (replaced):** `DeliveryChannel` and its errors, `ApprovalNotifier`, `adapters/delivery/api.py` and `adapters/channels/`.
  - **Composition root:** `composition.build_job_handlers` is shared by the worker and the PostgreSQL tests.
  - **Bug found in the live run and fixed:**
    - **Cause:** with several linked phones, the simulated provider reported the file name as the message ID, and that ID is unique per connection. The second "sent" update failed, so the token written to the phone was never marked sent ("Action not accepted").
    - **Fix:** the ID is now the message's own ID.
    - **Tests:** a unit test and a PostgreSQL regression test.
  - **Wording change staff can see:** a refused send now reads "Rejected by the provider: …" (was "…by the channel…"). A tool with no connection reads "No active connection for '<provider>' to send the quotation".
  - **Checks:** 391 unit tests; `check_database.py --cycle` with 448 tests (450 after the regression test). Live demo: enquiry, phone, approve, PDF, accept, notice.
  - **Observed, not phase 1:** the Gemini reader once read "2 x IND-FAN-450" as unit "x" and asked a clarification question. The free rules reader does not do this.
- **WI-9 done:**
  - **Intake:** API enquiries belong to the caller's `api` connection (business, provider, API key subject). A missing or disabled connection is refused (403); the provider must be installed.
  - **Envelope schema 2:** adds `connection_ref` and `reply_to`. The reply address is fixed at intake by the provider's delivery mode, and `queue_delivery` uses it.
  - **Removed:** the `Channel` enum. Stored and returned `channel` values are the provider key (`"api"`), unchanged in the API.
  - **Checks:** 394 unit tests; 452 tests on PostgreSQL.
- **WI-10 done:**
  - **Product source per business:** each business's catalogue connection names its product source. `CatalogueSourceResolver` and `TenantCatalogue` (an `ProductSource`) replace the hardcoded `SimulatedErp` in the API and the worker. Readers are cached for 30 s and keyed by connection and `updated_at`.
  - **No source (P1-Q3 = b):** the enquiry goes to `needs_review` with "No product source is connected", at once.
  - **ERP errors that can't succeed on retry (P1-Q4 = a):** they stop the job at once; temporary ones are still retried.
  - **Drafting edge case:** if a source disappears between extraction and drafting, the draft job stops with that error. This is a small difference from the plan's `pricing_failed` issue, recorded for review.
  - **Tests:**
    - The ERP contract suite also runs over `TenantCatalogue`.
    - New tests for the resolver and the job runner.
    - A PostgreSQL test with a new business without a source: needs review on attempt 1.
  - **Checks:** 406 unit tests; 465 tests on PostgreSQL.
- **WI-12 done:**
  - **`GET /api/v1/tenants/{t}/connections`:** administrators see all connections, including the last error; approvers see only active approval connections; others get 403. No config, secrets or token hashes are ever returned.
  - **`POST /connections/{id}/enrollments`:** the code works only on that connection. 404 for an unknown connection or another business's; 422 for a connection that is not for approvals or not active.
  - **The old `/channel-enrollments`:** now checks the provider registry and the business's active connections (unknown provider or no connection gives 422). The `ApprovalChannel` enum is removed.
  - **Bindings:** each now shows `connection_ref` and `connection_name`.
  - **Checks:** 409 unit tests; 468 tests on PostgreSQL.
- **WI-13 done:**
  - **Feature:** the portal feature `connections` replaces `channels`. The page is at `#/connections`, and `#/channels` redirects there. The top bar says "Connections".
  - **Page contents:** a list of tools (status, purposes; last error for administrators only) and "Link my account on <tool>" for each active approval connection. The code is per connection; linked accounts show the tool's name.
  - **Removed:** the hardcoded `"simulated"` in the portal. Decisions use purposes and status only.
  - **Checks:** portal typecheck, 46 tests, build.
- **WI-14 done:**
  - **`tests/unit/providers/test_provider_contract.py`:** runs every installed provider and a fake third provider (`echo`) through the same checks:
    - forged or missing signatures are refused;
    - parsing is stable and never depends on the business;
    - send outcomes match the delivery mode;
    - unsupported operations raise errors;
    - a catalogue reader exists exactly for catalogue providers;
    - `echo` enrols a manager through the generic services with no core change.
  - **Samples:** a test fails until a newly installed provider has its contract sample.
  - **Architecture test:** core code may not name a provider or import a provider package. Four listed exceptions: the API's own routes, the deprecated alias, API keys and the demo seed.
  - **Checks:** 424 unit tests.
- **WI-15 done:**
  - **New documents:** [ADR 0018](adr/0018-connector-framework.md), the [adding providers](development/adding-providers.md) guide (the old channels guide is now a pointer) and [connections](features/connections.md).
  - **Updated:** "extended by 0018" notes in ADRs 0007, 0011, 0013, 0015 and 0016; integrations, execution flow, code map, testing, portal architecture, configuration, setup ("after pulling phase 1") and the feature documents. All relative links checked.
- **Deviations from the plan (for review):**
  1. `ChannelEvent` keeps the provider key and the legacy connection key, so decision IDs are stable across the upgrade.
  2. If a product source disappears before drafting, the draft job stops with the error; no `pricing_failed` issue is recorded.
  3. Wording staff can see: "Rejected by the provider", and "No active connection for '<provider>' to send the quotation".
  4. The portal's link card no longer shows the `simulated_channel.py` command, because provider-specific text is not allowed in the portal.
- **Final verification (2026-10-27):**
  - **Automated:** ruff, mypy, and `check_database.py --cycle` with 483 passed and 1 skipped, twice. The evaluation scored 14/14 (rules). The portal passes typecheck, 46 tests and the build.
  - **Live, demo page:** enquiry, phone, approve, PDF, accept, notice.
  - **Live, portal API:** the manager sees only the linkable connection; sales staff get 403.
  - **Live, generic hook:** `--via hooks` enrol gives 200, and the worker enrols the account. A bad token gives 404 and a bad signature 401.
  - **Cosmetic, dev databases only:** a connection created by the 0008 backfill keeps its generated name (e.g. "simulated simulated-local"), because the seed does not rename existing connections.
- **Merged:** the user allowed the merge on 2026-10-27; PR #6 was green (`verify` and `portal`).

## Phase 2 accounts and onboarding (complete, merged to main through PR #7, merge commit e76b7e7, 2026-10-28)
- **User (2026-10-27):** "work on phase two". All of Q1-Q27 and P1-Q9 were answered; see [phase 2 plan, user answers](plan/platform/implementation/phase-2-accounts.md#user-answers-2026-10-27). Same commit arrangement as phase 1 (one commit per work item, pushed, draft PR; the user merges).
- **Answers that change the plan:**
  - no account lockout; the rate limits are the only guard against guessing;
  - rate-limit counters live in PostgreSQL (`rate_limit_counters`);
  - demo people also get a password, from `SALES_DEMO_PASSWORD` in `.env`;
  - an API key sees only the enquiries and quotations it submitted itself (was: any key of the business).
- **W0 done:** answers recorded in the plan, [07 decisions](plan/platform/07-decisions-needed.md) and here.
- **W1-W11 done** (ADR [0019](adr/0019-accounts-and-onboarding.md), feature doc [accounts and onboarding](features/accounts-and-onboarding.md)):
  - **Domain/ports/migration `0010_accounts`:** users, credentials, email links, account notices, invitations, admin events (append-only), tenant profiles, rate-limit counters, business status with a Go-live-only trigger, pricing "set later", portal key grants, and the four SECURITY DEFINER functions. Full downgrade.
  - **Services and API:** sign-up, verification, password login, forgot/reset/change, profile, create business, settings, PDF details and logo, pricing, setup checklist and Go live, members, invitations, portal API keys; `send_account_email` with the `console` mailer (files in `var/storage/dev-mail/`).
  - **Gates:** suspended keys 403 and jobs wait; nothing pushed to customers before Go live (API pull and manager messages still work).
  - **P1-Q9:** an API key reads only its own enquiries and quotations.
  - **Portal:** features `account` and `business`; no business -> `/start`; setting-up businesses open `/setup`; browser storage only in the tenant store (architecture test).
  - **Removed:** `scripts/create_api_key.py` (keys come from the portal); `QUOTATION_AUTH_PROVIDER` (now `SALES_DEV_LOGIN`).
  - **PDF renderer v2:** seller details and logo from the business profile.
- **After pulling:** `uv sync --all-extras`, `uv run python scripts/init_local_env.py --add-missing` (adds `SALES_DEMO_PASSWORD`), `uv run alembic upgrade head`, `uv run python -m sales_platform.seed`. See [setup](development/setup.md#accounts-and-onboarding-phase-2).
- **Bugs found and fixed during verification:**
  1. The "account exists" notice allowance was used up by every sign-up (found by the end-to-end test); it is now asked only when the address exists.
  2. Approved PDFs did not show the PDF details (found by the live run): the document source now loads the profile and the logo is read and hash-checked before rendering.
  3. The pricing API printed a zero tax rate as `0E-6`; now fixed notation.
- **Deviations from the plan (for review):**
  1. Two extra tables: `account_notices` (emails without a link) and `rate_limit_counters` (Q5). Recorded in the [contracts change log](plan/platform/implementation/00-shared-contracts.md).
  2. No "all six pricing values or none" check: currency or tax can be saved alone at creation (Q14); "complete" = all six set.
  3. `create_business` does not create a catalogue connection (no in-portal catalogue provider before phase 3).
  4. Changing your password keeps the current session (others are signed out); the plan said "new cookie".
  5. The "account exists" notice uses the resend allowance (1 per minute, 5 per day per address); the plan did not name a limit.
  6. A missing or changed logo file is logged and the PDF is made without it (the logo is decoration).
  7. The common-password list has only 10 entries of 12+ characters, so with a 12-character minimum it adds little. A bigger list would help (open point for the user).
  8. Account email wording was written by Claude (plain, fixed templates in `domain/messages.py`); the user may want to review it.
- **Verification (2026-10-27/28):**
  - **Static:** ruff, mypy (217 files).
  - **Unit:** 509 passed.
  - **PostgreSQL:** `check_database.py --cycle` 584 passed, then downgrade to empty, upgrade, 585 passed; after the PDF fix the full suite again 586 passed. Includes definer-function abuse tests, isolation of two self-created businesses, concurrent removal of two administrators, single-use links, the end-to-end API flow with the real worker, and the 0010 migration round trip with data.
  - **Evaluation:** 14/14 (rules).
  - **Portal:** typecheck, 67 tests, build (422 kB JS, 128 kB gzip).
  - **CI:** green on PR #7 for the W10-W11 commit.
  - **Live run** (API on :8010 serving the built portal, real worker, dev database): 32 steps from sign-up to Go live, invitation, role change, removal, demo password login, isolation and refusals, all OK; then an approved demo quotation whose PDF shows the legal name, address and GSTIN (profile restored afterwards). API log: no keys, tokens, passwords or email addresses.
  - **Not run:** the portal was not clicked through in a real browser (no browser tool here); the logo is covered by unit tests only.
  - **Watch:** one setup error in `test_expiry_closes_an_unanswered_quotation` appeared once in a full run and did not repeat in four later runs (phase 1 saw a similar one-off).
- **Merged:** the user asked for the merge on 2026-10-28 ("merge phase 2 pr"); PR #7 was green (`verify` and `portal`).

## Phase 3 product catalogue (complete, merged to main through PR #8, merge commit 9d31e22, 2026-10-28)
- **User (2026-10-28):** "continue with phase 3". All Q1-Q23 and the default-source question were answered with the proposals and the summary confirmed; see [phase 3 plan, user answers](plan/platform/implementation/phase-3-catalogue.md#user-answers-2026-10-28) and [07 decisions](plan/platform/07-decisions-needed.md). Same commit arrangement as phases 1 and 2 (one commit per work item, pushed, draft PR; the user merges).
- **Asked mid-work (W15):** `pg_trgm` needs CREATE on the database; the user chose to grant it to `quotation_owner` on `quotation_dev` in `infra/postgres/init.sql` (`quotation_test` is owned by it already). **Existing dev databases need the one-off command in [setup](development/setup.md#product-catalogue-phase-3).**
- **W0-W13 done** (ADR [0020](adr/0020-local-catalogue.md), feature doc [product catalogue](features/product-catalogue.md)):
  - **W1:** the search word rule moved from the simulated ERP to `domain/catalogue_search.py` (no behaviour change).
  - **W2-W3:** editing rules (`domain/catalogue_admin.py`) and `catalogue:view` / `catalogue:manage`.
  - **W4-W6:** ports, migration `0011_catalogue` (five tables, `pg_trgm`, RLS, triggers, grants, `create_business` with the default source, backfill, downgrade), `SqlCatalogueStore`, the **Demo Local Catalogue** business (`33333333-…`, people `admin@`, `sales@`, `manager@local.example`).
  - **W7-W8:** `LocalCatalogue` and provider `local_catalogue` ("Our product list"); `InMemoryCatalogueStore`; ERP and provider contract suites extended; a shared search-case table.
  - **W9-W11:** `ProductService`, `/api/v1/tenants/{t}/catalogue/...`, `SALES_DEFAULT_CATALOGUE_PROVIDER`, the checklist's Products step (done with one priced product; not required).
  - **W12:** portal feature `catalogue` (list, product page with prices/stock/archive/changes, categories); top bar "Products"; the pricing form suggests a 7-day stock age (Q10).
  - **W13:** evaluation on both sources (`evaluate_extraction.py --source local`), PostgreSQL end-to-end tests.
- **After pulling:** the init command, `uv sync --all-extras`, `uv run alembic upgrade head`, `uv run python -m sales_platform.seed` ([setup](development/setup.md#product-catalogue-phase-3)).
- **Deviations from the plan (for review):**
  1. **C7 was not quite true:** migration 0008 gives the demo businesses' simulated connection only manager approvals; the seed adds the catalogue purpose. After a downgrade/upgrade without seeding, 0011 gives them our list; the seed now disables such a second source on the two ERP demos (found by the full PostgreSQL run).
  2. The provider gets its store through `ProviderEnvironment.catalogue_store` (each process's own role), not a new registry hook.
  3. `InMemoryCatalogueStore` lives in `src/` (`adapters/catalogue/memory.py`) because tests run with `--import-mode=importlib` and the evaluation script uses it offline; the plan put a fake in `tests/conftest.py`.
  4. `evaluate_extraction.py --source local` uses that in-memory store, not the dev database's third business (PostgreSQL is covered by the integration tests).
  5. The catalogue summary also returns the business time zone (a scheduled price starts at midnight there, Q8).
  6. Prices and stock cannot be added to an archived product (409 `product_archived`); restore it first. The plan did not say.
  7. Archiving a category keeps its products' link but stops using its name for search and "we supply" replies.
- **Open points found (for the user, not changed):**
  1. **A near-miss SKU can be matched to a sibling product.** Live run: an enquiry for the archived `LIVE-VALVE-…-B` was matched automatically to the active `LIVE-VALVE-…` (three shared words: live, valve, the code). An unknown `IND-…` code gets "which product did you mean" with every `IND-` product. Both follow the existing rule (ADR 0017: "best word matches; exactly one candidate is matched"), the same as the simulated ERP; manager approval still applies. Options for a later decision: treat a query that looks like a SKU as "exact SKU or nothing", or require more than shared prefix words.
  2. Deviation 7 of phase 2 (a bigger common-password list) and the account email wording are still open.
- **Verification (2026-10-28):**
  - **Static:** ruff, ruff format, strict mypy (237 files).
  - **Unit:** 636 passed, 3 skipped.
  - **PostgreSQL:** `check_database.py --cycle`: 757 passed, then downgrade to empty, upgrade, 757 passed again (`alembic check` clean on both databases). Includes RLS on every catalogue table, history-only rows, concurrent revisions, the 0011 migration round trip, the API with real sessions and the end-to-end quotation flow.
  - **Evaluation:** 14/14 (rules) on the simulated ERP and on our list, 0 invented facts.
  - **Portal:** typecheck, 97 tests, build (447 kB JS, 134 kB gzip).
  - **Live run** (API on :8010 serving the built portal, real worker, dev database): 24/25 steps OK: source and counts, category/product/price/stock, a portal API key, enquiry quoted with evidence "standard r1, local-catalogue", approval, customer sees it, price raised, acceptance refused with version 2 at the new price, an 8-day-old count stale in the portal and in the quotation, archive, other business 404, demo read-only 409, a newly signed-up business starts with Our product list and an open Products step. The failed step is open point 1 (sibling match). API log: no keys, tokens, passwords or email addresses; no 5xx.
  - **Not run:** the portal was not clicked through in a real browser (no browser tool here); CI runs on push.
- **Merged:** the user merged PR #8 on 2026-10-28 ("already merge phase 3 prs"). Open points 1-2 above stay open.

## Phase 4 customers, conversations and website chat (complete, merged to main through PR #9, merge commit a155244, 2026-10-29)
- **User (2026-10-28):** "move on to phase 4". All open questions (D13-D15, Q1-Q14) were asked and answered, and the summary confirmed ("Yes, start"); see [phase 4 plan, user answers](plan/platform/implementation/phase-4-conversations.md#user-answers-2026-10-28) and [07 decisions](plan/platform/07-decisions-needed.md). Same commit arrangement as phases 1-3 (one commit per work item, pushed, draft PR; the user merges).
- **Answers that change the plan:**
  - rapid messages also join an enquiry that is still being read, and the round is re-run (Q3 = b);
  - customers are told when a quotation is withdrawn or expires (Q10), without the staff reason;
  - website chat visitors may send files through the quarantine (Q11): PDF, text, PNG, JPEG; 5 MB, 5 per message, 20 per session per day;
  - chat rate limits reuse the phase 2 PostgreSQL counters (Q14 = b, no release gate);
  - administrators **and sales operators** may merge contacts; no undo (D13).
- **W0 done:** answers recorded in the plan, 07 decisions, shared contracts (U2) and here.
- **Asked mid-work (2026-10-28):** cap of 200 chat reading rounds reached -> the message is stored and the enquiry goes to review (no AI call); START gets "You will receive messages from {business} again."; repeated identical messages are not dropped; the chat start form must be open at least 2 seconds. The user then asked to "build everything first" before closing phase 4.
- **W1 done:** migration `0012_conversations` (contacts, identities, conversations, append-only messages, `workflow_runs.round`, action refs, customer sessions, `connections.public_handle`, `tenants.acknowledge_enquiries`, reopen guard trigger, lookup policies, backfill, downgrade). `test_migrations_0012.py`.
- **W2 done:** `domain/contacts.py` (consent words, claimed details, masking), `domain/conversations.py` (`follow_up_target`), `quantity_quote` in the verbatim check, customer templates, permissions `conversations:view`, `contacts:details` (not viewers), `contacts:merge` (administrators, sales operators).
- **W3-W5 done:** `IntakeStore.record` routes through the conversation lock (`persistence/conversations.py`); `application/conversations.py` `IntentRouter` with pluggable handlers; API answer `continued`. PostgreSQL race tests: four answers at once reopen once; four first messages of a new customer give one contact and one enquiry.
- **W7-W8 done:** rounds (payload `round`, fresh thread per round), transcript port (`EnquiryTranscript`), per-message verbatim checks, LangChain message markers (customer-typed markers neutralised, our questions outside the data tag, too-long conversations refused), rules bare-answer binding (Q6); a round that missed a newer message or an uninspected file is history only and the enquiry is read again (Q3).
- **W9 done:** `CustomerMessenger` (push tools only; API customers read status as before), acknowledgement from preparation (D14 setting), questions from the clarify step, quotation ready from delivery, answer/withdrawn/expired notices from `notify_response` (now queued for every answer; managers still only for accept/decline), `CustomerConsentHook` (opted out -> failed with reason). Outbox messages with a conversation are also thread rows.
- **W10 done:** `customer_action_refs` issued at send time (`QuotationReadyHook`): button reference (sender identity checked) or secure-link token (hash only); `respond_by_reference` shares the API answer core and consumes the reference in the same transaction.
- **Final checks (2026-10-28):** ruff lint and format, strict mypy (266 files); 714 unit passed; `check_database.py --cycle` 851 passed, then downgrade to empty, upgrade, 851 passed again; portal typecheck, 104 tests (one timing failure during the parallel database run did not repeat in two reruns), build (portal 459 kB JS / 137 kB gzip; widget about 3 KB gzip).
- **Deviations so far (for review):**
  1. One action reference per offered version and identity (not one per action), so an offer is answered once whichever button is pressed.
  2. WI-6 (customer messages from webhook providers through `process_inbound`) is not built: no webhook customer provider exists before phase 5; the API and website chat route synchronously.
  3. Messages to API (pull) customers are not created: their system reads status and questions through the API as before.
  4. Two extra permissions (`contacts:details`, `contacts:merge`) besides `conversations:view`.
- **W11-W12 done:** `web_chat` provider (capability `hosted_chat`), one-click Enable with a public handle (changed once), allowed websites and greeting, public chat API (sessions, honeypot + 2 s form time, PostgreSQL rate limits, files through quarantine, STOP/START as consent events for the worker, Accept/Decline buttons, PDF download, end), secure answer link page, daily reading cap. `SALES_CHAT_IP_KEY` (required in production, with an https `SALES_PUBLIC_BASE_URL`).
- **W13-W14 done:** portal API and feature `conversations` (inbox, conversation, contacts with masking for viewers, manual merge with confirmation and no undo, acknowledgement switch), quotation page shows the enquiry's messages, website chat card on Connections (`GET /connections/available` keeps provider names out of the portal).
- **W15 done:** widget `portal/src/widget/` (plain TypeScript, Shadow DOM, about 3 KB gzip), hosted page `/chat/<handle>` and `/chat/widget.js` served by the API from `SALES_PORTAL_DIST_DIR`.
- **W16 done:** the demo page shows the industrial business's real website chat; API follow-ups are listed once.
- **W17 done:** [ADR 0021](adr/0021-customers-conversations-and-website-chat.md), [conversations](features/conversations.md), [website chat](features/website-chat.md), configuration, setup ("after pulling phase 4"), code map, testing, data model, intake/extraction, portal architecture, adding providers, shared contracts change log rev 5, follow-up plan marked superseded.
- **After pulling:** `uv sync --all-extras`, `uv run python scripts/init_local_env.py --add-missing`, `uv run alembic upgrade head`, `npm ci && npm run build` in `portal/` ([setup](development/setup.md#customers-conversations-and-website-chat-phase-4)).
- **Bug found by the live run and fixed:** approving a quotation whose enquiry envelope had no `reply_to` (written by an API process still running pre-phase-1 code) failed on a NULL delivery recipient (the `resume_quotation` job died). Delivery now falls back to the submitting connection; regression test `test_an_enquiry_stored_before_phase_1_is_still_delivered`.
- **Live run (2026-10-28, Gemini `google_genai:gemini-3.5-flash-lite`, synthetic data, API on :8010 with the built portal, real worker, dev database):**
  - hosted page and `/chat/widget.js` 200;
  - "can you get cables price?" -> acknowledgement + "We need a bit more information: - How many metre of 2-core power cable do you need?";
  - "10 meter?" -> the **same** enquiry, `ready_for_quotation`, one line IND-CABLE-2C 10 metre (384.00);
  - manager approval -> quotation card in the chat ("Your quotation Q-343EB9F6 (version 1) is ready: total 453.12 INR, valid until 2026-11-04 10:29 UTC.") with PDF (200, `%PDF-`) and Accept/Decline;
  - Accept -> `accepted` and "Thank you, we received your acceptance of Q-343EB9F6."; the inbox shows the visitor's conversation as accepted;
  - API and worker logs: no session tokens, bearer headers or visitor emails; no 5xx.
- **Not run:** the portal and widget were not clicked through in a real browser (no browser tool here); they are unit-tested, built, and the hosted page/widget were served. The conversation evaluation set (`evals/conversation_cases.jsonl`) was not added; the literal case is covered by the live run with Gemini and the rules variant by PostgreSQL tests.
- **More deviations (for review):**
  5. Ending a chat session only signs the visitor out; it does not opt them out (the plan proposed opting out; not decided by the user).
  6. The inbox hides chats that were started but have no message yet.
  7. Enabling a chat that lost its address (after a downgrade/upgrade) assigns a new one.
- **Next:** the user reviews PR #9 (deviations above, open points of phase 3 still open); click through the portal and the demo in a browser; then ask every open question for phase 5 first.

## Phase 5 provider framework and Telegram (complete, merged to main through PR #10, merge commit 3d0a928, 2026-10-29)
- **User (2026-10-29):** "start working on phase 5, phase 4 has been merged". All open questions were asked in six rounds and the summary confirmed ("Yes, start"); see [phase 5 plan, user answers](plan/platform/implementation/phase-5-one-click-providers.md#user-answers-2026-10-29) and [07 decisions](plan/platform/07-decisions-needed.md) (D5, D9, D10).
- **Scope (narrowed by the user):** the generic provider framework (platform webhooks, resume and pending actions, hosted provider pages, public media links, company-level provider settings, AI reading of manager replies with a "Yes" confirmation, signed approval link with sign-in, Enable cards and pending-action screens, Resend of failed customer messages, email parsing helpers) plus **Telegram** as the only real provider. No SendGrid, Twilio or Meta code. **No live accounts and no live run**: fixtures from the providers' documentation and fakes only.
- One branch, one commit per work item, one draft PR at the end (user answer).
- **W0 done:** answers recorded in the phase 5 plan, 07 decisions and here.
- **Asked mid-work (2026-10-29):**
  - Telegram customers get the website chat limits (10/min, 100/day per sender; the 200 readings a day are shared by all push customer tools); over a limit a message is stored but held for review;
  - files: PDF, text, PNG and JPEG up to 5 MB; other kinds skipped and noted;
  - an answer other than YES/NO to "You want to …?" is read as a new reply;
  - a manager writing to the bot without reply-to gets a generic hint.
- **W1 done:** migration `0013_provider_state` (routes, reply prompts, reply audit, media links, outbox action reference and resend link, pending step, grants). `test_migrations_0013.py`.
- **W2 done:** provider contract (`ProviderDefaults`, lifecycle, platform webhooks, handshakes, files, hints, provider pages, cost notes), new events, platform secret namespace, `InMemorySecretStore`.
- **W3-W4 done:** `application/lifecycle.py` (Enable with cost tick, pending steps, setup events, poll, test, disable); platform route `/api/v1/hooks/p/{key}` and GET handshakes; `CustomerInbox` (webhook customer messages through intake, limits, files, `hold_for_review`); staff messages as reader context; public PDF links (`/api/v1/media/{token}`); Resend (`messages:resend`).
- **W5-W6 done:** approval by message: guards, YES/NO confirmation, reason prompts, recorded/state confirmations, rules mode not reading replies, reply audit, `classify_manager_reply`, `evals/manager_replies.jsonl` (80 cases) and `scripts/evaluate_replies.py`. Signed link API `/api/v1/approval-links/{token}` (sign-in, CSRF); approval requests carry `<portal>#/approve/<token>`.
- **W7 done:** `adapters/providers/email_common` (threading, new text, automatic mail, DKIM/SPF trust), unused.
- **W8 done:** `adapters/providers/telegram` (business connection for customers, bot chat for managers, `/link` code, deep-link enrollment, buttons, replies, files, 24 h refusals), `python -m sales_platform.ops platform-config|platform-setup`, `httpx` limited to provider packages. Fixtures written from the Bot API docs (`tests/data/providers/telegram/updates.json`).
- **W9 done:** portal: one-click cards (cost tick), pending step panel (steps, page, provider page, form), Test, Disable with typed name, polling while setting up, linking hints, Send again, `#/approve/:token` page.
- **W10-W11 done:** platform route HTTP tests; docs: [ADR 0022](adr/0022-one-click-providers-and-approval-by-message.md), [connections](features/connections.md), [Telegram](features/telegram.md), [approval by message](features/approval-by-message.md), [provider setup](operations/provider-setup.md), configuration, setup, code map, testing, data model, approvals, security, adding providers, contracts rev 6.
- **Final checks (2026-10-29):**
  - ruff lint and format, strict mypy (290 files);
  - 856 unit tests passed;
  - `check_database.py --cycle`: 1008 passed, then downgrade to empty, upgrade, 1008 passed again;
  - portal: typecheck, 119 tests, build (471 kB JS / 140 kB gzip; widget about 3 KB gzip).
- **Not run:**
  - no live run (no accounts, user answer); nothing has touched Telegram's real API;
  - no model evaluation of the reply set;
  - the portal was not clicked through in a browser.
- **Deviations and choices (for review):**
  1. Enable provisions in the request (no `provision_connection` job); long tool reviews use `poll_connection`.
  2. Routes use a lookup policy (like the other cross-tenant lookups), not a `SECURITY DEFINER` function.
  3. One `approval_reply_prompts` table for both reason and confirm prompts (instead of `approval_reason_prompts`).
  4. A conditional approval becomes "unclear" (asked again), not "revise" as the plan suggested: the safer reading.
  5. Every message decision (buttons too) gets the recorded confirmation (D10 rule 4); decisions through the deprecated simulated alias route do not.
  6. The 200 readings a day are shared by all push customer tools of a business (website chat + Telegram), not per tool.
  7. Over-limit messages that join an enquiry still being read are not held (that round re-runs once); the daily cap still applies.
  8. Telegram's rate limit is handled by retrying on 429; no shared limiter.
- **Next:** the user reviews the draft PR; for a live Telegram run, create a bot and follow [provider setup](operations/provider-setup.md#telegram) with a tunnel; optionally run `scripts/evaluate_replies.py` with a model.

## Phases 6 and 7: ERPNext product source and orders (implemented, branch `feat/phase-6-7-erp-and-orders`)
- **User (2026-10-29):** "complete remaining phase 6 and 7. I have merged phase 6 PR". The merged PR was #10, phase 5; phases 6 and 7 had not started.
- **Answered by the user in two rounds:**
  - D21: ERPNext, sandbox only (no pilot business), local Docker;
  - read mode: hybrid;
  - sync interval per business;
  - stock: a setting, default available;
  - tax-inclusive prices refused;
  - warehouses: a chosen list, summed;
  - items: enabled sales items of chosen groups;
  - one price list;
  - one PR for both phases;
  - Q1: creation is a per-business setting;
  - Q2: operators, approvers and administrators manage orders;
  - Q3: staff may enter the ERP customer;
  - Q4: one order per quotation.
- **Delegated:** during round three the user said "for this specific phase you will answer all questions, don't ask me". Claude decided Q5-Q16 and the phase 6 details; they are marked **(Claude)** in [ADR 0023](adr/0023-erpnext-product-source.md) and [ADR 0024](adr/0024-orders.md), and listed in the [plan's answers](plan/platform/implementation/phase-6-7-pilot-source-and-orders.md#answers-2026-10-29). The delegation covers this phase only; ask-before-building still applies afterwards.
- **Built:**
  - provider package `adapters/providers/erpnext` (one-form Enable, a synced copy for search, live prices and stock, an order writer with the reference in `po_no`);
  - generic catalogue/order capabilities;
  - one product source and order destination per business (replace and restore);
  - the `sync_catalogue` job and runs;
  - orders (domain, store, service, five job handlers, track my order, API, portal);
  - migrations `0014_catalogue_sync` and `0015_orders`;
  - a simulated writer with fault modes;
  - the sandbox `infra/erpnext/compose.yaml` plus `scripts/seed_erpnext_sandbox.py`.
  See [ERPNext](features/erpnext.md) and [orders](features/orders.md).
- **Spike on the sandbox (plan §5.1), all five checks passed:**
  - items, prices and Bins read;
  - available = actual - reserved, and a submitted order reserves its stock;
  - `po_no` lookup works;
  - a second create with the same reference returns the same order;
  - the accepted rate is kept.
- **Found and fixed during checks:**
  - enabling a new source updated the connection before switching the old one off, which hit the one-source index;
  - a first "Sync now" design gave the API role read access to jobs, which a test forbids. It now uses a partial unique index (`uq_jobs_manual_sync`) instead.
- **Final checks (2026-10-29):**
  - ruff lint and format, strict mypy (335 files);
  - 949 unit tests passed (13 skipped: sandbox and integration cases);
  - `check_database.py --cycle`: 1110 passed, then downgrade to empty, upgrade, 1110 passed again (14 skipped: the sandbox cases);
  - **live run against the local ERPNext sandbox** (`SALES_TEST_ERPNEXT_*` set): 13 passed. `test_erpnext_live.py` covers Enable with the form, replacing the product list, the first copy, an enquiry priced live from ERPNext (`price_source erpnext`), approval, acceptance, the automatic order written once to ERPNext with `po_no = QO-…`, and the customer's pull view. The sandbox tests and the writer contract also passed;
  - portal: typecheck, 124 tests, build (485 kB JS / 143 kB gzip).
- **Not run:** the portal was not clicked through in a browser; no real business ERPNext (sandbox only, user answer).
- **Choices for review (all in the ADRs):**
  1. Full feed every run.
  2. Typed products with an ERP SKU are adopted; products the ERP no longer offers are archived.
  3. Production accepts only public https ERPNext addresses.
  4. ERP orders arrive as drafts unless "Submit orders" is on; ERP writes are off by default.
  5. No reservation by us.
  6. A shortfall holds the order.
  7. Look-ups every 5 minutes for 1 hour, then needs attention, with no message.
  8. Fixed customer wording for confirmed, shipped, delivered and cancelled.
  9. "Track my order" answers fixed phrases or an order number only.
  10. Settings change by Disable and Enable.
- **Next:**
  - The user reviews; commit, push and the PR only with the user's permission (AGENTS.md).
  - Click through the portal (Connections → ERPNext, Products → "Copy from your ERP", Orders) against the sandbox.
  - When a pilot business exists, ask it the plan §5.6 questions.

## Next task
- **Platform plan for review** (user request 2026-10-23): self-service businesses, generic connectors (WhatsApp, Telegram, Instagram, email, website chat...), product data sources, future orders/payments/inventory. [plan/platform/README.md](plan/platform/README.md); decisions in [07-decisions-needed.md](plan/platform/07-decisions-needed.md). Nothing built until the user answers. Detailed step-by-step plan per phase: [plan/platform/implementation/README.md](plan/platform/implementation/README.md) (shared contracts first).
- Follow-up messages that continue the customer's open enquiry: planned, waiting for the user's confirmation ([plan/conversation-follow-ups.md](plan/conversation-follow-ups.md)).
- Commits, pushes and PRs only with the user's explicit permission each time (AGENTS.md).
0. When the user has a Gemini key in `.env`: run `uv run python scripts/evaluate_extraction.py --model google_genai:gemini-3.5-flash-lite` and record the result in ADR 0017.
1. Optionally open the portal in a browser (use "Run all" in VS Code):
   - Run `npm run dev` in portal/, plus the API and worker.
   - Visit http://localhost:5173/portal/.
   - Follow [setup](development/setup.md#approvals-and-portal-m4).
2. Phases 6-7 are implemented (see above); after review, ask the user every open question for the next phase first (clarify-before-building).
- Update this handoff in every change; never mark milestones complete without evidence.
