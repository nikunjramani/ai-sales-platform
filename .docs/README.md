# Documentation index
Status 2026-10-27: M0-M6 and platform phase 1 are complete (local and CI); phase 2 (accounts) is implemented on `feat/phase-2-accounts`. Business workflow documents describe planned behaviour. See the handoff for verified implementation status.

## Planning
- [Product scope and acceptance criteria](plan/product.md)
- [Milestones from setup to release](plan/roadmap.md)
- [Technology and access requirements](plan/stack-and-access.md)
- [Open decisions and risks](plan/open-decisions.md)
- [Future quotation negotiation](plan/negotiation.md)
- [Future manager enquiries and commercial commands](plan/manager-operations.md)
- [Pre-implementation plan review](plan/readiness-review.md)

## Architecture
- [System boundaries and components](architecture/system.md)
- [System flowcharts](architecture/flowcharts.md)
- [Channel and business-system contracts](architecture/integrations.md)
- [Data ownership and tenant isolation](architecture/data-model.md)
- [Execution flow and recovery](architecture/execution-flow.md)
- [Approval and customer acceptance](architecture/approvals.md)
- [Module boundaries and extension rules](architecture/module-boundaries.md)
- [Architecture decision records](adr/README.md)

## Engineering and operations
- [Local setup walkthrough](development/setup.md)
- [Configuration, roles and API contracts](development/configuration.md)
- [Code map (implemented and planned)](development/code-map.md)
- [Portal architecture: features, layers, state management](development/portal-architecture.md)
- [Quotation pricing and tenant context (M1)](features/quotation-pricing.md)
- [Enquiry intake, job worker and attachment quarantine (M2)](features/enquiry-intake.md)
- [Enquiry extraction workflow with swappable models (M3)](features/enquiry-extraction.md)
- [Quotation drafting, approval and the portal (M4)](features/quotation-approval.md)
- [Quotation PDFs and delivery (M5)](features/quotation-delivery.md)
- [Customer acceptance (M6)](features/customer-acceptance.md)
- [Adding a provider (email, Telegram, ERP...)](development/adding-providers.md)
- [Connections (phases 1, 4 and 5)](features/connections.md)
- [Telegram (phase 5)](features/telegram.md)
- [Orders (phase 7)](features/orders.md)
- [Approval by message (phase 5)](features/approval-by-message.md)
- [Accounts and onboarding (phase 2)](features/accounts-and-onboarding.md)
- [Documentation policy and templates](development/documentation-policy.md)
- [Testing and evaluation](development/testing.md)
- [Security and privacy](operations/security.md)
- [Provider setup (company level)](operations/provider-setup.md)
- [Release and recovery runbook](operations/release.md)
- [Learning guide](learning/guide.md)
- [Source references](references.md)
- [Portable agent context](agent-context.md)
- [Current state and next steps](handoff.md)

Read product -> system -> execution flow -> roadmap for a first introduction.
Read agent context -> handoff -> task-specific documents for a returning session.
