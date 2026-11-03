# 0020: Our own product catalogue as a product source
Date: 2026-10-28
Status: accepted; implemented on branch `feat/phase-3-catalogue` (phase 3 of the [platform plan](../plan/platform/README.md), verified locally)
Amends [0003](0003-erp-authority.md). Extends [0010](0010-deterministic-pricing-and-tenant-context.md), [0016](0016-customer-acceptance.md), [0017](0017-gemini-reader-and-category-replies.md) and [0018](0018-connector-framework.md). Plan: [phase 3](../plan/platform/implementation/phase-3-catalogue.md); answers in its [user answers](../plan/platform/implementation/phase-3-catalogue.md#user-answers-2026-10-28).

## Context
Until phase 3 every product, price and stock figure came from an ERP (in practice the simulated one). A self-service business without an ERP had nothing to quote from (phase 1: "No product source is connected"). D18 decided that products are typed into the portal for now; D19 that we may store a business's catalogue.

## Decision
| Topic | Decision |
|---|---|
| Source | Our own tables, served by `LocalCatalogue`, an `ErpReader`. To the quotation core it is just another product source: matching, pricing, approval and acceptance are unchanged. It is provider `local_catalogue` (purpose catalogue, capability `catalogue_editable`) |
| Authority | The business's selected catalogue source is authoritative. With our list, our rows are the truth; when an ERP is connected later, the ERP is, and our list becomes read-only (this amends 0003) |
| Default | A new business gets our list as its source, created with the business (`SALES_DEFAULT_CATALOGUE_PROVIDER`, no provider name in code). Migration 0011 gives it to every business without an active source; the demo businesses keep the simulated ERP |
| Products | SKU (fixed after creation, unique ignoring case), name (customers see it), unit (`each`, `metre`, `kg`, `box`: the units the reader understands), optional category and staff-only notes, minimum quantity and step (minimum a multiple of the step). The unit is locked once a price or stock count exists. 10,000 active products per business (Q1-Q4, Q13, Q16, Q17) |
| Prices | Excluding tax, in the business currency, > 0, up to 6 decimals and stored with at least the currency's minor units. Each change is a new row (revision N of the one `standard` list); a price starts now or later, never in the past. The price in force at time t: the latest start ≤ t, then the highest revision (Q4-Q9, Q23) |
| Stock | Optional, one location `MAIN`, a count "as of" a time not in the future; "not tracked" means unknown. The latest "as of" wins (Q21) |
| Evidence | Price `fetched_at` = the time of reading (the row is the source, never stale); price list `standard`, version = revision. Stock `fetched_at` = the "as of" time, so the business's max stock age flags old counts (Q10) |
| Archive and delete | Archived products are no longer sold: hidden from search, categories and pricing, so accepting an open quotation with one is refused and revised (0016). Delete only a product never priced, counted or quoted (Q11, Q12) |
| Search | Unchanged: the simulated ERP's word matching moved to `domain/catalogue_search.py` and both sources use it (SKU and its hyphen parts, name, category; simple plurals; best matches only). Stored as `search_terms` with a GIN index. No fuzzy matching, because one hit is quoted automatically (Q15, Q22) |
| Access | All staff read (`catalogue:view`); administrators and sales operators edit (`catalogue:manage`). Edits are refused for a read-only source (by capability) and while suspended (Q14, Q18) |
| Audit | Products and categories carry a row version (409 on a stale edit) and an append-only change log; prices and stock are their own history. Price and stock entries carry a request ID, so a repeated submit is stored once |

## Alternatives considered
- **PostgreSQL full-text search with English stemming:** rejected for now; it would change which products match (and the evaluation) and businesses write in several languages.
- **Overwriting prices:** rejected; quotation evidence must stay explainable.
- **Fuzzy matching in search:** rejected; a typo could be quoted. Trigram indexes exist for the portal search box and a later "did you mean" (only after the user agrees its wording).

## Implementation notes
- **Migration `0011_catalogue`:** `catalogue_categories`, `catalogue_products` (with unused `external_id`/`synced_at` for later sync), `catalogue_prices` and `catalogue_stock` (history, `reject_change`), `catalogue_changes`, `pg_trgm`, forced RLS, API read/write with column-level UPDATE, worker read-only. `create_business` gains two optional arguments for the default source. Downgrade drops the catalogue and every local catalogue connection.
- **Numbers** are unconstrained `numeric` with `scale()` checks, so `4250.00` is never shown as `4250.000000` and the acceptance comparison stays exact.
- **Layers:** the adapter reads through `CatalogueReadStore` (no SQL in adapters); `SqlCatalogueStore` implements both ports; `InMemoryCatalogueStore` (tests, offline evaluation) follows the same rules. `ProviderEnvironment.catalogue_store` hands each process's store to the provider.
- **Setup checklist:** the product step is done when our list has an active product with a price in force (any other source: when connected). It is shown, not required for Go live (Q19).

## Consequences
- A business can quote with no ERP; its prices are typed by staff, so wrong entries are possible: every quotation still needs manager approval, and acceptance re-checks the price.
- A price change after a quotation was issued sends the customer's acceptance back for a revision (by design, 0016).
- Changing the search word rules later needs a recompute of stored `search_terms` (a small owner script).
- Import, ERP/shop connectors, inventory, promotions, customer prices and synonyms plug in later without changing this model (plan §13).
