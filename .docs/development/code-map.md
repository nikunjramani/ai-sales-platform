# Code map
Status: everything listed here exists. Since ADR 0026 (2026-11-03) the code is grouped by business module; the sections further down are the history by milestone, with links updated to the new places.

## Business modules (ADR 0026)
`src/sales_platform/` holds nine modules in a fixed order; a module may import only the modules before it, and inside a module the layer rules apply (domain, ports, application, adapters, persistence, api, workflows). [test_architecture.py](../../tests/unit/test_architecture.py) enforces both.

| # | Module | Owns | Main files |
|---|---|---|---|
| 1 | `platform/` | tenancy context and permissions, money, validation, jobs and the job runner (`ServiceError`), storage, secrets, rate limits, secret hashing (`domain/security.py`), text safety (`domain/text.py`), sign-in guard (`api/auth.py`: `PortalAuth`), sessions, settings (`config.py`), `Base` and the platform tables | `domain/`, `application/jobs.py`, `persistence/models.py` |
| 2 | `integrations/` | connections, provider registry and the generic provider contract (messages, webhooks), inbound events (`domain/events.py`) and their router, the outbox (`domain/messages.py`, `persistence/messages.py` with `OutboxListener`), website-chat connection settings (`domain/hosted_chat.py`) | `application/inbound.py`, `application/messaging.py`, `api/hooks.py` |
| 3 | `accounts/` | users, businesses, members, invitations, API keys, logo, account emails (`domain/emails.py`), session routes (`api/auth.py`) | `application/accounts.py`, `persistence/accounts.py` |
| 4 | `catalogue/` | products, prices, stock, the `ProductSource` port (`ports/erp.py`) and its provider contract (`ports/sources.py`), sync | `application/catalogue_admin.py`, `persistence/catalogue.py` |
| 5 | `conversations/` | contacts, threads, consent, the customer messenger, consent and staff-message routes, the thread outbox listener | `application/customer_messages.py`, `persistence/conversations.py` |
| 6 | `enquiries/` | intake, attachments, requirement extraction, follow-up routing (`*/follow_ups.py`), customer targets, staff messages store, document text, the enquiry workflow | `application/intake.py`, `workflows/enquiry.py` |
| 7 | `quotations/` | pricing and pricing policy, versions, approvals (portal, linked phones, by message, signed link), inbound routes for managers and customer buttons, PDFs, delivery and the delivery outbox listener, acceptance, media links, manager replies, the quotation workflow, quotation message texts | `application/approvals.py`, `workflows/quotation.py` |
| 8 | `orders/` | orders, the `OrderWriter` provider contract (`ports/writers.py`), reconciliation, "track my order" | `application/orders.py` |
| 9 | `customer_channels/` | website chat, the customer inbox, the customer API (`api/enquiries.py`) | `application/chat.py`, `api/public_chat.py` |
| – | `providers/` | plug-in packages (api, simulated, web_chat, telegram, erpnext, local_catalogue); loaded only by entry point | `providers/*/` |
| – | wiring | `composition.py` (`build_core`, `build_outbox`, `build_catalogue_store`, job handlers), `api_app.py`, `worker.py`, `seed.py`, `ops.py` | |

Each module's tables are in `<module>/persistence/tables.py`; `migrations/env.py` imports them all so Alembic sees one metadata. Tests mirror this layout under `tests/unit/<module>/...` and `tests/integration/<module>/...`.

