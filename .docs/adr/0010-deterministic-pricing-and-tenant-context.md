# 0010: Deterministic pricing and trusted tenant context
Date: 2026-10-22
Status: implemented in M1; verified locally and in CI ([CI run 39738532222](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39738532222))

## Context
M1 needs commercial arithmetic that is exact and explainable, tenant context that cannot be forged from request data, and an ERP contract that real adapters can later satisfy.

## Decision
- Money is `Decimal` only. Floats are rejected. Multiplication traps precision loss (`decimal.Inexact`) instead of silently rounding.
- Each line total is rounded to the currency's minor units using the tenant's named rounding mode (`half_up` or `half_even`). Tax is one tenant-level rate applied once to the rounded subtotal and then rounded. One currency per quotation; no exchange rates.
- A missing, stale, not-yet-effective, wrong-unit or wrong-currency price blocks the whole quote with listed issues. No price is ever assumed or set to zero.
- Units must match exactly; no conversion is guessed. Quantity rules come from the ERP product: positive, at least the minimum, and a multiple of the step.
- Stock never blocks calculation, but each line reports one of five distinct statuses: available, insufficient, unavailable, unknown or stale.
- `TenantContext` is built only by `TenantContextResolver` from an authenticated subject plus stored membership. Roles map to permissions in code; administrators do not approve unless also granted the approver role.
- Repositories set a transaction-local tenant context, filter by tenant explicitly, and rely on forced RLS as a second guard. The runtime role has SELECT only on memberships and policies.
- ERP access goes through the `ProductSource` protocol: unknown SKUs are omitted, results carry source and timezone-aware `fetched_at`, and failures raise normalized `ErpError` codes. A shared contract suite applies to every adapter.

## Alternatives
Float arithmetic; rounding only the grand total; tax per line; treating missing prices or stock as zero; guessing unit conversions; trusting a tenant ID from the request; letting the runtime role manage memberships; exception-free result objects for ERP failures.

## Consequences
Per-line rounding and subtotal-level tax are synthetic defaults. A pilot business may need per-line tax, multiple tax rates, discounts or customer-specific price lists, which need a policy version change and tests. Discounts are not implemented: negotiated changes are human-approved revisions (M4/M11). Membership and policy administration have no API yet, so they are seeded by the owner role until admin features exist. Since M4 the subject comes from a portal session (dev login now, OIDC later; ADR 0013) or an API key.

## Verification
Rule, service, contract, architecture and two-tenant PostgreSQL tests are listed in [testing](../development/testing.md); results are in the [handoff](../handoff.md).
