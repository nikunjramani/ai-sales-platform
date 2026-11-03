# 5. Product data: how catalogue, prices and stock reach us
**The problem:** quotations are only as good as the product, price and stock data behind them. Today it comes from a hardcoded demo ERP, and a new business would have none. Businesses keep this data in very different places:
- an ERP or accounting system (Tally, Zoho Inventory/Books, Odoo, ERPNext, SAP Business One...);
- an online shop (Shopify, WooCommerce);
- a spreadsheet;
- nowhere written down.

## Design: one read contract, many sources
The existing **`ProductSource` port** (renamed in the docs to **catalogue source**; the code name can stay) already holds everything the core needs:
- `search_products`
- `list_categories`
- `get_products`
- `get_prices` (with effective dates and price list)
- `get_availability` (stock, or "unknown")

The core keeps calling only this. Each business **chooses its source** in the portal, as a [connection](03-connectors.md) with purpose `catalogue`. The worker resolves the source **per business** from that connection, instead of the hardcoded `SimulatedErp`.

| Source | How it works | Freshness | Effort | Fits |
|---|---|---|---|---|
| **A. Spreadsheet import** | Manager uploads CSV/Excel (or links a Google Sheet). A mapping screen matches columns (SKU, name, category, unit, price, currency, stock, min qty). Rows are checked, errors are shown per row, and a good file replaces the active version. Stored in **our catalogue tables** | As of the upload; stock gets an "as of" time and the existing stale rules apply | Medium | Small businesses, pilot, demos |
| **B. Manual entry** | Add and edit products, prices and stock in the portal. Same tables as A | As entered | Low (with A) | Very small catalogues, corrections |
| **C. ERP / shop connector (live)** | An adapter reads the business's system on demand, using the existing contract and contract tests | Live | Per system: medium to high; needs that system's API and sandbox | Businesses with an ERP/shop |
| **D. ERP / shop sync** | An adapter copies the system into our catalogue tables on a schedule or by webhook; the core reads our tables | Minutes behind | Per system | Systems with slow or limited APIs; also prepares for E |
| **E. Our own inventory (future)** | Our catalogue tables grow into a full stock system (movements, reservations, warehouses) | Live, it is the source | Large; a future product | Businesses with no system that want one |

**Decided (D18, user 2026-10-23): manual entry in the portal (B) only for now**; spreadsheet import, ERP and shop connectors come in later phases. We store these products (D19). Original recommendation, for context:
- Build **A + B** first: our own catalogue tables plus a "local catalogue" source.
  - It works for every business on day one, needs no outside accounts, and is the foundation for E.
  - Real ERP connectors (C/D) follow for the pilot business's actual system.
  - The existing simulated ERP stays for tests and demos.

## Our catalogue tables (for A, B, D and later E)
All under tenant row-level security (names as in [implementation/00-shared-contracts.md](implementation/00-shared-contracts.md)):
- `catalogue_products`: internal ID, SKU (unique per business), name, description, category, unit, minimum quantity, quantity step, attributes (JSON), active.
- `catalogue_prices`: currency, unit price, price list name and revision, effective from/to. History is kept, because quotations already record the price list ID and version.
- `catalogue_stock`: product, warehouse, available quantity (or unknown), as of, source.
- `catalogue_categories` and `catalogue_changes` (audit of edits).
- Later, with spreadsheet import: `catalogue_imports` (file, column mapping, row errors, status, who, when; the active version is switched atomically).

The "local catalogue" source implements the same contract and passes the same contract tests as the simulated ERP.

## Decision this needs: ADR 0003
ADR 0003 says the platform is "not a replacement inventory database" and keeps only evidence and disposable caches. Options A, B, D and E mean storing a business's catalogue as **their chosen source of truth**. Proposed amendment (D19):
- **When a business connects an ERP, the ERP stays authoritative.**
- **When a business chooses spreadsheet or manual entry, our catalogue is their source,** and the business is responsible for keeping it up to date. The portal shows "as of" dates, and stale stock is flagged as today.

## Product matching on real catalogues
- **Today:** the demo search is simple word matching. With hundreds or thousands of real products, matching quality matters.
- **First:** PostgreSQL full-text search plus trigram similarity (`pg_trgm`) on SKU, name and category. Free, built in.
- **Later, optional:** semantic search with embeddings (pgvector), already listed in the roadmap (M10). Only as a candidate finder; the final match still comes from the source's own products, and the AI never invents a SKU.
- **Synonyms per business** ("ss bolt" = "stainless steel bolt"), editable in the portal (D20).

## Not in scope yet
Customer-specific prices, discounts, several tax rates, cost and margin. They are open in `open-decisions.md` and can be added to the price list model later without breaking it.