## Implemented foundation
| Actual file | Responsibility |
|---|---|
| [pyproject.toml](../../pyproject.toml) / [uv.lock](../../uv.lock) | Package configuration, quality checks and reproducible dependencies |
| [config.py](../../src/sales_platform/platform/config.py) | Environment/.env validation and secret wrappers |
| [app.py](../../src/sales_platform/api_app.py) | Application factory, lifecycle and health routes |
| [database.py](../../src/sales_platform/platform/persistence/database.py) | Bounded connection pool and schema/runtime-role readiness |
| [models.py](../../src/sales_platform/platform/persistence/models.py) | Tenant, Membership and TenantPricingPolicy table metadata |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0001_tenants`) | Initial schema, forced RLS and runtime read grants |
| [env.py](../../migrations/env.py) | Alembic execution with separate owner credentials |
| [seed.py](../../src/sales_platform/seed.py) | Idempotent development/test-only fictional tenants, memberships and policies |
| [compose.yaml](../../compose.yaml) / [init.sql](../../infra/postgres/init.sql) | Local database container, persistent volume and role bootstrap |
| [init_local_env.py](../../scripts/init_local_env.py) | Generate ignored local credentials without overwriting |
| [check_database.py](../../scripts/check_database.py) | Run local migration/schema/integration checks |
| [tests/](../../tests/conftest.py) | `unit/` mirrors `src/sales_platform`; `integration/` needs PostgreSQL; shared fixtures in conftest files. See [testing](testing.md) |
| [ci.yml](../../.github/workflows/ci.yml) | Automated offline and live PostgreSQL checks; passing on main |

## Implemented M1 domain and tenancy
Dependency direction: application -> ports -> domain; adapters and persistence implement ports. [test_architecture.py](../../tests/unit/test_architecture.py) enforces the domain/ports/application/adapters import rules.

| Actual file | Responsibility |
|---|---|
| [domain/money.py](../../src/sales_platform/platform/domain/money.py) | Decimal Money, currency codes, rounding modes, exact multiplication |
| [domain/tenancy.py](../../src/sales_platform/platform/domain/tenancy.py) | Roles, permissions, TenantContext, authorization errors |
| [domain/catalogue.py](../../src/sales_platform/catalogue/domain/catalogue.py) | ERP fact values: Product, PriceQuote, StockLevel |
| [domain/pricing.py](../../src/sales_platform/quotations/domain/pricing.py) | PricingPolicy and the deterministic `calculate_quotation` rules |
| [ports/erp.py](../../src/sales_platform/catalogue/ports/erp.py) | ProductSource protocol and normalized ErpError codes |
| [ports/tenancy.py](../../src/sales_platform/platform/ports/tenancy.py) | Membership and pricing-policy reader contracts |
| [application/tenancy.py](../../src/sales_platform/platform/application/tenancy.py) | TenantContextResolver: subject + stored membership -> context |
| [application/pricing.py](../../src/sales_platform/quotations/application/pricing.py) | QuotationPricingService use case (read-only) |
| [adapters/erp/simulated.py](../../src/sales_platform/catalogue/adapters/erp/simulated.py) | Per-tenant in-memory ERP, failure injection, synthetic demo catalogues |
| [persistence/tenancy.py](../../src/sales_platform/platform/persistence/tenancy.py) | `tenant_transaction` and SQL membership/policy readers |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0002_memberships_policies`) | Membership and pricing-policy tables, forced RLS, runtime SELECT grants |
| [demo_quotation.py](../../scripts/demo_quotation.py) | Prints a pricing trace and access-denial examples |
| [tests/unit/domain/](../../tests/unit/quotations/domain/test_pricing.py) | Rule tests: rounding, quantities, units, prices, stock, roles |
| [tests/unit/application/](../../tests/unit/quotations/application/test_pricing.py) / [test_erp_contract.py](../../tests/unit/catalogue/ports/test_erp_contract.py) | Use-case behaviour and the shared ERP adapter contract suite |
| [tests/integration/persistence/test_tenancy.py](../../tests/integration/platform/persistence/test_tenancy.py) | Two-tenant negative tests through the PostgreSQL runtime role |

## Implemented M2 intake, jobs and attachments
Migrations: one baseline, [0001_baseline.py](../../migrations/versions/0001_baseline.py) + [.sql](../../migrations/versions/0001_baseline.sql), replaced the 15 earlier migrations (ADR 0026 step 4); the rows below say what each former migration created.

Entry points: the HTTP API ([api/app.py](../../src/sales_platform/api_app.py)) and the worker process ([worker.py](../../src/sales_platform/worker.py)). Both are composition roots that wire adapters to application services; the services both use are built once by `composition.build_core` (ADR 0026 step 1). See [enquiry intake](../features/enquiry-intake.md).

