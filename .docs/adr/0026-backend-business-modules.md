# 0026: Backend by business module, one shared wiring, and the `sales_platform` name
**Date:** 2026-11-02. **Status:** accepted (user, 2026-11-03: "continue with the plan"); being implemented step by step. Builds on ADR 0001 (channel-independent core), 0009 (tooling and database roles), 0014 (the portal's feature structure) and 0018 (connector framework).

## Context
The backend started as a quotation tool. It now also runs accounts and onboarding, the catalogue and sync, enquiries and extraction, customer conversations and website chat, quotations with approval, delivery and acceptance, orders, and the integrations framework. A review on 2026-11-02 found:

| # | Finding | Evidence |
|---|---|---|
| 1 | Code is grouped by technical layer, so one business area is spread over 6–7 folders and nothing shows or enforces where one area ends | e.g. orders: `domain/orders.py`, `ports/orders.py`, `application/orders.py`, `persistence/orders.py`, `api/orders.py` and two wiring files |
| 2 | Two wiring places build the same objects | `api/app.py` builds 57 objects and `composition.py` 44; 20 are built in both (`OrderService`, `ApprovalService`, `SqlOrderStore`...), so they can drift |
| 3 | The import-rule test does not cover `persistence` or `api` | `tests/unit/test_architecture.py`, `ALLOWED` has domain, ports, application, adapters and workflows only |
| 4 | One table file for the whole system | `persistence/models.py`, 1,865 lines |
| 5 | Leftovers | empty `adapters/channels/` and `adapters/delivery/`; the deprecated route in `api/channels.py` (its docstring says "removed in phase 4/5"); a 525-line in-memory catalogue double in `adapters/catalogue/memory.py` |
| 6 | Names no longer fit | package `quotation_app`, portal "Quotation Portal"; the generic product-source and order-writer ports are `ErpReader` / `ErpWriter` even when the source is our own list |

Size at review: about 36,000 lines in `src/` (persistence 11.3k, application 7.3k, api 5.0k, adapters 4.7k, domain 4.3k, ports 3.3k, workflows 0.7k) and about 20,600 lines of tests (84 unit, 30 integration files).

## Decision (user answers, 2026-11-02)
| Question | Answer |
|---|---|
| Scope | Clean-ups **and** regrouping into business modules. No behaviour change. |
| Name | Rename to **`sales_platform`**; the portal becomes "Sales Portal". |
| Deprecated simulated route | **Remove** it; the demo page and scripts use the generic hook route `/api/v1/hooks/{connection}/{token}`. |
| Delivery | One PR per step; Claude commits, opens and merges each PR after CI passes (user, 2026-11-03). |
| Q1 Settings prefix | Rename `QUOTATION_` → `SALES_` **at once**, no old-name fallback (every `.env` and CI value changes in that step). |
| Q2 Database names | Rename the roles and databases to `sales_*`, and **replace the 15 migrations with one fresh baseline**. Local databases are deleted and recreated, then demo data re-seeded (user accepted losing local data; all data is synthetic). No downgrade path to the old migrations. |
| Q3 Repository | Rename the repository too. The user renames it on GitHub; Claude updates links in docs and CI. |
| Q4 Offline evaluation | **Remove the whole offline evaluation** (`evaluation.py`, the offline mode of `scripts/evaluate_extraction.py`, its fixtures and test, the in-memory catalogue). The enquiry reader is covered by its unit tests and live runs with a model key. |

### Target structure
A modular monolith: one application, one database, one deployment, but split by business module. Each module keeps today's inner layers, so the existing layer rules still apply inside it.

```text
src/sales_platform/
  platform/        shared core every module may use: tenancy context, money, validation, jobs and
                   the job runner, database engine and table base, secrets, storage, workflow
                   checkpoints, settings, ops
  accounts/        users, sessions and sign-in, businesses, members, invitations, API keys
  integrations/    providers framework, connections, secrets use, one webhook route, inbound
                   events, the outbound outbox, provider packages (api, simulated, web_chat,
                   telegram, erpnext, local_catalogue)
  catalogue/       products, prices, stock, categories, product sources, sync
  enquiries/       intake, attachments, requirement extraction (interpreters, enquiry workflow)
  conversations/   contacts, conversations, website chat, customer messages and actions
  quotations/      drafting and pricing, versions, approval (portal, message, signed link),
                   documents and delivery, customer acceptance, manager replies, quotation workflow
  orders/          orders, order writer operations, reconciliation, track my order
  bootstrap.py     the ONE wiring: builds stores and services once for the API and the worker
  api_app.py       FastAPI app: routers from each module + bootstrap
  worker.py        job worker: handlers from each module + bootstrap

  <module>/        inside every module (only the parts it needs):
    domain/        rules and types (stdlib only)
    ports.py       the interfaces it needs from outside
    application/   use cases and job handlers
    persistence/   SQL stores and this module's tables (tables.py)
    api/           FastAPI routers
    __init__.py    the module's public names: the only import other modules may use
```

The existing files move as follows. The list is indicative; each step checks it against the code.

| Module | From `domain/` | From `application/` | From `persistence/` | From `api/` | Other |
|---|---|---|---|---|---|
| platform | money, validation, tenancy, jobs | tenancy, jobs | database, tenancy, jobs, rate_limits, workflow, Base of models | — | config, ops, adapters/secrets, adapters/storage; ports tenancy, jobs, secrets, storage, workflow |
| accounts | accounts | accounts, api_keys, sessions | accounts, api_keys, sessions | accounts, auth, business | adapters/accounts, adapters/identity; ports accounts, api_keys, identity |
| integrations | connections | connections, providers, plugins, lifecycle, inbound, channel_actions, messaging | connections, provider_state, channels, inbound, messages | connections, hooks | adapters/providers/*; ports connections, providers, channels, inbound, messages |
| catalogue | catalogue, catalogue_admin, catalogue_search, catalogue_sync | catalogue_admin, catalogue_sources, catalogue_sync | catalogue, catalogue_sync | catalogue | adapters/catalogue/local; ports catalogue_admin, catalogue_sync, the reader half of erp |
| enquiries | intake, attachments, requirements | intake, preparation, media | intake, media | enquiries, media | workflows/enquiry, adapters/interpreters (rules, langchain, factory); ports intake, interpretation, media |
| conversations | conversations, contacts, chat, messages | conversations, contacts, chat, customer_inbox, customer_messages, customer_actions, resend | conversations, contacts, chat, customer_actions | conversations, public_chat | ports conversations, contacts, chat, customer_actions |
| quotations | quotations, pricing, documents, acceptance, manager_replies | quotations, pricing, approvals, approval_links, documents, delivery, acceptance, manager_replies, notifications | quotations, documents, acceptance, manager_replies | portal, pdf, approval_links | workflows/quotation, adapters/documents, adapters/interpreters/reply_classifier; ports quotations, approvals, documents, acceptance, manager_replies, reply_classification |
| orders | orders | orders | orders | orders | the writer half of ports/erp; adapters/erp/simulated_orders |

### Rules (enforced by tests, no new dependency)
1. **Inside a module:** today's layer rules (domain stdlib only; application uses domain and ports; adapters and persistence implement ports).
2. **Between modules:** a module imports another module only through its `__init__.py` public names, never its inner files. `platform` imports no business module.
3. **No cycles between modules.** Where today's code calls in both directions (likely conversations ↔ quotations, integrations ↔ quotations for approvals by message), the dependency is inverted with a port owned by the calling module and implemented in the other. Each such case is listed in the step's PR.
4. **One wiring:** only `bootstrap.py`, `api_app.py` and `worker.py` construct stores and services; the test fails if a route or handler builds its own.
5. The existing checks stay: provider names only inside provider packages; HTTP clients only inside provider packages.

### Steps (one PR each, all checks green after every step)
| Step | What | Risk |
|---|---|---|
| 0 | Docs: record PR #12 (merged as `fee24c5`, CI green), this ADR and the answers | none |
| 1 | Guard rails: the architecture test also covers `persistence` and `api`; one shared `bootstrap` used by the API and worker (removes the 20 double constructions). No files move | low |
| 2 | Leftovers: remove the empty folders, the deprecated simulated route (demo page and scripts move to the hook route) and the whole offline evaluation (Q4) | low |
| 3 | Rename the package `quotation_app` → `sales_platform`, the entry-point groups, the project name, the portal title, and the settings prefix `QUOTATION_` → `SALES_` (Q1) | medium (wide diff) |
| 4 | Database: one fresh baseline migration with `sales_*` role and database names, replacing 0001–0015; init scripts, `.env.example`, CI and a one-command "recreate local database" script (Q2). Done: the baseline is the pg_dump of the old head with the roles renamed, proven equal by comparing dumps; schema-rule tests kept in `test_schema_rules.py` | high (local data is recreated) |
| 5 | Create `platform/` and move the shared core; split `models.py` so each module owns its tables (Alembic still sees one metadata) | medium |
| 6–12 | Move one module per PR: accounts, integrations, catalogue, enquiries, conversations, quotations, orders. Turn on the module-boundary rule for each module as it lands | medium |
| 13 | Interface names: `ErpReader` → `ProductSource`, `ErpWriter` → `OrderWriter` (with the catalogue/orders moves if simpler) | low |
| 14 | Repository: after the user renames it on GitHub, update links in docs and CI (Q3); docs (code map, module boundaries, execution flow, configuration, testing); this ADR becomes "implemented" | none |

Verification for every step: Ruff, strict mypy, unit tests, PostgreSQL integration tests with `scripts/check_database.py --cycle`, the portal checks when it is touched, and CI. Behaviour is unchanged, so no test changes its expectations; only imports and paths.

## Open points (to ask the user before the step that needs them)
Q1–Q4 were answered on 2026-11-03 (see the decision table). Still open:

| # | Question | Why it matters | Needed by |
|---|---|---|---|
| Q5 | How each module cycle found in steps 6–12 is broken | Changes which module "owns" a rule | The step that finds it |

## Alternatives considered
- **Keep layers, only clean up:** cheapest, but finding 1 stays and the system keeps growing sideways.
- **Separate services per module:** more deployments, a network between modules and distributed transactions (orders and stock need one transaction). Not needed at this size; a modular monolith keeps the option open.
- **import-linter or tach:** good tools, but a new dependency; the existing AST test already does this job and is extended instead.

## Consequences
- Finding a feature means opening one folder; module boundaries are visible and tested.
- About 14 PRs of mostly mechanical moves; each is reviewable on its own and can be paused between steps.
- Old import paths in notes, branches and docs break after step 3; the docs are updated in the same PRs.

## Addendum: the module order (user approved, 2026-11-03)
An analysis before step 6 showed that moving the files as planned would leave 18 pairs of modules that depend on each other both ways (13 even with 17 shared contract files in the core). The user chose to untangle every link now, and approved this order. A module may use only the modules above it, and only through their public names:

| # | Module | Owns |
|---|---|---|
| 1 | `platform` | tenancy context and permissions, money, validation (with email checks), jobs and the job runner, storage, secrets, rate limits, secret hashing, text safety, web sign-in and sessions (`PortalAuth`), LangGraph checkpoints, the message engine (each module registers its templates) |
| 2 | `integrations` | connections, the provider registry and the generic provider contract (messages, webhooks), the webhook route, inbound event types and the router (routes are registered by modules), the outbox and sending, website-chat connection settings |
| 3 | `accounts` | users, businesses, members, invitations, API keys (and their API connection), the business logo, account emails |
| 4 | `catalogue` | products, prices, stock, the `ProductSource` port and its provider contract, sync |
| 5 | `conversations` | contacts, threads, consent, the customer messenger, staff messages |
| 6 | `enquiries` | intake, attachments, requirement extraction, the enquiry workflow, reading document text |
| 7 | `quotations` | pricing and the pricing policy, versions, approvals (portal, linked manager phones, by message, signed link), PDFs and delivery, acceptance, manager replies, the quotation workflow |
| 8 | `orders` | orders, the `OrderWriter` port and its provider contract, "track my order" |
| 9 | `customer_channels` | the website chat service, the customer inbox and the customer API (they combine enquiries, quotations and orders for one customer) |
| – | `providers/` | the plug-in packages (api, simulated, web_chat, telegram, erpnext, local_catalogue): may use any module; nothing imports them (entry points only) |

Key moves that break the links:
- Each module defines its own plug-in contract (catalogue: `ProductSource`; orders: `OrderWriter`); the integrations provider contract covers messaging and webhooks only.
- Inbound routes (approve, accept, consent, track my order...) live in their own module; integrations only dispatches to the routes the wiring registers.
- Outbox status changes notify modules through hooks in the same transaction (quotation delivery status, the conversation thread, the expiry job) instead of the outbox writing their tables.
- Manager phone links and approval notices move to quotations.

Delivery: one PR per module group, in the order above; the architecture test gains the module order and enforces it as each module lands. Behaviour does not change.
