# 1. Current state (code review, 2026-10-23)
The core business flow works end to end through APIs (M0-M6): enquiry → AI/rules reading → product matching → questions → pricing → manager approval → PDF → customer accept/decline. Everything *around* that core is fixed for two demo businesses. Paths are relative to `src/sales_platform/` unless stated.

## What we can reuse as-is
| Area | Reusable piece | Where |
|---|---|---|
| Tenant isolation | Forced row-level security, `tenant_transaction`, `TenantContext` + roles/permissions (`tenant:manage` already exists) | `persistence/tenancy.py`, `domain/tenancy.py`, migrations 0001-0007 |
| Login plumbing | `VerifiedIdentity` port, sessions (hashed token, 8 h), CSRF, provider-independent login (ADR 0013) | `ports/identity.py`, `application/sessions.py`, `api/auth.py` |
| Inbound messages | `InboundEnvelope` (channel, connection, provider event id, verified sender) and `inbox_events` de-duplication | `domain/intake.py`, `persistence/intake.py` |
| Manager approval | One approval service; channel enrollment codes (10 min), bindings, single-use action references (24 h), `ChannelActionService` (works on plain strings, not provider names) | `application/approvals.py`, `application/channel_actions.py` |
| Outbound | Delivery outbox with honest outcomes (`pending → sending → sent/available/failed/unknown`) | `application/delivery.py`, `persistence/documents.py` |
| Jobs | Durable PostgreSQL job queue with retries and leases | `application/jobs.py`, `worker.py` |
| AI reader | Generic: any LangChain model from settings (ADR 0017) | `adapters/interpreters/factory.py` |
| Product data | `ErpReader` port (search, categories, products, prices, stock) + contract tests | `ports/erp.py`, `tests/unit/ports/test_erp_contract.py` |
| Portal | Feature folders with enforced layers; generic `BindingList` | `portal/src/features/*` |

## What is hardcoded
| Hardcoded | Where | Effect |
|---|---|---|
| Businesses and members exist only from the seed script | `persistence/seed.py` | No sign-up, no "create business", no invitations; the runtime database role cannot even insert tenants or memberships |
| Login is the dev login (no password, seeded people only) | `adapters/identity/dev.py` | Refused in production; no real identity provider yet |
| Customer channel enum has one value, `API` | `domain/intake.py:23` | Another channel needs code edits in several places |
| Manager channel enum has one value, `SIMULATED` | `domain/quotations.py:69` | The portal can only link the simulated phone (`EnrollmentCard.tsx` sends `"simulated"`) |
| Adapter maps are fixed in the worker | `worker.py` (notifiers, delivery channels) | Adding a tool means editing the worker |
| One global secret for the simulated webhook; tenant taken from the request body | `api/channels.py`, `config.py` | Real providers must find the business from a verified connection, never from the message |
| No connection/integration table, no stored credentials | — | `connection_id` is free text that points to nothing |
| ERP fixed to the in-memory simulated one with two demo catalogues | `worker.py:69`, `api/app.py:92` | A new business has no products, prices or stock; its jobs retry and die |
| No customer/contact entity; follow-ups not linked | `enquiries.customer_reference` (free text) | Each message is a new enquiry ("10 meter?" problem) |
| Clarification questions are stored, never sent | `workflows/enquiry.py` (`clarify`) | Customers only see them by polling the API |
| Customer answers only through the API key | `api/enquiries.py`, ADR 0016 | No acceptance from chat, email or a link |
| The word "channel" means 3 different things | enquiry channel, delivery channel, approval channel | Confusing; the new design gives one name per concept |

## Planned in earlier documents but never built
- **Connection** and **contact identity** entities (`.docs/architecture/data-model.md:69-72`).
- Richer envelope with conversation ID, reply-to and event type, and a provider **capabilities** model (`.docs/architecture/integrations.md:75-100`).
- Separate tenant settings for customer channels and manager channels (ADR 0007, `.docs/architecture/approvals.md:22`).
- Orders after acceptance: reservation, idempotent ERP order, reconciliation (roadmap M6b).
- Onboarding sequence: create tenant → admins → connect channels and ERP → map fields → policies → test → activate (`.docs/plan/product.md:43-44`).

## Not covered anywhere yet
- Businesses **without** an ERP: spreadsheet upload, manual product entry, our own inventory. ADR 0003 explicitly said "no replacement inventory database", so this needs the user's decision.
- Order progress tracking, order status updates to customers, payment status, invoices.
- Consent and opt-out for messaging customers.
- A chat widget for the business's own website.