| Actual file | Responsibility |
|---|---|
| [domain/intake.py](../../src/sales_platform/enquiries/domain/intake.py) | Submission limits, payload fingerprint, idempotency-key rules, canonical InboundEnvelope |
| [domain/attachments.py](../../src/sales_platform/enquiries/domain/attachments.py) | Storage keys, attachment statuses, content inspection |
| [domain/jobs.py](../../src/sales_platform/platform/domain/jobs.py) | Job states, kinds, PermanentJobError, backoff with jitter |
| [ports/intake.py](../../src/sales_platform/enquiries/ports/intake.py), [ports/jobs.py](../../src/sales_platform/platform/ports/jobs.py), [ports/storage.py](../../src/sales_platform/platform/ports/storage.py), [ports/api_keys.py](../../src/sales_platform/accounts/ports/api_keys.py) | Contracts for receipt storage, job queue, object storage and key lookup |
| [application/intake.py](../../src/sales_platform/enquiries/application/intake.py) | EnquiryIntakeService: replay/conflict handling, quarantine writes, cleanup on failure |
| [application/api_keys.py](../../src/sales_platform/accounts/application/api_keys.py) | Key generation, token parsing, constant-time authentication |
| [application/preparation.py](../../src/sales_platform/enquiries/application/preparation.py) | Idempotent attachment inspection job handler |
| [application/jobs.py](../../src/sales_platform/platform/application/jobs.py) | JobRunner: complete, retry with backoff, dead-letter, lease-loss tolerance |
| [api/enquiries.py](../../src/sales_platform/customer_channels/api/enquiries.py) | `/api/v1/enquiries` routes, bearer auth, size limit, error mapping |
| [worker.py](../../src/sales_platform/worker.py) | `python -m sales_platform.worker [--once]` |
| [persistence/intake.py](../../src/sales_platform/enquiries/persistence/intake.py) | Atomic receipt (inbox + enquiry + attachments + job), status reads, worker updates |
| [persistence/jobs.py](../../src/sales_platform/platform/persistence/jobs.py) | SKIP LOCKED claims, leases, fenced completion |
| [persistence/api_keys.py](../../src/sales_platform/accounts/persistence/api_keys.py) | Key lookup via the narrow RLS policy; owner-only create/revoke |
| [adapters/storage/local.py](../../src/sales_platform/platform/adapters/storage/local.py) | Local disk storage with strict keys and atomic writes |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0003_intake`) | Intake/job tables, composite tenant FKs, RLS, worker/API grants |
| [tests/unit/application/](../../tests/unit/enquiries/application/test_intake.py), [tests/unit/api/test_enquiries.py](../../tests/unit/customer_channels/api/test_enquiries.py) | Use-case and HTTP contract tests over in-memory fakes |
| [tests/integration/api/test_enquiries.py](../../tests/integration/customer_channels/api/test_enquiries.py), [test_jobs.py](../../tests/integration/platform/persistence/test_jobs.py) | End-to-end intake, races, durability, crash recovery and role isolation on PostgreSQL |

## Implemented M3 extraction workflow
Entry point: `extract_requirements` jobs in the worker ([worker.py](../../src/sales_platform/worker.py)). See [enquiry extraction](../features/enquiry-extraction.md). Import rules: `workflows` may use domain, ports, application, `langgraph` and `langchain_core`; provider SDKs stay in `adapters`.

| Actual file | Responsibility |
|---|---|
| [domain/requirements.py](../../src/sales_platform/enquiries/domain/requirements.py) | RequestedItem, verbatim checks, unit aliases, decide_match, assess and template questions |
| [ports/interpretation.py](../../src/sales_platform/enquiries/ports/interpretation.py) | EnquiryInterpreter (the swap point for models), DocumentReader, DocumentText |
| [ports/workflow.py](../../src/sales_platform/enquiries/ports/workflow.py) | WorkflowStore: enquiry content, run/thread mapping, outcome recording |
| [workflows/enquiry.py](../../src/sales_platform/enquiries/workflows/enquiry.py) | LangGraph graph (extract -> match -> assess -> ready/clarify/review) and ExtractionJobHandler |
| [adapters/interpreters/rules.py](../../src/sales_platform/enquiries/adapters/interpreters/rules.py) | Free offline rules interpreter (default) |
| [adapters/interpreters/langchain.py](../../src/sales_platform/enquiries/adapters/interpreters/langchain.py) | Any LangChain chat model: JSON + strict Pydantic, data-tagged prompts |
| [adapters/interpreters/factory.py](../../src/sales_platform/enquiries/adapters/interpreters/factory.py) | Generic interpreter selection: no model = rules; `<provider>:<model>` = any LangChain chat model via `init_chat_model` (ADR 0017) |
| [adapters/documents/text.py](../../src/sales_platform/enquiries/adapters/documents/text.py) | Bounded PDF/text reading; unsupported reasons instead of empty text |
| [persistence/workflow.py](../../src/sales_platform/enquiries/persistence/workflow.py) | SqlWorkflowStore and the worker-role PostgreSQL checkpointer |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0004_workflow`) | workflow_runs, frozen LangGraph checkpoint tables, M3 enquiry statuses, worker grants |
| [tests/unit/workflows/test_enquiry.py](../../tests/unit/enquiries/workflows/test_enquiry.py), [tests/integration/workflows/test_enquiry.py](../../tests/integration/enquiries/workflows/test_enquiry.py) | Branches, invented facts, crash/resume, isolation; Postgres end to end including M1 pricing |

## Implemented M4 drafting, approval and portal
Entry points:
- quotation jobs in the worker;
- portal and channel routes in the API ([api/app.py](../../src/sales_platform/api_app.py));
- the React app in [portal/](../../portal/src/app/App.tsx).

See [quotation approval](../features/quotation-approval.md).

