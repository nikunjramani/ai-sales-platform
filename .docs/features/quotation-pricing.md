# Quotation pricing and tenant context (M1)
**Status and milestone:** implemented in M1 and verified locally against PostgreSQL. No HTTP route exposes it yet; authenticated API access arrives with identity in M4.

**Business purpose:** calculate a draft quotation from live ERP facts with exact, explainable arithmetic, for the right business, by a person who is allowed to do it.

## Terms in simple language
- **Tenant context**: a trusted note saying "this person, acting for this business, with these roles".
- **Decimal**: exact base-10 numbers. `0.1 + 0.2` is exactly `0.3`; with floats it is not.
- **Minor units**: the smallest coin of a currency. INR and EUR use 2 decimals.
- **Rounding mode**: the tie-break rule for an exact half. `half_up`: 0.125 becomes 0.13. `half_even` (banker's rounding): 0.125 becomes 0.12 and 0.135 becomes 0.14.
- **Port**: the contract an adapter must satisfy. **Adapter**: the code that talks to a particular ERP.
- **Contract test**: one test suite that every adapter must pass, so a new ERP behaves like the others.

## Source files and entry point
- Caller: `QuotationPricingService.price(context, lines)` in [application/pricing.py](../../src/sales_platform/quotations/application/pricing.py). Demo: [demo_quotation.py](../../scripts/demo_quotation.py).
- Context: `TenantContextResolver.resolve(subject, tenant_id)` in [application/tenancy.py](../../src/sales_platform/platform/application/tenancy.py).
- Rules: `calculate_quotation` in [domain/pricing.py](../../src/sales_platform/quotations/domain/pricing.py); money in [domain/money.py](../../src/sales_platform/platform/domain/money.py); roles in [domain/tenancy.py](../../src/sales_platform/platform/domain/tenancy.py).
- Contracts: [ports/erp.py](../../src/sales_platform/catalogue/ports/erp.py) and [ports/tenancy.py](../../src/sales_platform/platform/ports/tenancy.py).
- Implementations: [simulated ERP](../../src/sales_platform/catalogue/adapters/erp/simulated.py) and [SQL repositories](../../src/sales_platform/platform/persistence/tenancy.py).

## Inputs and validation
- `subject`: authenticated identity string. The placeholder until OIDC (M4).
- `tenant_id`: the business the person asks to act for. It is only a request; stored membership decides.
- `lines`: SKU, `Decimal` quantity and unit. Product matching from free text is M3.
- The tenant policy is read from `tenant_pricing_policies`: currency, minor units, rounding mode, tax rate and maximum price/stock age.

## Execution steps
1. The resolver reads the subject's roles for that tenant inside a tenant-scoped transaction. No roles means `AccessDenied`.
2. The service requires the `quotations:draft` permission before any ERP call.
3. It loads the tenant policy, then fetches products, prices (in the policy currency, effective now) and stock from the ERP.
4. `calculate_quotation` checks each line and either returns `PricedQuotation` or `PricingRejected` with every issue found.
5. A priced quote holds each line's formula, price-list evidence, stock check and the policy snapshot. `explain()` prints the steps.

## Outputs
- `PricedQuotation`: lines, subtotal, tax, total, policy snapshot, `calculated_at` and `stock_review_required`.
- `PricingRejected`: issues with line index, SKU, code and message (e.g. `price_missing`, `quantity_not_multiple`).

## Tenant and authorization boundary
Tenant comes from stored membership, never from a request field. Repositories filter by tenant and forced RLS hides other tenants' rows even if a query forgets. The runtime database role cannot insert memberships or change policies. The ERP adapter only answers for the context's tenant.

## External effects and idempotency
None. The use case reads only; it stores and sends nothing, so repeating it is safe. Storing immutable quotation versions comes with approval work (M4).

## Failures and recovery
- `AccessDenied` or `PermissionDenied`: authorization failures. The messages do not reveal other tenants.
- `PricingPolicyNotConfigured`: the tenant cannot be quoted until an administrator configures a policy.
- `ErpError`: propagated with a normalized code. `retryable` is true for `rate_limited` and `temporary_unavailable`. No fallback values are used.
- `decimal.Inexact`: values too large to multiply exactly; rejected rather than rounded silently.

## Configuration and secrets
No new environment variables. Runtime uses `SALES_DATABASE_URL` and the seed uses `SALES_MIGRATION_DATABASE_URL`. See [configuration](../development/configuration.md).

## Tests and example
See [testing](../development/testing.md). Example with the seeded industrial tenant (INR, `half_up`, 18% synthetic tax):
`IND-BOLT-M8: 150 each x 2.375 INR = 356.250 -> 356.25 INR`. A subtotal of 9336.25 gives tax 1680.525, which rounds to 1680.53, for a total of 11016.78.

## Known limitations
- No discounts.
- No customer-specific price lists (the contract's `customer_context` is deferred).
- No per-line or multiple tax rates, no unit conversion, and no delivery-date promises.
- No admin API for memberships or policies, and no persisted quotation versions.
- Policy changes do not yet keep history: the version number is stored, but older versions are not.

**Related ADRs:** [0002](../adr/0002-tenant-isolation.md), [0003](../adr/0003-erp-authority.md), [0010](../adr/0010-deterministic-pricing-and-tenant-context.md).
