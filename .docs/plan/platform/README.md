# Platform plan: self-service businesses, any communication tool, any product source
**Status:** proposal for the user's review (2026-10-23). Nothing here is built or decided until the user confirms it. The open questions are in [07-decisions-needed.md](07-decisions-needed.md).

## The idea (user, 2026-10-23)
- **A manager creates an account and a business in the web portal.** The manager then decides how customers can reach the business and how they approve quotations.
- **Tools are linked from the portal.** WhatsApp, Telegram, Instagram, email, a chat on the business's own website, or the portal itself. One or many, chosen separately for customer enquiries and for approvals. More tools will be added later, so everything must be **generic**: a new tool is a new adapter package, not changes across the code.
- **Products, prices and stock must come from somewhere.** This is the hardest question, because each business keeps them differently.
- **Today the product handles quotations only.** Later: order placement, progress tracking, order status updates, payment status and possibly **our own stock/inventory system**. The code must be shaped now so these fit in later without rewrites.

## Documents
| # | Document | What it covers |
|---|---|---|
| 1 | [Current state](01-current-state.md) | What exists today, what is hardcoded, what is missing (from a full code review) |
| 2 | [Accounts and onboarding](02-accounts-and-onboarding.md) | Sign-up, creating a business, members and roles, settings, real login |
| 3 | [Connectors](03-connectors.md) | One generic model for every tool: providers, connections, purposes, inbound webhooks, outbound messages, manager approval, website chat |
| 4 | [Customers and conversations](04-customers-and-conversations.md) | Customer identities across tools, conversations, follow-up messages, sending questions and quotations back |
| 5 | [Product data sources](05-product-data.md) | How catalogue, prices and stock reach us: ERP, spreadsheet, manual entry, our own inventory later |
| 6 | [Future commerce](06-future-commerce.md) | Orders, tracking, status updates, payments, own inventory: the extension points to build in now |
| 7 | [Decisions needed](07-decisions-needed.md) | Every open question, with options and a recommendation |
| 8 | [Phases](08-phases.md) | The order of work, what each phase delivers and how it is verified |
| — | **Detailed implementation plan** (git history) | Step-by-step work per phase: files, tables, APIs, screens, tests, docs, risks |
| 9 | [One-click integrations](09-one-click-integrations.md) | The business presses **Enable** and we create every account the tool needs (per business); what each platform still requires; company setup tasks |

## Principles (from AGENTS.md and earlier ADRs, still binding)
- **Generic and pluggable.** Providers are chosen by configuration or by adding a package, never by `if provider == "x"` branches or provider-named settings (AGENTS.md, ADR 0017).
- **Tenant isolation everywhere,** including connections, secrets, webhooks, jobs and files (ADR 0002).
- **Commercial facts are never invented.** Prices and stock come from the business's chosen source; AI only reads text (ADR 0003, 0010, 0017).
- **Manager approval stays mandatory and channel-independent:** one approval service, whatever the tool (ADR 0007, 0013).
- **Honest outcomes for remote calls:** a send or ERP write whose result is unknown is recorded as `unknown`, never retried blindly (ADR 0015).
- **Ask, don't assume.** Business rules come from the user (AGENTS.md).

## In one picture
```
                         Web portal (manager)
     sign up · business settings · members · connections · catalogue · approvals
                                   │
   ┌───────────────────────────────┴────────────────────────────────┐
   │                      Connector framework                        │
   │  provider registry (installed adapter packages)                 │
   │  connections per business: provider + purposes + encrypted      │
   │  secrets + webhook address + status                            │
   └───────┬──────────────────────┬───────────────────────┬──────────┘
   inbound events           outbound messages        catalogue source
   (customer message,       (questions, quotation    (ERP / spreadsheet /
    manager action,          PDF, approval request,   manual / own
    delivery status)         notices, order status)   inventory later)
           │                      │                        │
   ┌───────┴──────────────────────┴────────────────────────┴──────────┐
   │ Core (unchanged rules): conversations → enquiry workflow (AI read,│
   │ match, questions) → pricing → manager approval → PDF → acceptance │
   │ → [later] orders → status → payments                             │
   └───────────────────────────────────────────────────────────────────┘
```