| Actual file | Responsibility |
|---|---|
| [domain/quotations.py](../../src/sales_platform/quotations/domain/quotations.py) | Statuses, DecisionCommand, version content and hash, review text, approval errors |
| [ports/approvals.py](../../src/sales_platform/quotations/ports/approvals.py), [ports/quotations.py](../../src/sales_platform/quotations/ports/quotations.py), [ports/channels.py](../../src/sales_platform/quotations/ports/channels.py), [ports/identity.py](../../src/sales_platform/platform/ports/identity.py) | Decision unit of work; quotation read, revision and workflow stores; channel bindings, references and notifier; identity and sessions |
| [application/approvals.py](../../src/sales_platform/quotations/application/approvals.py) | ApprovalService: the one decision path for portal and channels |
| [application/quotations.py](../../src/sales_platform/quotations/application/quotations.py) | List, detail, re-priced revisions |
| [application/channel_actions.py](../../src/sales_platform/quotations/application/channel_actions.py) / [notifications.py](../../src/sales_platform/quotations/application/notifications.py) | Enrollment and channel decisions / review delivery job |
| [application/sessions.py](../../src/sales_platform/platform/application/sessions.py) | Session tokens, expiry, CSRF |
| [workflows/quotation.py](../../src/sales_platform/quotations/workflows/quotation.py) | Approval graph with interrupts; QuotationJobHandler (run lock, stale-resume protection) |
| [adapters/identity/dev.py](../../src/sales_platform/platform/adapters/identity/dev.py) | Development login (development/test only; password login is in phase 2 below) |
| [adapters/providers/simulated/](../../src/sales_platform/providers/simulated/provider.py) | Simulated provider (phase 1): outbox files, HMAC signatures |
| [api/auth.py](../../src/sales_platform/platform/api/auth.py), [api/portal.py](../../src/sales_platform/quotations/api/portal.py), api/channels.py (removed in ADR 0026 step 2) | Sign-in and session dependencies, tenant routes, signed webhook |
| [persistence/quotations.py](../../src/sales_platform/quotations/persistence/quotations.py), [channels.py](../../src/sales_platform/quotations/persistence/channels.py), [sessions.py](../../src/sales_platform/platform/persistence/sessions.py) | SQL stores, advisory run lock, job queuing in the same transaction |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0005_approvals`) | M4 tables, append-only triggers, column grants, narrow RLS policies |
| [scripts/simulated_channel.py](../../scripts/simulated_channel.py) | Play the manager's phone: inbox, enroll, decide |
| [portal/src/](../../portal/src/app/App.tsx) | React portal organised by feature (auth, tenant, quotations, enquiries, channels) and layer (model, api, services, store, hooks, ui, routes). See [portal architecture](portal-architecture.md). Tests live in [portal/tests/](../../portal/tests/architecture.test.ts), mirroring src |
| [tests/unit/conftest.py](../../tests/unit/conftest.py) | In-memory M4/M5 "database" and harness used by unit tests |

## Implemented M5 quotation PDFs and delivery
Entry points:
- the `approved` workflow step (queues delivery);
- the `deliver_quotation` job in the worker;
- PDF and delivery routes in the portal API and the enquiry API.

See [quotation delivery](../features/quotation-delivery.md) and [adding providers](adding-providers.md).

| Actual file | Responsibility |
|---|---|
| [domain/documents.py](../../src/sales_platform/quotations/domain/documents.py) | Delivery statuses, DocumentSource, recipient rule, document IDs/keys, document errors |
| [ports/documents.py](../../src/sales_platform/quotations/ports/documents.py) | QuotationRenderer, DeliveryStore, DocumentReader (sending moved to the outbox in phase 1) |
| [application/delivery.py](../../src/sales_platform/quotations/application/delivery.py) | QuotationDeliveryHandler: render once, compare-and-set, record honest outcomes |
| [application/documents.py](../../src/sales_platform/quotations/application/documents.py) | QuotationDocumentService: previews, verified approved files, customer view and downloads |
| [adapters/documents/pdf.py](../../src/sales_platform/quotations/adapters/documents/pdf.py) | ReportLab renderer: deterministic, escaped, refuses unsupported characters |
| [adapters/providers/api/](../../src/sales_platform/providers/api/__init__.py) | API provider (pull: makes the document available) |
| [api/pdf.py](../../src/sales_platform/quotations/api/pdf.py) | Download response headers, integrity-failure response |
| [persistence/documents.py](../../src/sales_platform/quotations/persistence/documents.py) | `queue_delivery` (outbox row + job), SqlDeliveryStore, SqlDocumentReader |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0006_documents`) | quotation_documents (append-only) and quotation_deliveries, RLS, column grants |
| portal `features/quotations` | `DocumentPanel`, `DeliveryList`, `deliveryService.ts`, `useQuotationDocuments.ts` |

## Implemented M6 customer acceptance
See [customer acceptance](../features/customer-acceptance.md).

