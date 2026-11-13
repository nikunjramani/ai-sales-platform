# ADR 0027: Remove the ERP integration for now
**Status:** accepted (user decision 2026-11-03). Supersedes [0023](0023-erpnext-product-source.md)
and the ERP parts of [0003](0003-erp-authority.md) and [0024](0024-orders.md).

## Decision
The user is not sure an ERP (ERPNext or another) will be used. Until that is decided:
- products, prices and stock live only in our own product list (ADR 0020); the demo businesses
  get seeded demo products (`seed.DEMO_CATALOGUES`);
- removed: the ERPNext package and sandbox, catalogue sync (runs, jobs, "Sync now"), the ERP
  order writer and its operations, and the simulated ERP;
- orders are local only: staff move the status in the portal;
- nothing was released, so the baseline migration was edited in place, not migrated.

## Kept
The product source contract (`catalogue/ports/sources.py`, `ProductSource`, `catalogue_reader`)
and its contract suite, so an ERP or shop can come back as one provider package.

## Consequences
- About 7,000 lines less code, tests and docs; the worker only reads the product tables.
- Bringing an ERP back means a new ADR for its reading (live or copied) and order writing.
