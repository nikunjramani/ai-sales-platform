# Architecture decision records
Accepted denotes intended design, not implemented code. Date: 2026-10-22.

- [0001: Channel-independent core and adapters](0001-adapter-boundaries.md) — accepted
- [0002: Tenant isolation from the foundation](0002-tenant-isolation.md) — accepted
- [0003: Existing ERP remains authoritative](0003-erp-authority.md) — accepted; amended by 0020 (our own catalogue when there is no ERP)
- [0004: Persisted graph with durable jobs and outbox](0004-durable-execution.md) — accepted
- [0005: Version-bound internal approval and customer acceptance](0005-versioned-approval.md) — superseded by 0007
- [0006: Versioned documentation and portable agent instructions](0006-portable-documentation.md) — accepted
- [0007: Channel-independent manager approval](0007-channel-independent-approval.md) — accepted; core implemented in M4 (portal + simulated channel, ADR 0013); real providers M7 (email approval deferred, ADR 0015)
- [0008: Deferred negotiation module](0008-negotiation-extension.md) — accepted; manager approval mandatory; implementation deferred
- [0009: Foundation tooling and database roles](0009-foundation-tooling.md) — implemented and verified (local and CI)
- [0010: Deterministic pricing and trusted tenant context](0010-deterministic-pricing-and-tenant-context.md) — implemented in M1; verified locally and in CI
- [0011: Durable intake, PostgreSQL job queue, attachment quarantine and API keys](0011-durable-intake-and-jobs.md) — implemented in M2; verified locally and in CI
- [0012: Provider-independent enquiry extraction with LangGraph and verbatim checks](0012-provider-independent-extraction.md) — implemented in M3; verified locally and in CI
- [0013: Portal identity, immutable versions and the shared approval core](0013-portal-identity-and-approval-core.md) — implemented in M4; verified locally and in CI
- [0014: Portal structure by feature and layer, with TanStack Query and Zustand](0014-portal-structure-and-state.md) — implemented; verified locally and in CI
- [0015: Approved quotation PDFs and an honest delivery outbox; email approval deferred](0015-quotation-documents-and-delivery.md) — implemented in M5; verified locally and in CI
- [0016: Customer acceptance through the API, with validity, price re-check and automatic revision](0016-customer-acceptance.md) — implemented in M6 (user decisions 2026-10-23); verified locally and in CI
- [0017: A generic AI enquiry reader (first model: Gemini free tier), and category-based "not found" replies](0017-gemini-reader-and-category-replies.md) — user decisions 2026-10-23; implemented, live Gemini evaluation pending the user's key
- [0018: Connector framework: providers, connections, secrets, one webhook route, one outbox](0018-connector-framework.md) — phase 1, user decisions 2026-10-27; implemented and merged (PR #6)
- [0019: Accounts, sign-up and business onboarding](0019-accounts-and-onboarding.md) — phase 2, user decisions 2026-10-27; implemented and merged (PR #7)
- [0020: Our own product catalogue as a product source](0020-local-catalogue.md) — phase 3, user decisions 2026-10-28; implemented on `feat/phase-3-catalogue`, verified locally
- [0021: Customers, conversations and website chat]

(0021-customers-conversations-and-website-chat.md) — phase 4, user decisions 2026-10-28; implemented and merged (PR #9), verified locally and in a live run with Gemini
- [0022: One-click providers, platform webhooks and approval by message](0022-one-click-providers-and-approval-by-message.md) — phase 5, user decisions 2026-10-29; implemented and merged (PR #10), not live-verified (no accounts)
- [0023: ERPNext as the first real product source: a synced copy for search, live prices and stock](0023-erpnext-product-source.md) — phase 6, user decisions 2026-10-29 (rest delegated to Claude); implemented on `feat/phase-6-7-erp-and-orders`, verified against a local ERPNext sandbox
- [0024: Orders: one per accepted quotation, written once to the ERP with intent before call](0024-orders.md) — phase 7 (M6b), user answers Q1-Q4 2026-10-29, Q5-Q16 delegated to Claude; implemented on `feat/phase-6-7-erp-and-orders`
- [0025: A friendly portal design: plain words, a simple menu, a Home page and page hints](0025-friendly-portal-design.md) — user decisions 2026-10-30; implemented on `feat/portal-friendly-design`
- [0026: Backend by business module, one shared wiring, and the `sales_platform` name](0026-backend-business-modules.md) — proposed 2026-11-02 (plan only, no code yet)

New decisions get sequential IDs. Preserve history and explicitly link superseding records.