| Actual file | Responsibility |
|---|---|
| [domain/acceptance.py](../../src/sales_platform/quotations/domain/acceptance.py) | Customer statuses, outcomes, price comparison, validity, answer fingerprints |
| [ports/acceptance.py](../../src/sales_platform/quotations/ports/acceptance.py) | ResponseStore (offers, atomic recording under a lock) |
| [application/acceptance.py](../../src/sales_platform/quotations/application/acceptance.py) | QuotationResponseService (respond/withdraw/expire), expiry and notice job handlers |
| [persistence/acceptance.py](../../src/sales_platform/quotations/persistence/acceptance.py) | SqlResponseStore: row-locked recording, revised version, resume/notice jobs |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0007_acceptance`) | Validity setting and column, quotation_responses (append-only), new statuses |
| portal `features/quotations` | `CustomerPanel`, `customerService.ts`, `useCustomerAnswer.ts`, list filters |

## Phase 1 connector framework (merged, ADR 0018)
See the [phase 1 plan](../plan/platform/implementation/phase-1-foundations.md).

| Actual file | Responsibility |
|---|---|
| [domain/connections.py](../../src/sales_platform/integrations/domain/connections.py) | Purpose, ConnectionStatus, DeliveryMode, Capabilities, Connection (with `legacy_key`), name and purpose checks |
| [domain/messages.py](../../src/sales_platform/integrations/domain/messages.py) | Outbox MessageStatus, TemplateKey, OutboundMessage, `render_template` (the fixed approval-request and notice wording moved here word for word) |
| [domain/intake.py](../../src/sales_platform/enquiries/domain/intake.py) | Also the inbound event types: CustomerMessage, ManagerAction, DeliveryStatusUpdate, ConsentChange, `event_type` |
| [ports/providers.py](../../src/sales_platform/integrations/ports/providers.py) | The Provider contract, InboundRequest, RenderedMessage, SendReceipt, provider errors, ProviderEnvironment/Factory |
| [ports/connections.py](../../src/sales_platform/integrations/ports/connections.py), [ports/secrets.py](../../src/sales_platform/platform/ports/secrets.py), [ports/messages.py](../../src/sales_platform/integrations/ports/messages.py) | ConnectionStore and HookLookup; SecretStore; MessageOutbox and SendHook |
| [adapters/secrets/](../../src/sales_platform/platform/adapters/secrets/local.py) | `local` (AES-256-GCM files bound to tenant and reference) and `none` secret stores, registered as entry points |
| [application/plugins.py](../../src/sales_platform/platform/application/plugins.py) | Load a pluggable part by entry point name, with clear errors |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0008_connections`) | connections, connection_events, inbox/binding/enrollment `connection_ref`, lookup policy, worker grants, backfill and downgrade |
| [persistence/connections.py](../../src/sales_platform/integrations/persistence/connections.py) | SqlConnectionStore, SqlHookLookup, owner-only `ensure_connection`/`set_status` with audit events; API keys and the seed use them |
| [application/providers.py](../../src/sales_platform/integrations/application/providers.py) | ProviderRegistry: loads `sales_platform.providers` entry points, checks keys, skips dev-only providers in production |
| [application/inbound.py](../../src/sales_platform/integrations/application/inbound.py) | InboundService (token, size, verify-before-parse, store once), ManagerActionRoute, ProcessInboundHandler (`process_inbound` job), `routes_for` |
| [ports/inbound.py](../../src/sales_platform/integrations/ports/inbound.py), [persistence/inbound.py](../../src/sales_platform/integrations/persistence/inbound.py) | InboundStore and SqlInboundStore: inbox rows with dedupe, the job in the same transaction, outcome recording |
| [api/hooks.py](../../src/sales_platform/integrations/api/hooks.py) | `POST /api/v1/hooks/{connection_id}/{webhook_token}` |
| api/channels.py (removed in ADR 0026 step 2) | Deprecated simulated route, now finding the business's connection and routing through ManagerActionRoute |
| [adapters/providers/api/](../../src/sales_platform/providers/api/__init__.py) | Provider `api` (pull: send = available) |
| [adapters/providers/simulated/](../../src/sales_platform/providers/simulated/provider.py) | Provider `simulated` (dev only): outbox files, per-connection HMAC signing, manager actions, demo catalogue reader; `signing.py` (incl. `hook_token`), `outbox.py` |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0009_outbound_messages`) | outbound_messages (forced RLS, column grants), `quotation_deliveries.outbound_message_id`, backfill and downgrade |
| [persistence/messages.py](../../src/sales_platform/integrations/persistence/messages.py) | SqlMessageOutbox: idempotent enqueue with the send job, compare-and-set status mirrored onto a linked delivery, provider receipts |
| [application/messaging.py](../../src/sales_platform/integrations/application/messaging.py) | OutboundService (render a template, store the message) and SendMessageHandler (`send_message` job) |
| [application/delivery.py](../../src/sales_platform/quotations/application/delivery.py) | `deliver_quotation`: prepare the PDF, queue the outbox message; QuotationDocumentHook attaches the verified PDF at send time |
| [application/notifications.py](../../src/sales_platform/quotations/application/notifications.py) | `notify_approvers` queues approval requests; ApprovalRequestHook creates the single-use token at send time |
| [composition.py](../../src/sales_platform/composition.py) | Composition root shared by the API, worker and tests: secret store (built lazily), provider registry, mailer, `build_core` (the stores and services the API and the worker share, built once per process: connections, product source, pricing, customer answers, approvals, channel actions, connection lifecycle, orders, outbox...), `build_job_handlers` for every job kind (each wrapped so a suspended business's jobs wait) |

## Phase 2 accounts and onboarding (ADR 0019)
See the [phase 2 plan](../plan/platform/implementation/phase-2-accounts.md) and [accounts and onboarding](../features/accounts-and-onboarding.md).

| Actual file | Responsibility |
|---|---|
| [domain/accounts.py](../../src/sales_platform/accounts/domain/accounts.py) | Email normalisation, password rules, token lifetimes, rate rules, invitations, last-administrator rule, setup checklist and Go-live check |
| [domain/tenancy.py](../../src/sales_platform/platform/domain/tenancy.py) | Also TenantStatus and one function per status gate; `members:manage`; PEOPLE_ROLES |
| [domain/messages.py](../../src/sales_platform/integrations/domain/messages.py) | Also the five account email templates (single-line subjects) |
| [domain/documents.py](../../src/sales_platform/quotations/domain/documents.py) | Also SellerDetails and `printable_text` (shared by the renderer and the profile form) |
| [ports/accounts.py](../../src/sales_platform/accounts/ports/accounts.py) | UserStore, BusinessStore, MemberStore, InvitationStore, ApiKeyAdminStore, AccountEmailStore, Mailer, RateLimiter, LogoNormalizer, PasswordHasher |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0010_accounts`) | All phase 2 tables, policies, grants, the status trigger and the four SECURITY DEFINER functions, with backfill and downgrade |
| [persistence/accounts.py](../../src/sales_platform/accounts/persistence/accounts.py) | SQL stores for people, businesses, members, invitations and account emails; maps the functions' SQLSTATEs to port errors |
| [persistence/rate_limits.py](../../src/sales_platform/platform/persistence/rate_limits.py) | SqlRateLimiter: fixed windows in one atomic upsert, hashed keys |
| [persistence/api_keys.py](../../src/sales_platform/accounts/persistence/api_keys.py) | Also SqlApiKeyAdminStore (portal keys with their connection and audit) |
| [adapters/accounts/password.py](../../src/sales_platform/accounts/adapters/accounts/password.py) | Argon2id hasher with bounded concurrency; the bundled common-password list (`common_passwords.txt`, MIT) |
| [adapters/accounts/mail/console.py](../../src/sales_platform/accounts/adapters/accounts/mail/console.py) | Development mailer (entry point `console`): one file per email |
| [adapters/documents/logo.py](../../src/sales_platform/accounts/adapters/documents/logo.py) | Logo checks and re-encoding (Pillow) |
| [application/accounts.py](../../src/sales_platform/accounts/application/accounts.py) | Signup, Login, Profile, Business, Member, Invitation and ApiKeyAdmin services; AccountEmailHandler (`send_account_email`) |
| [application/jobs.py](../../src/sales_platform/platform/application/jobs.py) | Also PauseWhileSuspended |
| [api/accounts.py](../../src/sales_platform/accounts/api/accounts.py), [api/business.py](../../src/sales_platform/accounts/api/business.py) | Account routes and per-business settings routes; one error mapping (`guarded`) |
| portal `features/account`, `features/business` | Sign-up, login, links, profile, invitations; create a business, setup and Go live, settings, people, API keys |

## Phase 3 product catalogue (ADR 0020)
See the [phase 3 plan](../plan/platform/implementation/phase-3-catalogue.md) and [product catalogue](../features/product-catalogue.md).

| Actual file | Responsibility |
|---|---|
| [domain/catalogue_search.py](../../src/sales_platform/catalogue/domain/catalogue_search.py) | The one word-matching rule for product search (moved out of the simulated ERP): words, query and product terms, best matches only |
| [domain/catalogue_admin.py](../../src/sales_platform/catalogue/domain/catalogue_admin.py) | Editing rules (SKU, units, decimals, price start, stock "as of"), price and stock selection, staleness, change lists; product, category, price and stock records |
| [domain/tenancy.py](../../src/sales_platform/platform/domain/tenancy.py) | Also `catalogue:view` and `catalogue:manage` |
| [domain/connections.py](../../src/sales_platform/integrations/domain/connections.py) | Also the `catalogue_editable` capability |
| [ports/catalogue_admin.py](../../src/sales_platform/catalogue/ports/catalogue_admin.py) | CatalogueReadStore (for LocalCatalogue) and CatalogueStore (for the screens), queries, pages, counts, errors |
| [ports/providers.py](../../src/sales_platform/integrations/ports/providers.py) | Also `ProviderEnvironment.catalogue_store` |
| [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0011_catalogue`) | Catalogue tables, indexes, RLS, triggers, grants; `create_business` with the default source; backfill and downgrade |
| [persistence/catalogue.py](../../src/sales_platform/catalogue/persistence/catalogue.py) | SqlCatalogueStore: both ports, best-match search in one query, row locks for revisions, replays by request ID, change log |
| [persistence/seed.py](../../src/sales_platform/seed.py) | Also the Demo Local Catalogue business and `seed_local_catalogue` |
| [adapters/catalogue/local.py](../../src/sales_platform/catalogue/adapters/catalogue/local.py) | LocalCatalogue: our list as an `ProductSource` (evidence rules of Q10) |
| [tests/support/catalogue_store.py](../../tests/support/catalogue_store.py) | InMemoryCatalogueStore, a test double (moved out of `src/` in ADR 0026 step 2) |
| [adapters/providers/local_catalogue/](../../src/sales_platform/providers/local_catalogue/__init__.py) | Provider `local_catalogue` ("Our product list"), entry point in `pyproject.toml` |
| [application/catalogue_admin.py](../../src/sales_platform/catalogue/application/catalogue_admin.py) | ProductService: permissions, read-only sources, currency, request IDs, summary, the checklist's product step |
| [composition.py](../../src/sales_platform/composition.py) | Also `default_catalogue_source` and the store given to providers |
| [api/catalogue.py](../../src/sales_platform/catalogue/api/catalogue.py) | `/api/v1/tenants/{t}/catalogue/...` routes and their error codes |
| portal `features/catalogue` | Product list, product page (details, prices, stock, archive, changes), categories |

## Phase 4 customers, conversations and website chat (ADR 0021)
| Area | Files |
|---|---|
| Domain | `domain/contacts.py` (consent words, claimed details, masking), `domain/conversations.py` (`follow_up_target`), `domain/chat.py` (limits, handle, allowed websites), `domain/requirements.py` (`quantity_quote`), `domain/messages.py` (customer templates) |
| Routing | `application/conversations.py` (`IntentRouter`), `persistence/conversations.py` (conversation lock, open enquiries, thread rows, `SqlCustomerTargets`), `persistence/intake.py` (`record` with the router, receipts through messages) |
| Rounds | `ports/interpretation.py` (`EnquiryTranscript`), `persistence/workflow.py` (transcript, re-read check, `readings_today`), `workflows/enquiry.py` (rounds, cap, questions to the customer), `adapters/interpreters/*` |
| Customer messages | `application/customer_messages.py` (`CustomerMessenger`, `CustomerConsentHook`), `application/acceptance.py` (`CUSTOMER_TEMPLATE_FOR`, `respond_by_reference`), `application/delivery.py` (quotation ready), `application/inbound.py` (`ConsentRoute`) |
| Answers by reference | `application/customer_actions.py`, `persistence/customer_actions.py`, `ports/customer_actions.py` |
| Website chat | `adapters/providers/web_chat`, `application/chat.py`, `persistence/chat.py`, `api/public_chat.py`, `application/connections.py` + `persistence/connections.SqlHostedChatAdmin` |
| Staff | `application/contacts.py`, `persistence/contacts.py`, `api/conversations.py`; portal `features/conversations`, `features/connections/ui/components/HostedChatCard.tsx` |
| Widget | `portal/src/widget/` (`api/chatClient.ts`, `state/session.ts`, `ui/chatView.ts`, `main.ts`), `portal/vite.widget.config.ts` |
| Migration | [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0012_conversations`) |

## Phase 5 one-click tools, platform webhooks and approval by message (ADR 0022)
| Area | Files |
|---|---|
| Contract | `ports/providers.py` (lifecycle, platform webhooks, `ProviderDefaults`, `PendingAction`, `ProvisionResult`), `domain/connections.py` (capabilities `self_service`, `platform_webhooks`, `attachments_by_url`; `pending_action`), `domain/intake.py` (`ManagerReply`, `CustomerAction`, `ConnectionUpdate`, `InboundFile`), `ports/secrets.py` (platform namespace) |
| Lifecycle | `application/lifecycle.py` (Enable, resume, setup events, poll, test, disable), `persistence/provider_state.py` (lifecycle store, routes), `api/connections.py` (enable, actions, provider page, test, disable) |
| Inbound | `application/inbound.py` (platform route, handshakes, staff, customer action, setup and reply routes), `application/customer_inbox.py` (webhook customer messages, limits, files), `api/hooks.py`, `persistence/conversations.SqlStaffMessages`, `persistence/workflow.py` (staff text as context) |
| Approval by message | `domain/manager_replies.py` (guards, YES/NO), `application/manager_replies.py` (`ManagerReplyService`, `ApprovalPromptHook`), `ports/manager_replies.py`, `persistence/manager_replies.py`, `ports/reply_classification.py`, `adapters/interpreters/reply_classifier.py`, `adapters/interpreters/factory.py` (`build_chat_model`, `build_reply_classifier`), `application/channel_actions.py` (`check`, `decide_reference`), `domain/messages.py` (manager templates) |
| Signed link | `application/approval_links.py`, `api/approval_links.py`; `application/notifications.py` (the link in approval requests); portal `features/approvals` |
| Outbox extras | `application/media.py`, `persistence/media.py`, `api/media.py` (public PDF links); `application/resend.py`, `persistence/messages.resend`, `api/conversations.py` (Resend); `application/messaging.py` (routes from receipts, purpose, link) |
| Telegram | `adapters/providers/telegram/` (`provider.py`, `client.py`, `mapping.py`, `config.py`) |
| Email helpers | `adapters/providers/email_common/__init__.py` (unused until an email package) |
| Operators | `ops.py` (`platform-config`, `platform-setup`) |
| Portal | `features/connections` (`OneClickToolsCard`, `PendingActionPanel`, `ConnectionList` test/disable, `store/connectionSetup.ts`), `features/conversations` (Send again), `features/approvals` |
| Migration | [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0013_provider_state`) |

## Phases 6 and 7: ERPNext product source and orders (ADR 0023, 0024)
| Area | Files |
|---|---|
| Contract | `domain/connections.py` (`CatalogueCapabilities`, `OrderCapabilities`, `StockSemantics`, purpose `orders`), `ports/providers.py` (`fetch_catalogue`, `order_writer`), `ports/erp.py` (`OrderWriter`, `SalesOrderRequest`, `ErpOrder`, `ErpOrderState`) |
| ERPNext package | `adapters/providers/erpnext/` (`provider.py` form and checks, `config.py` form and settings, `client.py` Frappe REST and error mapping, `catalogue.py` hybrid reader and feed, `orders.py` writer) |
| Sync | `domain/catalogue_sync.py` (feed, unit map, stock sums, runs), `ports/catalogue_sync.py`, `persistence/catalogue_sync.py`, `application/catalogue_sync.py` (handler, "Sync now"), `api/catalogue.py` (`/sync`) |
| One source per business | `application/lifecycle.py` (`EXCLUSIVE_PURPOSES`, first copy), `persistence/provider_state.py` (`_replace_others`, `_restore_replaced`), `ports/connections.ConnectionChange` |
| Orders | `domain/orders.py`, `ports/orders.py`, `persistence/orders.py`, `application/orders.py` (service, five job handlers, `OrderStatusRoute`, `OrderDestinations`), `api/orders.py`, `api/enquiries.py` (`/order`), `domain/messages.py` (order templates), `persistence/acceptance.py` (queues `create_order`) |
| Track my order | `domain/orders.order_status_command`, `domain/intake.OrderStatusRequest`, `application/customer_inbox.py`, `application/chat.py`, `application/inbound.routes_for(orders=...)` |
| Test doubles | `adapters/erp/simulated_orders.py` (fault modes) |
| Sandbox | `infra/erpnext/compose.yaml`, `scripts/seed_erpnext_sandbox.py` |
| Portal | `features/orders` (list, detail, `QuotationOrderPanel`), `features/catalogue/ui/components/CatalogueSyncPanel.tsx`, `features/connections/services/connectionService.formDefaults` |
| Migrations | [0001_baseline](../../migrations/versions/0001_baseline.py) (formerly `0014_catalogue_sync`, `0015_orders`) |

## Planned modules

| Proposed location | Responsibility |
|---|---|
| src/sales_platform/domain/ | Business models, money, pricing and policy rules |
| src/sales_platform/application/ | Use cases, transaction boundaries and authorization |
| src/sales_platform/application/negotiation/ (later) | Counteroffer lifecycle reusing existing quotation/approval services; deferred |
| src/sales_platform/application/manager_commands/ (later) | Verified stock enquiries and scoped change requests through existing services; deferred |
| src/sales_platform/workflows/ | LangGraph state, nodes, routing and versioning |
| src/sales_platform/ports/ | Typed interfaces for channels, ERP, model and storage |
| src/sales_platform/adapters/ | Provider-specific implementations and simulated ERP |
| src/sales_platform/persistence/ | Repositories, tables, tenant context and checkpoint access |
| src/sales_platform/workers/ | Jobs, scheduler, outbox and reconciliation |
| src/sales_platform/observability/ | Logging, tracing and metrics configuration |
| migrations/ | Alembic migrations and data transition notes |
| tests/ | Unit, integration, contract, security, workflow and evaluation suites |
| infra/ | Local Compose and deployment configuration |
| examples/ | Synthetic requests and provider examples |

Domain code must not import FastAPI routes, provider SDKs or graph orchestration.
Adapters implement ports. Workflow/application services depend on ports and domain rules.
See [module boundaries](../architecture/module-boundaries.md) for the expected change footprint of a new provider. Do not create the deferred negotiation module during foundation work.
When implemented, replace proposed descriptions with links to real paths/symbols, entry commands, and actual dependency flow.
No run/setup command is documented as working until it has been executed successfully.

## Friendly portal design (ADR 0025)
| Area | Files |
|---|---|
| Menu | `portal/src/app/layout/navigation.ts` (items, icons, permissions), `SideNav.tsx` (sidebar and phone bottom bar with More), `TopBar.tsx` (brand, business, person with plain role names, Sign out) |
| Home | `portal/src/features/home/` (`services/homeCards.ts` card rules, `hooks/useHomeSummary.ts`, `ui/pages/HomePage.tsx`) at `#/home` |
| Shared UI | `portal/src/shared/ui/PageHeader.tsx`, `StatusBadge.tsx` (plain labels, icons, `ToneBadge`) |
| Styles | `portal/src/styles/global.css` |
| Settings | `portal/src/app/layout/SettingsLayout.tsx`, `settingsNavigation.ts`; sections from `features/business` (`businessSettingsRoutes`) and `features/connections` (`connectionSettingsRoutes`: `IntegrationsPage`, `ToolGroupCard`, `services/comingSoon.ts`); `LinkedAccountsSection` on the profile page |
