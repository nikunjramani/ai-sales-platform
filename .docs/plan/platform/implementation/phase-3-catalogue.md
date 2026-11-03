# Implementation plan, phase 3: product data (our own catalogue)
**Status:** all open questions answered by the user on 2026-10-28 (see [User answers](#user-answers-2026-10-28)); implementation in progress on branch `feat/phase-3-catalogue`. Where an answer differs from a proposal, the answers section wins. It follows the binding names in [00-shared-contracts.md](00-shared-contracts.md); every deviation is listed in [section 12](#12-proposed-changes-to-shared-contracts) and must be agreed and copied into the shared contracts before coding.
**Builds on:** [05 product data](../05-product-data.md), [06 future commerce](../06-future-commerce.md), [07 decisions](../07-decisions-needed.md) (D18, D19), [08 phases](../08-phases.md), ADRs [0001](../../../adr/0001-adapter-boundaries.md), [0003](../../../adr/0003-erp-authority.md), [0010](../../../adr/0010-deterministic-pricing-and-tenant-context.md), [0016](../../../adr/0016-customer-acceptance.md), [0017](../../../adr/0017-gemini-reader-and-category-replies.md).
**Needs first:** phase 1 (connections, provider registry, per-business catalogue source resolution) and phase 2 (accounts, `create_business`, settings, setup checklist).

## 1. Goal
A business with no ERP can type its products, categories, prices and stock into the portal, and the existing quotation core quotes from them **without any change to matching, pricing, approval or acceptance rules**.

In simple words:
- **Catalogue tables:** new tables that hold one business's products, price history and stock counts, protected by the same row-level security as every other table.
- **`LocalCatalogue`:** a new adapter that implements the existing `ProductSource` port over those tables. To the core it looks exactly like an ERP: same five operations, same contract tests.
- **Product editor:** a new portal feature `catalogue` to add and edit products, categories, prices and stock.
- **Default source:** a new business gets the local catalogue as its catalogue source; the demo businesses keep the simulated ERP.

## 2. Scope
### In scope
| Area | Delivered |
|---|---|
| Database | Migration `0011_catalogue`: `catalogue_categories`, `catalogue_products`, `catalogue_prices` (append-only history), `catalogue_stock` (append-only counts), audit table (proposed, section 12), `pg_trgm`, indexes, RLS, grants |
| Core rules | Product editing rules (`domain/catalogue_admin.py`), one shared search tokeniser (`domain/catalogue_search.py`) |
| Source adapter | `LocalCatalogue` (`adapters/catalogue/local.py`) passing the ERP contract suite; registered as provider `local_catalogue` with purpose `catalogue` |
| Search | Word matching identical to today's simulated ERP (hyphen split, simple plurals, category words, "best matches only"), backed by a GIN index; trigram index for the portal search box |
| Edits | Prices: new row per change, never overwritten. Stock: new count per change with an "as of" time. Products/categories: optimistic versioning, archive instead of delete, audit trail |
| API | `/api/v1/tenants/{tenant_id}/catalogue/...` endpoints (list with search, filter and paging; CRUD; prices; stock; categories; summary) |
| Portal | Feature `catalogue`: product list, add/edit product, categories, prices, stock |
| Onboarding | Setup checklist step "Product data" (at least one active product with a current price) |
| Quality | ERP contract suite on the new adapter (in memory and PostgreSQL), search quality cases, evaluation dataset against `LocalCatalogue` |
| Docs | ADR 0020 (local catalogue; amends ADR 0003), feature doc, data model, integrations, code map, configuration, testing, handoff |

### Out of scope (later phases, designed to plug in)
- Spreadsheet import (`catalogue_imports`), ERP or shop connectors and sync (D18).
- Our own inventory: stock movements, reservations, warehouses (06).
- Customer-specific prices, discounts, several tax rates, cost and margin.
- Temporary/promotional prices with an end date (open question Q9).
- Product synonyms per business (D20, open).
- Semantic search with embeddings (roadmap M10).
- Product images, attributes JSON, variants, bundles.
- Bulk edit in the portal.

## 3. Decisions used and open questions
### User answers (2026-10-28)
The user answered every question on 2026-10-28 and confirmed the summary ("Yes, start phase 3"). Every answer is the proposed default, so the plan below stands unchanged:

| # | Answer |
|---|---|
| Q1 | Required: SKU, name, unit; category and description optional; minimum and step default to 1 |
| Q2 | SKU format as proposed; unique per business ignoring case; cannot be changed after creation |
| Q3 | Only the four units the reader understands (`each`, `metre`, `kg`, `box`) |
| Q4 | Prices up to 6 decimals (stored with at least the currency's minor units); quantities up to 3 |
| Q5 | Price must be greater than 0 |
| Q6 | Prices exclude tax |
| Q7 | One currency per business (the quotation currency) |
| Q8 | A price starts now or in the future, never in the past |
| Q9 | No temporary prices in phase 3 |
| Q10 | Price `fetched_at` = read time; stock `fetched_at` = "as of"; new businesses get max stock age **7 days** |
| Q11 | Archived products cannot be quoted or accepted |
| Q12 | Delete only if never priced, counted or quoted; otherwise archive |
| Q13 | 10,000 active products per business |
| Q14 / U1 | `catalogue:view` for all four staff roles; `catalogue:manage` for administrator and sales operator |
| Q15 | No "did you mean" suggestions yet; only the trigram index is built |
| Q16, Q17 | Minimum must be a multiple of the step; unit locked once a price or stock entry exists |
| Q18 | Other sources: read-only screens with a "Products come from <source>" banner |
| Q19 | "Product data" checklist step shown, not blocking Go live |
| Q20 | Third dev-only demo business "Demo Local Catalogue"; both existing demo businesses stay on the simulated ERP |
| Q21 | Stock optional, one location `MAIN`, "not tracked" allowed |
| Q22 | Search uses SKU, name and category only |
| Q23 | One price list `standard`; evidence "standard, revision N"; source display name "Our product list" |
| C6 | New businesses get the local catalogue by default (`SALES_DEFAULT_CATALOGUE_PROVIDER`); existing businesses without a catalogue connection get one in migration 0011 |
| Git | Same as phases 1 and 2: one commit per work item, pushed, draft PR to `main`; the user merges |
| pg_trgm (asked during W15, 2026-10-28) | The migration role gets CREATE on `quotation_dev` in `infra/postgres/init.sql` (it owns `quotation_test` already), so 0011 can create the trusted `pg_trgm` extension. A hosted database needs the same or a pre-installed extension (D24) |

### Decisions used (binding)
| # | Decision | Effect on this plan |
|---|---|---|
| D18 | Products are added **by hand in the portal** for now | Only the manual editor is built. Tables have a `source` column (only `manual` allowed now) so import and sync add values later |
| D19 | We store the business's catalogue; an ERP stays authoritative when connected later | ADR 0003 is amended by a new ADR 0020. The local catalogue is the business's source of truth only while it is their selected catalogue source |
| Contract §1/§2/§4/§6/§7 | Names `LocalCatalogue`, `catalogue_products`, `catalogue_prices`, `catalogue_stock`, `catalogue_categories`, migration `0011_catalogue`, permission `catalogue:manage` (administrator, sales_operator), portal feature `catalogue` | Used as given |
| ADR 0010 | Money is `Decimal`; missing or wrong price blocks; no unit conversion; stock never blocks but is reported | Unchanged; the local source must feed it honestly |
| ADR 0016 | Acceptance re-prices from the source; any difference refuses and revises | A manual price change between quote and acceptance triggers exactly that path |
| ADR 0017 | `list_categories` names what the business sells; search also matches category words | The local source keeps both behaviours |

### Open questions (ask with the clarify-before-building procedure before coding)
Each has a **proposed default, to confirm**. None of these is decided by this plan.

| # | Question | Proposed default (to confirm) | Why it matters |
|---|---|---|---|
| Q1 | Which product fields are required? | Required: SKU, name, unit. Optional: category, description. Minimum quantity and step default to 1 | Form and API validation |
| Q2 | SKU format and changes | 1-64 characters: letters, digits, `- _ . /`, starting with a letter or digit; unique per business ignoring case; **cannot be changed** after creation (archive and create a new one instead) | Quotations and snapshots refer to the SKU text |
| Q3 | Which units can a product use? | Only the catalogue units the enquiry reader already understands: `each`, `metre`, `kg`, `box` (from `UNIT_ALIASES`). New units (litre, pair, set, roll...) are added by extending that table | Any other unit could never match a customer's words (see finding F8) |
| Q4 | Decimal places | Prices up to 6 decimals; quantities (minimum, step, stock) up to 3 decimals. A price is stored with at least the currency's minor units (4250 becomes 4250.00) | Display and exact acceptance comparison (F12) |
| Q5 | Is a price of 0 allowed (free item)? | No: price must be greater than 0 | "A price is never set to zero" today means missing; a real free item is a business rule |
| Q6 | Prices exclude tax? | Yes: the calculator adds the business's tax once on the subtotal (ADR 0010), so the form says "price excluding tax" | Wrong entry would double-tax |
| Q7 | One currency per business? | Yes: prices are entered only in the business's quotation currency (pricing policy). If the business later changes currency, old prices stop being used and products show "no price in INR" | Currency mismatch rules |
| Q8 | Can a price start in the future or the past? | Future: yes (a date, from midnight in the business's time zone, or "now"). Past: no (history must not be rewritten) | Price history and evidence |
| Q9 | Temporary prices with an end date? | Not in phase 3; a price runs until the next price for that product starts | Avoids inventing promotion rules |
| Q10 | What do "fetched at" / "as of" mean for our own data, and what stale-stock age should new businesses get? | Price: fetched at = the moment we read it (the stored row **is** the source, so it is never stale). Stock: fetched at = the "as of" time a person entered. New businesses get **max stock age 7 days** and keep max price age as phase 2 sets it | With today's demo value (5 minutes) every typed stock count would be stale at once (F4) |
| Q11 | Can an archived product still be quoted or accepted? | No: archived means "no longer sold". It disappears from search, categories and `get_products`; an open quotation that is accepted later is refused and revised, and the revision ends in `pricing_failed` for a sales operator (ADR 0016 rule) | Customer-visible behaviour |
| Q12 | Can a product be deleted? | Only if it never had a price or stock entry and its SKU is in no quotation; otherwise "archive instead" | Evidence must stay explainable |
| Q13 | Maximum products per business | 10,000 active products | Performance, abuse |
| Q14 | Who may see the catalogue? | All staff roles (administrator, sales operator, approver, viewer). Editing: `catalogue:manage` (administrator, sales operator, as in the shared contract) | New read permission (section 12) |
| Q15 | Typing mistakes ("exaust fan", "INDFAN450"): suggest a product? | Not quoted automatically. Phase 3 builds the trigram index; "Did you mean ...?" suggestions are only built after a yes and agreed wording (section 6.4) | Today a single search result is matched automatically (F7) |
| Q16 | Must the minimum quantity be a multiple of the step? | Yes, otherwise the minimum itself cannot be ordered | Pricing rejects non-multiples (F3) |
| Q17 | Can the unit change after prices exist? | No: only while the product has no price or stock entry | Prices and stock are stored per unit; a change would block quoting |
| Q18 | Business whose catalogue source is not the local catalogue (the demo businesses, later ERP users) | The catalogue screens are read-only with a banner "Products come from <source>" | Avoid edits that never reach quotations |
| Q19 | Setup checklist and "Go live" | Step done when at least one active product has a current price; it is shown but does **not** block "Go live" | Phase 2 owns "Go live" |
| Q20 | Demo data | Keep both demo businesses on the simulated ERP; add a third dev-only demo business "Demo Local Catalogue" seeded with the same synthetic industrial data | Live demo of editing without breaking tests |
| Q21 | Stock entry | Optional; one location per business (`MAIN`) in phase 3; "not tracked" is a valid entry (unknown quantity) | Matches `StockLevel.available_quantity = None` |
| Q22 | Is the description searched? | No: search uses SKU, name and category only, as [05](../05-product-data.md) says | Search quality |
| Q23 | Price list name on evidence | One list per business called `standard`; evidence shows "price list standard, revision N" | Snapshot fields `price_list_id`/`price_list_version` |

## 4. Current-state findings (file:line)
| # | Finding | Where | Consequence for phase 3 |
|---|---|---|---|
| F1 | `ProductSource` has five reads: `search_products(query, limit=10)`, `list_categories`, `get_products`, `get_prices(skus, currency, effective_at)`, `get_availability(skus, warehouse)`. Rules: tenant-scoped, unknown SKUs omitted, timezone-aware `fetched_at`, failures as `ErpError` | `src/sales_platform/catalogue/ports/erp.py:1-8`, `:45-70`; codes `:19-28`; retryable `:31-42` | `LocalCatalogue` implements this unchanged. Database failures must become `ErpError(TEMPORARY_UNAVAILABLE)` so callers retry |
| F2 | `Product` has `sku` (≤64), `description` (≤500), `unit` (≤32), positive `minimum_quantity`/`quantity_step`, optional `category` (≤100). There is no separate "name" | `domain/catalogue.py:12-30` | Our `name` maps to `Product.description` (it is what customers see in questions). Our longer `description` is staff-only |
| F3 | `PriceQuote` carries `price_list_id`, `price_list_version`, `effective_from/to`, `fetched_at`, `source`; `is_effective_at` | `domain/catalogue.py:34-62` | Local mapping: list code `standard`, version = revision number, `effective_to` = start of the next scheduled price |
| F4 | `StockLevel.available_quantity=None` means unknown; `fetched_at` required | `domain/catalogue.py:66-84` | A "not tracked" entry stores NULL. `fetched_at` = the entered "as of" time |
| F5 | Pricing issues: `product_not_found`, `quantity_not_positive`, `unit_mismatch`, `quantity_below_minimum`, `quantity_not_multiple`, `price_missing`, `price_unit_mismatch`, `price_currency_mismatch`, `price_not_effective`, `price_stale`. Staleness: `now - price.fetched_at > max_price_age` (`:272-273`); stock stale `now - level.fetched_at > max_stock_age` (`:286-287`); stock unit differs gives unknown (`:282-285`); no stock row gives unknown (`:280-281`) | `domain/pricing.py:69-79`, `:214-274`, `:277-295` | Local price `fetched_at` = read time (never stale; the row is the source). Local stock `fetched_at` = "as of", so max stock age flags old counts. Price rows store their unit so a unit change cannot silently re-use an old price |
| F6 | The pricing service reads products, prices (at `now`, in the policy currency) and stock, then calls the pure calculator | `application/pricing.py:33-45` (`:38-42`) | No change; it receives the resolved source from phase 1 |
| F7 | `decide_match`: no candidates = not found; **exactly one candidate = matched automatically**; several = the interpreter may pick only among them, else ambiguous | `domain/requirements.py:107-118` | Search must not return weak or fuzzy hits, or a typo could be quoted. Hence "best matches only" and no fuzzy matching in `search_products` (Q15) |
| F8 | Customer units are normalised by a fixed alias table to `each`, `metre`, `kg`, `box`; a count without a unit is `each` only for `each` products; any other mismatch becomes a question | `domain/requirements.py:22-35`, `:205-217` | A product unit outside these four could never be quoted from text (Q3) |
| F9 | `needs_categories` / `assess` name the business's categories when nothing was understood or a product is unknown; ambiguous lists show up to 5 SKUs with descriptions | `domain/requirements.py:121-130`, `:156-228` (`:196`) | `list_categories` must list only categories that really have active products |
| F10 | Workflow match node: `SEARCH_LIMIT = 5`; one search per verified item; categories fetched only when needed | `workflows/enquiry.py:54`, `:164-187`, `:215-218` | Local search must respect `limit` and the 1-50 range |
| F11 | Simulated search: words = lower-case `[a-z0-9]+(-[a-z0-9]+)*`, trailing `s` removed for words longer than 3 not ending in `ss`, words shorter than 2 dropped (`:40-48`); query also searched with hyphens as spaces (`:87-88`); product words from SKU, SKU with hyphens split, description and category (`:91-93`); score = shared words; **only the best score is kept**, sorted by SKU (`:97-100`); categories sorted, from products (`:102-104`); price = latest `effective_from` among effective rows in the currency (`:123-132`); `fetched_at` = clock (`:119`, `:152`); other warehouse returns nothing (`:150-151`); unknown tenant raises `UNSUPPORTED` (`:76-78`) | `adapters/erp/simulated.py` | The tokeniser moves to `domain/catalogue_search.py` and both adapters use it, so matching stays identical. Evaluation cases depend on this exactly |
| F12 | Demo data: IND-PUMP-2HP has no price (`:215`), IND-CABLE-2C stock unknown (`:220`), FAN-450 has an old and a new price (`:195-205`), bolt min 100 step 50 (`:179-186`), cable step 0.5 | `adapters/erp/simulated.py:170-241` | The same synthetic data seeds the local catalogue for evaluation and the third demo business |
| F13 | Snapshot stores per line `price_list_id`, `price_list_version`, `price_source`, `price_fetched_at`, `stock_fetched_at` and prices as **strings** | `domain/quotations.py:188-199` | Local evidence must be stable text. PostgreSQL `numeric(18,6)` would turn `4250.00` into `4250.000000` on the PDF and in explanations |
| F14 | Acceptance re-prices and compares `sku`, `quantity`, `unit`, `unit_price`, `line_total`, totals **as strings** | `domain/acceptance.py:138-160` (`:154`); `application/acceptance.py:210-243` (`:219`) | Stored scale must not change between quoting and accepting. A manual price edit correctly triggers "refused, revised" |
| F15 | Pricing policy holds currency and freshness limits; demo: price 900 s, stock 300 s | `persistence/models.py:64-100`; `migrations/versions/0002_memberships_policies.py:55-56`; `persistence/seed.py:35-38`, `:80-81` | Q10: typed stock needs a longer default for new businesses |
| F16 | Table pattern: `_TENANT_FILTER`, ENABLE + FORCE RLS, `tenant_isolation` policy, explicit grants; append-only via `reject_change()` trigger; column-level UPDATE grants | `migrations/versions/0002_memberships_policies.py:11-22`; `0005_approvals.py:249-255`, `:286-304`; `0007_acceptance.py:24`, `:111-125` | Followed exactly |
| F17 | `alembic check` runs in `scripts/check_database.py` and compares `models.py` with the database; only checkpoint tables are excluded | `migrations/env.py:14-29`; `scripts/check_database.py` | GIN and expression indexes must be declared in `models.py` exactly, or explicitly excluded with a reason |
| F18 | Architecture test: `adapters` may import only domain, ports, adapters and a short third-party list (no SQLAlchemy); `persistence` is free | `tests/unit/test_architecture.py:12-30` | `LocalCatalogue` (adapter) cannot run SQL itself. It works over a port (`CatalogueReadStore`) implemented in `persistence/catalogue.py` |
| F19 | ERP contract suite: one factory per adapter (`ADAPTERS`), checks known SKU evidence, unknown SKU absent, other tenant blind, search scoped, naive date refused | `tests/unit/ports/test_erp_contract.py:22-91` | Add `local` (in memory) and a PostgreSQL run; move generic checks from the simulated tests into the contract |
| F20 | Simulated-only tests: unconfigured tenant, injected failure, unknown stock, warehouse, invalid search, categories, category search, partial code | `tests/unit/adapters/erp/test_simulated.py:17-98` | Invalid search, categories and best-match search become contract checks |
| F21 | Evaluation runs the real workflow with any `ProductSource`; 14 synthetic cases | `evaluation.py:32`, `:142-149`; `evals/enquiry_extraction.jsonl` | Run the same dataset against `LocalCatalogue` |
| F22 | Composition roots hardcode `SimulatedErp` | `worker.py:69`; `api/app.py:92` | Phase 1 replaces this with per-business resolution; phase 3 only registers the new provider |
| F23 | Portal API pattern: router per area with `/api/v1/tenants/{tenant_id}` prefix, `Context` dependency, `guarded()` maps errors to 403/404/409/422/503; mutations need the CSRF header | `api/portal.py:400-441`; `api/auth.py:32`, `:89-91` | Catalogue router copies this pattern |
| F24 | Session exposes `permissions` per tenant; portal checks with `useCurrentTenant().can(...)` | `api/auth.py:55-59`; `portal/src/features/tenant/hooks/useCurrentTenant.ts:24` | Edit buttons hidden without `catalogue:manage` (the API still enforces) |
| F25 | Portal layering is enforced: model/api/services pure; ui never imports api; only `shared/api/httpClient.ts` calls `fetch`; features only via `index.ts`; tests outside `src` | `portal/tests/architecture.test.ts:13-25`, `:59-87`, `:96-107` | Feature `catalogue` follows the same folders as `quotations` |
| F26 | Permissions live in `Permission` and `ROLE_PERMISSIONS` | `domain/tenancy.py:25-68` | Add `catalogue:manage` (and proposed `catalogue:view`) |

## 5. Design summary
### 5.1 Data flow
```
portal catalogue screens ──> /catalogue API ──> ProductService (application) ──> CatalogueStore (port)
                                                                                   │
                                                              SqlCatalogueStore (persistence) ──> catalogue_* tables (RLS)
                                                                                   │
workflow / pricing / acceptance ──> ProductSource (port) ── resolved per business (phase 1)
                                       └─ LocalCatalogue (adapter) ──> CatalogueReadStore (port, same SQL class)
```

### 5.2 What "fetched at" and "as of" mean for our own data (Q10, to confirm)
| Fact | Value given to the core | Reason |
|---|---|---|
| Price `fetched_at` | The time of the read (adapter clock) | Price rows are immutable and the effective one is chosen at read time from the business's own source. There is no copy that can be outdated, so `price_stale` never fires for manual prices. **Later:** synced or imported rows (`source` `sync`/`import`) use their sync/import time instead, so the policy's max price age then applies |
| Price `effective_from` / `effective_to` | Row's `effective_from`; `effective_to` = `effective_from` of the next later price for the same product, list and currency (or none) | Honest evidence of how long the price runs |
| Stock `fetched_at` | The count's `as_of` (when a person says the quantity was true) | A typed count ages. The business's max stock age flags it `stale`, which already forces stock review in approval (ADR 0010) |
| Stock unknown | Row with NULL quantity ("not tracked"), or no row at all | Both give `unknown` today |

### 5.3 Price history rules (never overwrite)
- Each change inserts a new `catalogue_prices` row with the next `revision` for (product, price list, currency).
- The price used at time *t* is the row with the latest `effective_from ≤ t`; if two rows start at the same moment, the higher revision wins. So a scheduled future price is cancelled by entering another price for the same start (no update, no delete).
- Evidence: `price_list_id = "standard"`, `price_list_version = str(revision)`. Together with the product SKU this names exactly one row, so every quotation snapshot stays explainable.
- Rows store `unit` (the product's unit at entry) and `currency`. If either no longer matches, the core reports `price_unit_mismatch` or `price_missing`; nothing is guessed.

### 5.4 Stock rules
- Each entry inserts a `catalogue_stock` row (quantity or NULL, `as_of`, `source = manual`, location `MAIN`).
- The current level is the row with the latest `as_of` (ties: latest `recorded_at`) per product and location. Entering an older count later does not hide a newer one.
- `as_of` may be in the past, never in the future (5 minutes clock tolerance).
- **Later inventory:** these rows become "counts" (`source = count`); `stock_movements` and `reservations` add deltas after the last count. `LocalCatalogue.get_availability` then returns "last count + movements − active reservations" with the same port. Nothing in this design needs to be dropped for that.

## 6. Ordered work items
Each item lists files, signatures and tests. Items 1-4 are backend foundations, 5-8 make the source work, 9-11 make it editable, 12-14 finish.

### 6.0 Ask and record (before any code)
- Run the clarify-before-building procedure with Q1-Q23; record answers in `07-decisions-needed.md` (new rows D25+), `open-decisions.md` and the handoff.
- Confirm the proposed contract changes in section 12 with the owners of phases 1 and 2.

### 6.1 Shared search tokeniser (refactor, no behaviour change)
- **New** `src/sales_platform/catalogue/domain/catalogue_search.py` (standard library only):
  ```python
  def search_words(text: str) -> frozenset[str]: ...            # today's _search_words, moved
  def query_terms(query: str) -> frozenset[str]:                 # words of query + query with "-" as " "
  def product_terms(sku: str, name: str, category: str | None) -> frozenset[str]
      # words of sku + sku with "-" as " " + name + category (same as simulated.py:91-93)
  def best_matches[T](scored: Iterable[tuple[int, str, T]], limit: int) -> list[T]
      # keep only the highest score > 0, sort by SKU (code-point order), cut to limit
  ```
- `adapters/erp/simulated.py` imports these instead of its private `_search_words` (`:40-48`, `:87-100`).
- Tests: `tests/unit/domain/test_catalogue_search.py` (hyphen compounds, plural rule incl. `ss`, two-letter minimum, case, digits, duplicates). All existing simulated and evaluation tests must stay green unchanged.

### 6.2 Product editing rules
- **New** `src/sales_platform/catalogue/domain/catalogue_admin.py`:
  ```python
  CATALOGUE_UNITS: frozenset[str] = frozenset(UNIT_ALIASES.values())   # each, metre, kg, box (Q3)
  LOCAL_PRICE_LIST = "standard"                                          # Q23
  DEFAULT_LOCATION = "MAIN"                                              # Q21
  MAX_ACTIVE_PRODUCTS = 10_000                                           # Q13
  PRICE_DECIMALS, QUANTITY_DECIMALS = 6, 3                               # Q4

  class InvalidCatalogueEntry(ValueError):  field: str                   # -> 422 with field name

  @dataclass(frozen=True) class ProductDraft:     sku, name, description, category_id, unit, minimum_quantity, quantity_step
  @dataclass(frozen=True) class ProductChanges:   optional name, description, category_id, unit, minimum_quantity, quantity_step
  @dataclass(frozen=True) class PriceEntryDraft:  unit_price: Decimal, effective_from: datetime | None (None = now), note
  @dataclass(frozen=True) class StockEntryDraft:  quantity: Decimal | None, as_of: datetime | None, note

  def normalise_price(amount: Decimal, minor_units: int) -> Decimal      # never fewer decimals than the currency
  def check_price_entry(entry, *, currency, policy_currency, now) -> datetime   # returns effective_from
  def check_stock_entry(entry, *, now) -> datetime                               # returns as_of
  def effective_price(rows: Sequence[PriceRow], at: datetime) -> tuple[PriceRow, datetime | None] | None
  def current_stock(rows: Sequence[StockRow], location: str) -> StockRow | None
  def stock_is_stale(as_of: datetime, max_age: timedelta, now: datetime) -> bool
  def field_changes(before: Mapping[str, object], after: Mapping[str, object]) -> dict[str, list[object]]
  ```
- Rules (all "to confirm" via Q1-Q17): SKU pattern; name 1-200; description ≤2000; unit in `CATALOGUE_UNITS`; minimum and step > 0, ≤3 decimals, minimum a multiple of step; price > 0, ≤6 decimals, < 10^12, currency = policy currency, `effective_from` ≥ now (minus 5 minutes tolerance); stock ≥ 0 or None, ≤3 decimals, `as_of` ≤ now + 5 minutes; unit change refused if prices or stock exist.
- Tests: `tests/unit/domain/test_catalogue_admin.py`, one test per rule plus `effective_price` (scheduled price, same-start tie, next change as `effective_to`, nothing effective yet) and `current_stock` (older count entered later).

### 6.3 Permissions
- `domain/tenancy.py`: add `Permission.MANAGE_CATALOGUE = "catalogue:manage"` to administrator and sales operator (contract §6). Proposed: `Permission.VIEW_CATALOGUE = "catalogue:view"` for all four staff roles, not `intake_service` (section 12).
- Tests: extend `tests/unit/domain/test_tenancy.py` (role map) and confirm the session's permission list in `tests/unit/api/test_portal.py`.

### 6.4 Ports
- **New** `src/sales_platform/catalogue/ports/catalogue_admin.py` (contract name):
  ```python
  class CatalogueUnavailable(Exception): ...            # store could not be reached; adapter maps to ErpError

  @dataclass(frozen=True) class CategoryRecord:  id, name, archived_at, row_version, active_products: int
  @dataclass(frozen=True) class ProductRecord:   id, sku, name, description, category: CategoryRecord | None, unit,
                                                 minimum_quantity, quantity_step, archived_at, row_version,
                                                 created_at, updated_at
  @dataclass(frozen=True) class PriceRecord:     id, product_id, price_list, revision, currency, unit, unit_price,
                                                 effective_from, source, note, created_at, created_by
  @dataclass(frozen=True) class StockRecord:     id, product_id, location, quantity, unit, as_of, source, note,
                                                 recorded_at, recorded_by
  @dataclass(frozen=True) class ProductQuery:    text: str | None, category_id: UUID | None,
                                                 state: Literal["active", "archived", "all"], limit: int, after: str | None
  @dataclass(frozen=True) class ProductPage:     items: tuple[ProductRecord, ...], next_after: str | None

  class CatalogueReadStore(Protocol):                    # used by LocalCatalogue only
      def search(self, tenant_id: UUID, terms: frozenset[str], limit: int) -> Sequence[tuple[int, ProductRecord]]: ...
      def active_products(self, tenant_id: UUID, skus: Sequence[str]) -> Sequence[ProductRecord]: ...
      def effective_prices(self, tenant_id: UUID, skus: Sequence[str], currency: str, at: datetime,
                           price_list: str) -> Sequence[tuple[str, PriceRecord, datetime | None]]: ...
      def current_stock(self, tenant_id: UUID, skus: Sequence[str], location: str) -> Sequence[tuple[str, StockRecord]]: ...
      def category_names_in_use(self, tenant_id: UUID) -> Sequence[str]: ...

  class CatalogueStore(Protocol):                        # used by ProductService
      list_products, get_product, insert_product, update_product (expected_version), archive/restore,
      delete_unreferenced_product, product_is_referenced, count_active_products,
      list_prices, insert_price (next revision, command_id replay), list_stock, insert_stock (command_id replay),
      list_categories, insert_category, rename_category (re-computes search terms of its products),
      archive_category, record_change, summary
  ```
  Exceptions: `ProductNotFound`, `CategoryNotFound`, `SkuAlreadyExists`, `CategoryNameTaken`, `VersionConflict`, `ProductReferenced`, `CatalogueLimitReached`.

### 6.5 Migration `0011_catalogue` and models
**File:** `migrations/versions/0011_catalogue.py`, `down_revision = "0010_accounts"`. Mirror every table, constraint and index in `persistence/models.py` (F17).

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;           -- trusted extension in PostgreSQL 13+ (we run 17)

CREATE TABLE catalogue_categories (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name varchar(100) NOT NULL CHECK (btrim(name) <> ''),
  archived_at timestamptz NULL,
  row_version integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
  created_at timestamptz NOT NULL DEFAULT now(), created_by varchar(200) NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(), updated_by varchar(200) NOT NULL,
  CONSTRAINT uq_catalogue_categories_tenant_id UNIQUE (tenant_id, id)
);
CREATE UNIQUE INDEX uq_catalogue_categories_name ON catalogue_categories (tenant_id, lower(name));

CREATE TABLE catalogue_products (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  sku varchar(64) NOT NULL CHECK (sku ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$'),
  name varchar(200) NOT NULL CHECK (btrim(name) <> ''),
  description varchar(2000) NULL,
  category_id uuid NULL,
  unit varchar(32) NOT NULL CHECK (btrim(unit) <> ''),          -- list of units lives in the domain (Q3)
  minimum_quantity numeric NOT NULL CHECK (minimum_quantity > 0 AND minimum_quantity < 1e9 AND scale(minimum_quantity) <= 3),
  quantity_step    numeric NOT NULL CHECK (quantity_step > 0 AND quantity_step < 1e9 AND scale(quantity_step) <= 3),
  search_terms text[] NOT NULL,                                 -- from domain.catalogue_search.product_terms
  source varchar(16) NOT NULL DEFAULT 'manual' CHECK (source IN ('manual')),
  archived_at timestamptz NULL, archived_by varchar(200) NULL,
  row_version integer NOT NULL DEFAULT 1 CHECK (row_version >= 1),
  created_at timestamptz NOT NULL DEFAULT now(), created_by varchar(200) NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(), updated_by varchar(200) NOT NULL,
  CONSTRAINT uq_catalogue_products_tenant_id UNIQUE (tenant_id, id),
  CONSTRAINT fk_catalogue_products_category FOREIGN KEY (tenant_id, category_id)
    REFERENCES catalogue_categories (tenant_id, id)              -- NULL category skips the check
);
CREATE UNIQUE INDEX uq_catalogue_products_sku ON catalogue_products (tenant_id, lower(sku));
CREATE INDEX ix_catalogue_products_category ON catalogue_products (tenant_id, category_id);
CREATE INDEX ix_catalogue_products_terms ON catalogue_products USING gin (search_terms);
CREATE INDEX ix_catalogue_products_sku_trgm  ON catalogue_products USING gin (lower(sku)  gin_trgm_ops);
CREATE INDEX ix_catalogue_products_name_trgm ON catalogue_products USING gin (lower(name) gin_trgm_ops);

CREATE TABLE catalogue_prices (                                 -- append-only history
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  product_id uuid NOT NULL,
  price_list varchar(64) NOT NULL DEFAULT 'standard',
  revision integer NOT NULL CHECK (revision >= 1),
  currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  unit varchar(32) NOT NULL,
  unit_price numeric NOT NULL CHECK (unit_price > 0 AND unit_price < 1e12 AND scale(unit_price) <= 6),  -- Q5/Q4
  effective_from timestamptz NOT NULL,
  source varchar(16) NOT NULL DEFAULT 'manual' CHECK (source IN ('manual')),
  note varchar(500) NULL,
  command_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), created_by varchar(200) NOT NULL,
  CONSTRAINT fk_catalogue_prices_product FOREIGN KEY (tenant_id, product_id) REFERENCES catalogue_products (tenant_id, id),
  CONSTRAINT uq_catalogue_prices_revision UNIQUE (tenant_id, product_id, price_list, currency, revision),
  CONSTRAINT uq_catalogue_prices_command UNIQUE (tenant_id, command_id)
);
CREATE INDEX ix_catalogue_prices_lookup
  ON catalogue_prices (tenant_id, product_id, price_list, currency, effective_from DESC, revision DESC);

CREATE TABLE catalogue_stock (                                  -- append-only counts
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  product_id uuid NOT NULL,
  location_code varchar(32) NOT NULL DEFAULT 'MAIN',
  quantity numeric NULL CHECK (quantity IS NULL OR (quantity >= 0 AND quantity < 1e12 AND scale(quantity) <= 3)),
  unit varchar(32) NOT NULL,
  as_of timestamptz NOT NULL,
  source varchar(16) NOT NULL DEFAULT 'manual' CHECK (source IN ('manual')),
  note varchar(500) NULL,
  command_id uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(), recorded_by varchar(200) NOT NULL,
  CONSTRAINT ck_catalogue_stock_as_of CHECK (as_of <= recorded_at + interval '5 minutes'),
  CONSTRAINT fk_catalogue_stock_product FOREIGN KEY (tenant_id, product_id) REFERENCES catalogue_products (tenant_id, id),
  CONSTRAINT uq_catalogue_stock_command UNIQUE (tenant_id, command_id)
);
CREATE INDEX ix_catalogue_stock_current
  ON catalogue_stock (tenant_id, product_id, location_code, as_of DESC, recorded_at DESC);

CREATE TABLE catalogue_changes (                                -- proposed (section 12); append-only audit
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  entity varchar(16) NOT NULL CHECK (entity IN ('product', 'category')),
  entity_id uuid NOT NULL,                                      -- no FK: survives deletion of an unused product
  action varchar(16) NOT NULL CHECK (action IN ('created', 'updated', 'archived', 'restored', 'deleted')),
  changes jsonb NOT NULL,                                       -- {"field": [old, new]}
  actor varchar(200) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_catalogue_changes_entity ON catalogue_changes (tenant_id, entity_id, created_at);
```
**Row-level security, triggers and grants** (same helper style as 0002/0007):
- For all five tables: `ENABLE` + `FORCE ROW LEVEL SECURITY`, policy `tenant_isolation USING (_TENANT_FILTER) WITH CHECK (_TENANT_FILTER)`.
- `BEFORE UPDATE OR DELETE ... EXECUTE FUNCTION reject_change()` on `catalogue_prices`, `catalogue_stock`, `catalogue_changes`.
- `quotation_app` (API): `SELECT, INSERT` on all five; `UPDATE (name, archived_at, row_version, updated_at, updated_by)` on categories; `UPDATE (name, description, category_id, unit, minimum_quantity, quantity_step, search_terms, archived_at, archived_by, row_version, updated_at, updated_by)` on products; `DELETE` on products and categories (service allows it only when unreferenced, Q12). No `UPDATE`/`DELETE` on prices, stock, changes.
- `quotation_worker`: `SELECT` on categories, products, prices, stock (workflow and pricing read; the worker never edits the catalogue).
- Numbers use unconstrained `numeric` with `scale()` checks, so a value keeps the decimals it was stored with (F13, F14).

**Default catalogue source for businesses** (depends on phase 1/2 shapes, section 12):
- `CREATE OR REPLACE FUNCTION create_business(...)` (phase 2's function) gains a parameter `default_catalogue_provider text`; when not NULL it also inserts one `connections` row (`provider_key` = that value, `purposes = {catalogue}`, enabled) and its `connection_events` audit row, in the same transaction. The API passes the setting `SALES_DEFAULT_CATALOGUE_PROVIDER` (default `local_catalogue`). No provider name is written in core code.
- Backfill: every tenant **without** an enabled `catalogue` connection gets a `local_catalogue` one. Demo tenants already have a simulated catalogue connection from phase 1 (assumption, section 12), so they are untouched.

**Downgrade** (`0011 → 0010`, loses catalogue data, like 0007 loses responses):
1. Restore phase 2's `create_business` body.
2. Remove backfilled `local_catalogue` connections and their events (temporarily `NO FORCE` RLS and disable the `connection_events` immutability trigger inside the migration, as 0007 did for documents).
3. `DROP TABLE catalogue_changes, catalogue_stock, catalogue_prices, catalogue_products, catalogue_categories`.
4. `DROP EXTENSION IF EXISTS pg_trgm` (plain `RESTRICT`: it fails loudly if something else uses it, instead of dropping it from under another feature).

**`alembic check`:** declare the expression and GIN indexes in `models.py` with `Index(..., postgresql_using="gin", postgresql_ops=...)`. If autogenerate still reports false differences for expression indexes, exclude **only those index names** in `migrations/env.py` `include_object`, with a comment, and assert their existence in an integration test instead.

### 6.6 SQL store
- **New** `src/sales_platform/catalogue/persistence/catalogue.py`: `SqlCatalogueStore(engine)` implementing `CatalogueStore` and `CatalogueReadStore`. Every method uses `tenant_transaction(engine, tenant_id)` and an explicit `tenant_id =` filter (RLS is the second guard). `OperationalError`/`InterfaceError` in read methods become `CatalogueUnavailable`; unique violations become `SkuAlreadyExists` / `CategoryNameTaken`; a revision clash retries once, then `VersionConflict`.
- **Search query** (one statement; `:terms` from `query_terms`):
  ```sql
  SELECT * FROM (
    SELECT p.*, c.name AS category_name,
           cardinality(ARRAY(SELECT unnest(p.search_terms) INTERSECT SELECT unnest(CAST(:terms AS text[])))) AS score,
           max(...same expression...) OVER () AS best
    FROM catalogue_products p LEFT JOIN catalogue_categories c
      ON c.tenant_id = p.tenant_id AND c.id = p.category_id AND c.archived_at IS NULL
    WHERE p.tenant_id = :tenant AND p.archived_at IS NULL AND p.search_terms && CAST(:terms AS text[])
  ) ranked
  WHERE score = best
  ORDER BY sku COLLATE "C"
  LIMIT :limit
  ```
  `&&` uses the GIN index; `COLLATE "C"` gives the same order as Python's `sorted()` (F11).
- **Effective prices:** `DISTINCT ON (product_id)` over rows with `currency = :c AND price_list = 'standard' AND effective_from <= :at`, ordered by `effective_from DESC, revision DESC`; `effective_to` = `min(effective_from)` of later rows for the same key. Only active products.
- **Current stock:** `DISTINCT ON (product_id)` at `location_code = :location` ordered by `as_of DESC, recorded_at DESC`.
- **Categories in use:** non-archived categories with at least one active product, `ORDER BY name COLLATE "C"`.
- **Admin list:** filters `state`, `category_id`; `text` matches `lower(sku) LIKE %t%` or `lower(name) LIKE %t%` (trigram indexes; `%`, `_` escaped); keyset paging on `lower(sku) > :after`, page size 1-100 (default 50).
- **Referenced check (Q12):** any price or stock row, or the SKU in `quotation_versions.content->'lines'` (`@> '[{"sku": ...}]'`) or in `workflow_runs` outcome lines.
- **Seed helper** in `persistence/seed.py` (development/test only, owner role): `seed_local_catalogue(settings, tenant_id, catalogue: SimulatedCatalogue | Mapping)` copies the synthetic demo data (products, categories, price rows with their original dates, stock as of seeding time). It writes through the owner connection, so the "no back-dating" rule of the service does not apply to fixtures. Optional third demo business (Q20).

### 6.7 `LocalCatalogue` adapter
- **New** `src/sales_platform/catalogue/adapters/catalogue/local.py` (contract name):
  ```python
  class LocalCatalogue:
      source = "local-catalogue"
      def __init__(self, store: CatalogueReadStore, clock: Callable[[], datetime] = _utc_now,
                   location: str = DEFAULT_LOCATION) -> None: ...
      def search_products(self, context, query, limit=10) -> Sequence[Product]
          # INVALID_REQUEST for blank query or limit outside 1-50 (same as simulated)
          # terms = query_terms(query); empty terms -> []; rows -> Product(sku, name, unit, min, step, category)
      def list_categories(self, context) -> Sequence[str]
      def get_products(self, context, skus) -> Mapping[str, Product]          # active only, exact SKU
      def get_prices(self, context, skus, currency, effective_at) -> Mapping[str, PriceQuote]
          # require_aware(effective_at); PriceQuote(..., price_list_id="standard", price_list_version=str(revision),
          #   effective_to=next start, fetched_at=clock(), source=self.source, unit=row.unit)
      def get_availability(self, context, skus, warehouse=None) -> Mapping[str, StockLevel]
          # warehouse not None and != location -> {}; StockLevel(unit=row.unit, quantity, location, fetched_at=row.as_of)
  ```
- Every call wraps `CatalogueUnavailable` into `ErpError(TEMPORARY_UNAVAILABLE)` (retryable; the job queue already retries any failure, `application/jobs.py:64`).
- It never invents data: no price → absent; no stock row → absent; archived → absent.
- **Provider registration** (phase 1 registry): package `src/sales_platform/providers/local_catalogue/` with `key = "local_catalogue"`, `display_name = "Our product list"` (wording to confirm), `purposes = {catalogue}`, capabilities `catalogue_editable=True` (section 12), empty config model, `test()` = "store reachable", every messaging operation raises `OperationUnsupported`; entry point in `pyproject.toml`. **Adding another catalogue source later** (spreadsheet sync, Odoo, Shopify...) = another provider package implementing the same port and passing the same contract suite; the business picks it as its `catalogue` connection. No core change.

### 6.8 Contract and adapter tests (before the editor)
- `tests/unit/ports/test_erp_contract.py`: add factory `_local_in_memory()` using `InMemoryCatalogueStore` (a fake in root `tests/conftest.py`, seeded from `demo_catalogues`). Move the generic checks from `test_simulated.py` into the contract: invalid search (blank, limit 0, 51), categories sorted and tenant-scoped, search best-matches-only. Keep simulated-only checks (injected failure, unknown tenant) where they are.
- Make the contract checks reusable by PostgreSQL: expose them as a fixture-free helper (e.g. `ErpContractChecks` in root `tests/conftest.py`), then call them from `tests/integration/adapters/catalogue/test_local_catalogue.py` with `LocalCatalogue(SqlCatalogueStore(runtime_engine))` and `worker_engine`.
- `tests/unit/adapters/catalogue/test_local.py`: mapping, `fetched_at` rules (price = clock, stock = as of), `effective_to` = next start, archived hidden, category of an archived category = None, warehouse filter, store failure → retryable `ErpError`, limit and validation.

### 6.9 Application service
- **New** `src/sales_platform/catalogue/application/catalogue_admin.py` (contract name `ProductService`):
  ```python
  class ProductService:
      def __init__(self, store: CatalogueStore, policies: PricingPolicyReader,
                   sources: CatalogueSourceInfo, clock: Callable[[], datetime] = utc_now) -> None
      # reads: require VIEW_CATALOGUE (proposed)
      def summary(self, context) -> CatalogueSummary
      def list_products(self, context, query: ProductQuery) -> ProductPage
      def get_product(self, context, product_id: UUID) -> ProductDetail      # with current + scheduled price, current stock, stale flag
      def price_history(self, context, product_id) -> Sequence[PriceRecord]
      def stock_history(self, context, product_id) -> Sequence[StockRecord]
      def changes(self, context, product_id) -> Sequence[ChangeRecord]
      def list_categories(self, context) -> Sequence[CategoryRecord]
      # writes: require MANAGE_CATALOGUE and an editable source (Q18)
      def create_product(self, context, draft: ProductDraft) -> ProductDetail
      def update_product(self, context, product_id, changes: ProductChanges, expected_version: int) -> ProductDetail
      def archive_product(self, context, product_id, expected_version) -> ProductDetail
      def restore_product(self, context, product_id, expected_version) -> ProductDetail   # checks MAX_ACTIVE_PRODUCTS
      def delete_product(self, context, product_id) -> None                                # ProductReferenced -> 409
      def add_price(self, context, product_id, entry: PriceEntryDraft, command_id: UUID) -> PriceRecord
      def record_stock(self, context, product_id, entry: StockEntryDraft, command_id: UUID) -> StockRecord
      def create_category(...), rename_category(..., expected_version), archive_category(..., expected_version)
  ```
- `CatalogueSourceInfo` is a small port answering "which provider is this business's catalogue source and is it editable" from phase 1's `ConnectionStore` and provider capabilities (no provider name compared).
- Every product/category write records a `catalogue_changes` row in the same transaction (actor = `context.subject`). Price and stock rows are their own audit (append-only, with `created_by`/`recorded_by`).
- Replays: the same `command_id` with the same body returns the stored row; with a different body → `VersionConflict` (409).
- Tests: `tests/unit/application/test_catalogue_admin.py` with the in-memory store: each permission (viewer reads only; approver reads only; sales operator and administrator edit; intake service denied), read-only source refuses writes, version conflicts, limit reached, replay, audit rows, price normalised to minor units, unit change refused once priced.

### 6.10 API
- **New** `src/sales_platform/catalogue/api/catalogue.py`: `build_catalogue_router(auth: PortalAuth, products: ProductService) -> APIRouter`, prefix `/api/v1/tenants/{tenant_id}/catalogue`, same `Context` and `guarded()` pattern (F23); wired in `api/app.py`. Decimals travel as strings; datetimes as ISO 8601 with offset. Mutations need the CSRF header (existing session auth).

| Method and path | Permission | Body / query | Success | Errors |
|---|---|---|---|---|
| `GET /summary` | view | — | 200 `CatalogueSummaryOut` | 403 |
| `GET /products` | view | `q`, `category_id`, `state=active\|archived\|all`, `limit` (1-100, default 50), `after` | 200 `{items: ProductSummaryOut[], next_after}` | 403, 422 |
| `POST /products` | manage | `ProductIn` | 201 `ProductOut` | 403, 409 SKU exists / source not editable / limit, 422 `{field, message}` |
| `GET /products/{id}` | view | — | 200 `ProductOut` | 404 (also for another business's ID) |
| `PATCH /products/{id}` | manage | `ProductPatchIn` incl. `expected_version` | 200 `ProductOut` | 404, 409 version/unit locked, 422 |
| `POST /products/{id}/archive`, `/restore` | manage | `{expected_version}` | 200 | 404, 409 |
| `DELETE /products/{id}` | manage | — | 204 | 404, 409 `product_referenced` (archive instead) |
| `GET /products/{id}/prices` | view | — | 200 `PriceOut[]` (newest first, with `state`: current/scheduled/past) | 404 |
| `POST /products/{id}/prices` | manage | `{unit_price, effective_from?, note?, command_id}` | 201 `PriceOut` (200 on replay) | 404, 409, 422 |
| `GET /products/{id}/stock` | view | — | 200 `StockOut[]` | 404 |
| `POST /products/{id}/stock` | manage | `{quantity\|null, as_of?, note?, command_id}` | 201 `StockOut` (200 on replay) | 404, 409, 422 |
| `GET /products/{id}/changes` | view | — | 200 `ChangeOut[]` | 404 |
| `GET /categories` | view | — | 200 `CategoryOut[]` | 403 |
| `POST /categories` | manage | `{name}` | 201 | 409 name taken, 422 |
| `PATCH /categories/{id}`, `POST /categories/{id}/archive` | manage | `{name?, expected_version}` | 200 | 404, 409 |

Example `ProductOut`:
```json
{
  "product_id": "6c1d…", "sku": "IND-FAN-450", "name": "Industrial exhaust fan 450 mm",
  "description": null, "category": {"category_id": "a2f…", "name": "Exhaust fans"},
  "unit": "each", "minimum_quantity": "1", "quantity_step": "1",
  "archived_at": null, "row_version": 3,
  "current_price": {"price_id": "…", "unit_price": "4250.00", "currency": "INR", "price_list": "standard",
                    "revision": 2, "effective_from": "2026-01-01T00:00:00+05:30", "effective_to": null},
  "scheduled_price": null,
  "stock": {"quantity": "120", "unit": "each", "as_of": "2026-10-20T09:00:00+05:30", "source": "manual",
            "stale": false},
  "created_at": "…", "updated_at": "…"
}
```
`CatalogueSummaryOut`: `{source: {provider_key, display_name, editable}, active_products, priced_products, products_without_price, stale_stock, unknown_stock, max_active_products, units: ["box","each","kg","metre"], currency}`. `stale` uses the business's `max_stock_age` and `domain.catalogue_admin.stock_is_stale`, the same rule the calculator applies.

- Tests: `tests/unit/api/test_catalogue.py` (route shapes, codes, CSRF, decimals as strings, 404 for other tenants, 422 field names).

### 6.11 Setup checklist (phase 2 hook)
- Phase 2 owns the checklist API. Phase 3 adds the "Product data" step evaluator: done when the business's catalogue source is the local catalogue **and** `summary.priced_products ≥ 1` (active product with a current price in the business currency); for any other source, done when its connection test passed (phase 1). Shown, not blocking (Q19).
- Portal: the `business` feature reads it through `@/features/catalogue` index (`useCatalogueSummary`) and links to `#/catalogue`.

### 6.12 Portal feature `catalogue`
Follows `.docs/development/portal-architecture.md` and passes `portal/tests/architecture.test.ts` unchanged.

| Layer | Files | Contents |
|---|---|---|
| model | `model/types.ts` | `ProductSummary`, `Product`, `ProductForm`, `ProductPatch`, `Category`, `PriceEntry`, `PriceForm`, `StockEntry`, `StockForm`, `CatalogueSummary`, `ProductPage`, `ProductState`, `FieldProblem` |
| api | `api/catalogueRepository.ts` | One function per endpoint via `http`/`tenantPath`; no state |
| services | `services/productFormService.ts` | Pure checks mirroring the backend (SKU pattern, decimals, minimum multiple of step, unit list from summary); `toCreatePayload`, `toPatchPayload` (only changed fields) |
| | `services/priceService.ts` | Labels current/scheduled/past, "excluding tax", decimal display; `pricePayload` |
| | `services/stockService.ts` | "as of" text, stale badge text (server flag), "not tracked" |
| store | `store/catalogueFilters.ts` (Zustand) | Search text, category filter, state filter (client-only state) |
| hooks | `hooks/queryKeys.ts` | `["tenants", id, "catalogue", ...]` |
| | `hooks/useCatalogue.ts` | `useCatalogueSummary`, `useProducts` (infinite query with `after`), `useProduct`, `usePriceHistory`, `useStockHistory`, `useCategories` |
| | `hooks/useProductForm.ts`, `usePriceForm.ts`, `useStockForm.ts`, `useCategoryForm.ts`, `useArchiveProduct.ts` | Form state, `newCommandId()` per submit (replay-safe), 409 version conflict → reload message, 422 → field problems |
| ui/pages | `ProductListPage.tsx`, `ProductEditPage.tsx` (new and edit), `CategoriesPage.tsx` | Read hooks; render components |
| ui/components | `CatalogueSourceBanner`, `ProductFilters`, `ProductTable`, `ProductRow`, `ProductForm`, `CategorySelect`, `PricePanel` (current, scheduled, history, add price), `StockPanel` (current, as of, stale badge, history, add count), `ArchiveControls`, `CategoryList`, `ChangeHistory` | Buttons hidden without `can("catalogue:manage")` or when `summary.source.editable` is false |
| routes | `routes.tsx` | `catalogue`, `catalogue/new`, `catalogue/:productId`, `catalogue/categories` |
| index | `index.ts` | `catalogueRoutes`, `useCatalogueSummary`, `CatalogueSummary` type |

- App shell: one line in `app/router.tsx`; one nav item in `app/layout/TopBar.tsx`.
- Wording: plain English, money and quantities shown exactly as the API returns them (no float formatting).

### 6.13 Evaluation against the local catalogue
- `src/sales_platform/evaluation.py` needs no change (it takes any `ProductSource`).
- `tests/unit/test_evaluation.py`: parametrise the rules-interpreter run over `SimulatedErp` and `LocalCatalogue(InMemoryCatalogueStore)` seeded with `demo_catalogues`; both must pass 14/14 with 0 invented facts.
- `tests/integration/evaluation/test_local_catalogue_evaluation.py`: seed two fresh tenants in PostgreSQL with `seed_local_catalogue`, run `evaluate(RulesInterpreter(), LocalCatalogue(SqlCatalogueStore(worker_engine)), …)`; 14/14.
- `scripts/evaluate_extraction.py`: option `--source simulated|local` (default `simulated`); `local` uses the development database's third demo business (Q20). Model runs stay opt-in.

### 6.14 Documentation
| File | Change |
|---|---|
| `.docs/adr/0020-local-catalogue.md` (new) | Decision D18/D19, tables, fetched-at semantics, append-only prices/stock, search design, default source, alternatives (PostgreSQL language full-text with stemming; overwriting prices; fuzzy matching in `search_products`) |
| `.docs/adr/0003-erp-authority.md` | Status: "amended by 0020": the ERP stays authoritative when connected; otherwise the local catalogue is the business's chosen source |
| `.docs/adr/README.md` | Add 0020, mark 0003 amended |
| `.docs/features/product-catalogue.md` (new) | Purpose, screens, rules, API, failure behaviour, staleness, how a later source plugs in |
| `.docs/features/quotation-pricing.md` | Local source evidence (`standard`/revision, fetched-at meaning) |
| `.docs/architecture/data-model.md` | "Added in phase 3 (migration 0011)" table; update "Authoritative sources" |
| `.docs/architecture/integrations.md` | ERP operations: local source implemented; contract suite list |
| `.docs/development/code-map.md`, `configuration.md` (`SALES_DEFAULT_CATALOGUE_PROVIDER`), `testing.md`, `portal-architecture.md` (features table) | Same change |
| `.docs/plan/platform/05-product-data.md` | Align table names with the shared contract (`catalogue_prices`, `catalogue_stock`) |
| `.docs/handoff.md` | Work done, checks and results, open points, next action |

### 6.15 Live run (end of phase)
1. `scripts/check_database.py --cycle` green.
2. Start API, worker and portal. Sign up a new business (phase 2); confirm its catalogue source is "Our product list".
3. In the portal: create category "Exhaust fans", product `IND-FAN-450` (each), price 4250.00, stock 120 as of now. Checklist step turns done.
4. Send "Please quote 2 x IND-FAN-450" with the business's API key: quotation priced at 4250.00, evidence "standard r1", stock available.
5. Approve; change the price to 4400.00; accept as the customer → refused, version 2 drafted at 4400.00 (ADR 0016).
6. Enter stock with "as of" older than the max stock age → next quotation shows stock `stale` and stock review required.
7. Archive the product → a new enquiry gets the "not in our catalogue" reply with the categories that still have products.
8. Second business cannot see or fetch the first one's products (portal and API by ID).
9. Demo businesses still quote from the simulated ERP; their catalogue screen shows the read-only banner.

## 7. Search quality
### 7.1 Behaviour kept from today (must not change)
| Case (industrial demo data) | Expected result |
|---|---|
| `IND-FAN-450` | only IND-FAN-450 (4 shared words beat 2) |
| `IND-FAN` | IND-FAN-450, IND-FAN-600 (not every IND- product) |
| `ind-fan-450` (lower case) | IND-FAN-450 |
| `exhaust fans` | both fans (plural rule; also category words) → ambiguous question |
| `fasteners` | IND-BOLT-M8 (category word) |
| `M8 bolts` | IND-BOLT-M8 |
| `2-core cable` | IND-CABLE-2C (hyphen compound in name) |
| `pumps` | IND-PUMP-2HP (found, even without a price; pricing then blocks with `price_missing`) |
| `tata sierra car`, `hydraulic presses` | nothing → category reply |
| Office tenant: `IND-FAN-450` | nothing (other business) |
| Archived product SKU | nothing |
| `limit = 1` with two equal matches | first by SKU order |
| Blank query, limit 0 or 51 | `ErpError(INVALID_REQUEST)` |

These cases live in one table (`tests/unit/adapters/catalogue/search_cases.py` style data in root `tests/conftest.py`) and run against `SimulatedErp`, `LocalCatalogue` in memory and `LocalCatalogue` on PostgreSQL. A difference in any of them fails the build.

### 7.2 What the indexes add
- **Word index (GIN on `search_terms`)**: our full-text search. It uses the same language-neutral words as today instead of PostgreSQL's English stemming, because businesses are in several countries and stemming would change which products match (and the evaluation). This is a design choice for review (ADR 0020).
- **Trigram indexes (`pg_trgm`) on SKU and name**: fast "contains" search in the portal list (e.g. `fan-4`, `zinc`).
- **Later, only after Q15 is answered yes:** an optional port method `suggest_products(context, query, limit)` (trigram similarity above a threshold, used only when `search_products` finds nothing) that produces a "We could not find 'X'. Did you mean: ...?" question. Suggestions are never auto-matched, so F7's "one candidate = matched" rule cannot quote a typo.
- Size: 10,000 products per business; GIN indexes without `tenant_id` are fine at that size; `btree_gin` composite indexes are the next step if needed.

## 8. Test plan
| Level | Files (mirrored `tests/` tree) | What it proves |
|---|---|---|
| Domain unit | `unit/domain/test_catalogue_search.py`, `unit/domain/test_catalogue_admin.py`, `unit/domain/test_tenancy.py` | Tokeniser parity; every editing rule; price/stock selection; staleness; permissions |
| Adapter unit | `unit/adapters/catalogue/test_local.py`, `unit/adapters/erp/test_simulated.py` (still green after the tokeniser move) | Mapping, fetched-at rules, failure mapping, archived hidden |
| Contract | `unit/ports/test_erp_contract.py` (simulated + local in memory); `integration/adapters/catalogue/test_local_catalogue.py` (local on PostgreSQL, API and worker roles) | Same contract for every source |
| Search quality | Shared case table (7.1) on all three implementations | No matching drift |
| Application unit | `unit/application/test_catalogue_admin.py` | Permissions, versioning, replay, audit, limits, read-only source |
| API unit | `unit/api/test_catalogue.py` | Shapes, codes, CSRF, tenant 404 |
| Workflow unit | `unit/workflows/test_enquiry.py` (add a `LocalCatalogue` variant of the match/assess cases) | Same outcomes as with the simulated ERP |
| Architecture | `unit/test_architecture.py` unchanged; `portal/tests/architecture.test.ts` unchanged | Adapter does not import SQLAlchemy; portal layering |
| Migration | `integration/persistence/test_catalogue_migration.py` + `check_database.py --cycle` | 0011 upgrade, downgrade to 0010, upgrade again with data present; downgrade to empty and back; `alembic check` clean; extension and indexes exist |
| PostgreSQL RLS / isolation | `integration/persistence/test_catalogue.py` | Tenant A cannot read B's rows (all five tables); insert with B's `tenant_id` refused by `WITH CHECK`; product cannot point at B's category (composite FK); worker cannot insert or update; API cannot update/delete prices, stock or changes (privilege) and the owner is stopped by the trigger; SKU unique ignoring case; `4250.00` and `0.5` keep their decimals after a round trip; categories ordered like Python |
| PostgreSQL API | `integration/api/test_catalogue.py` | Real session + CSRF; two businesses; paging; concurrent price entries give revisions 1 and 2, never a duplicate |
| End to end (PostgreSQL) | `integration/workflows/test_local_catalogue_quotation.py` | Enquiry priced from the local catalogue; price change → acceptance refused and version 2; stale stock flagged; archived product → category reply |
| Evaluation | `unit/test_evaluation.py` (two sources), `integration/evaluation/test_local_catalogue_evaluation.py` | 14/14 on both sources, 0 invented facts |
| Portal | `portal/tests/features/catalogue/services/*.test.ts`; `ui/components/ProductForm.test.tsx`, `PricePanel.test.tsx`, `StockPanel.test.tsx`, `CatalogueSourceBanner.test.tsx`; `ui/pages/ProductListPage.test.tsx` (mocked repository, `renderWithProviders`) | Validation, payloads, permission-hidden buttons, read-only banner, version-conflict message, stale badge |
| Live run | Section 6.15 | Real processes, real database |
| Truncation | `tests/integration/conftest.py` `_INTAKE_TABLES` gains the catalogue tables (`TRUNCATE` does not fire the row triggers) | Clean state per test |

## 9. Risks
| Risk | Mitigation |
|---|---|
| Matching drifts between the SQL query, the in-memory fake and the simulated ERP | One tokeniser in the domain; one shared case table run on all three; evaluation on both sources |
| A weak or fuzzy hit is quoted automatically (F7) | `search_products` keeps "best word matches only"; fuzzy only as suggestions after Q15 |
| Decimal scale changes cause false "prices changed" at acceptance or odd PDFs (F13, F14) | Unconstrained `numeric` + `scale()` checks; price normalised once at entry; round-trip tests |
| Typed stock is always stale with today's 5-minute demo policy | Q10 default for new businesses; summary shows how many products have stale stock |
| `alembic check` false differences for expression/GIN indexes | Declare in models; if needed, exclude only those names with a comment and test their existence |
| `pg_trgm` missing on a hosted database | It is a trusted, standard extension on PostgreSQL 13+ and main cloud providers; checked in the migration test and in the hosting decision (D24) |
| Phase 1/2 shapes differ from the assumptions in section 12 | Phase 3 starts only after those are agreed; changes go into the shared contract |
| Archiving a product with open quotations surprises staff | Product page shows "used in N open quotations" before archiving (from the referenced check); behaviour per Q11 |
| Changing the tokeniser later leaves old `search_terms` | A small owner script recomputes `search_terms` for all products; noted in the feature doc |
| Edits made while the business uses another source never reach quotations | Read-only screens unless the source is editable (Q18) |
| Concurrent edits overwrite each other | `row_version` checks on products/categories; append-only prices/stock with unique revision and `command_id` |

## 10. Definition of done
- Every Q1-Q23 answered and recorded; section 12 agreed and copied into `00-shared-contracts.md`.
- Migration 0011 upgrades, downgrades and re-upgrades with data; `alembic check` clean; `check_database.py --cycle` green.
- `LocalCatalogue` passes the ERP contract suite in memory and on PostgreSQL; search case table green on all three implementations.
- New businesses get the local catalogue by default; demo businesses still use the simulated ERP; all earlier tests unchanged and green (unit, PostgreSQL, portal, evaluation).
- Portal: list with search, filter and paging; add/edit product; categories; prices with history; stock with "as of" and stale badge; archive/restore/delete-if-unused; permissions respected.
- RLS and grant tests cover every new table, both runtime roles, and the owner trigger.
- Evaluation 14/14 with the rules interpreter against both sources.
- Live run (6.15) done and recorded in the handoff with evidence.
- ADR 0020, ADR 0003 amendment, feature doc and all listed docs updated.
- No commit, push or pull request without the user's explicit permission (AGENTS.md).

## 11. Rough size
About the size of one past milestone (M5/M6), for one developer, not a promise:

| Part | Size |
|---|---|
| Domain rules, tokeniser move, permissions, ports | S (1-2 days) |
| Migration 0011, models, SQL store, seed, RLS/migration tests | M (2-3 days) |
| `LocalCatalogue`, provider registration, default source, contract + search tests | M (1-2 days) |
| `ProductService` + API + tests | M (2 days) |
| Portal feature + tests | L (3-4 days) |
| Evaluation, end-to-end tests, live run, docs | M (1-2 days) |
| **Total** | **about 10-15 working days**, after phases 1 and 2 |

## 12. Proposed changes to shared contracts
**Merged (2026-10-26):** these proposals are now part of [00-shared-contracts.md](00-shared-contracts.md) revision 2; where they differed, the contracts file wins (e.g. one name, `catalogue_reader`, for the catalogue factory method). Points needing the user's decision are listed there in §8.

These are deviations from or additions to [00-shared-contracts.md](00-shared-contracts.md). They are proposals; if agreed, they are written into that file first.

| # | Contract section | Proposal | Why |
|---|---|---|---|
| C1 | §4 `0011_catalogue` | Add table **`catalogue_changes`** (append-only audit of product and category edits) | The scope asks for an audit trail; prices and stock audit themselves, but products and categories are edited in place |
| C2 | §6 permissions | Add **`catalogue:view`** (administrator, sales_operator, approver, viewer) for reading the catalogue; `catalogue:manage` unchanged | Reading should not need edit rights, and no existing permission fits (Q14). Alternative if rejected: reuse `quotations:view` |
| C3 | §2 module layout | Add **`domain/catalogue_search.py`** (shared tokeniser). `ports/catalogue_admin.py` holds both `CatalogueStore` and a read port `CatalogueReadStore`; `adapters/catalogue/local.py` depends on that port and `persistence/catalogue.py` implements it | The architecture test forbids SQLAlchemy in adapters (F18); this keeps the contract's file names without loosening the rule |
| C4 | §3 Provider contract | A catalogue provider needs a way to build its reader, e.g. `catalogue_reader(conn: Connection, secrets: SecretStore) -> ProductSource` (raises `OperationUnsupported` for non-catalogue providers), and the registry must hand providers their dependencies (for `local_catalogue`: the store bound to the role's engine) | The contract's Provider protocol has messaging operations only; phase 1 resolves "the ProductSource per business" but the shape is not fixed yet |
| C5 | §1 `Capabilities` | Add `catalogue_editable: bool` | The portal and service decide "read-only" by capability, never by provider name (generic integrations rule) |
| C6 | §4 `0010_accounts` | `create_business(...)` gains `default_catalogue_provider text`; 0011 replaces the function body and backfills a `local_catalogue` connection for tenants without one. New setting `SALES_DEFAULT_CATALOGUE_PROVIDER` (default `local_catalogue`) | A new business must have a catalogue source from its first minute, created atomically with the business |
| C7 | §4 `0008_connections` (assumption to confirm) | Phase 1's backfill gives both demo tenants an enabled connection with purpose `catalogue` to the simulated ERP | Otherwise 0011's backfill would switch the demo tenants to the empty local catalogue |
| C8 | §1 table list | The vocabulary row "Our product tables" should also list `catalogue_categories` (already in the §4 migration row) | Consistency |

### Contradictions found between documents
- **Table names:** [05-product-data.md](../05-product-data.md) names `price_lists`, `price_list_items`, `stock_levels`; the binding shared contract names `catalogue_prices`, `catalogue_stock`. This plan follows the contract and keeps the price list as columns (`price_list`, `revision`); 05 should be aligned (6.14).
- **Who manages the catalogue:** [02-accounts-and-onboarding.md](../02-accounts-and-onboarding.md) proposes `catalogue:manage` for administrators only; the shared contract grants it to administrator **and** sales_operator. This plan follows the contract; 02 should be aligned.
- **Archive and attributes:** 05 lists `attributes (JSON)` on products; it is left out here (no screen uses it) and can be added by a later migration without breaking anything.

## 13. Forward compatibility (later phases plug in without rewrites)
| Later feature | How it fits |
|---|---|
| Spreadsheet import (A) | New `catalogue_imports` table (file, mapping, row errors, status, who, when). An import writes products, prices (`source = import`) and stock counts through the same store in one transaction per file; `LocalCatalogue` is unchanged. The `source` checks gain `import` in that migration |
| ERP or shop live connector (C) | Another provider package with purpose `catalogue` implementing `ProductSource`, passing the same contract suite. The business switches its catalogue connection; local tables stay as they are (read-only banner) |
| ERP/shop sync into our tables (D) | Products gain `external_id`/`connection_id`; rows use `source = sync`; price `fetched_at` = sync time so the policy's max price age applies to copies |
| Our own inventory (E) | `stock_movements` (receipts, sales, adjustments, transfers) and `reservations` reference `catalogue_products`; current `catalogue_stock` rows become counts; `location_code` becomes a FK to a `locations` table. `get_availability` computes count + movements − reservations; the port is unchanged |
| Customer-specific prices, discounts | More price lists (`price_list` other than `standard`) and a customer reference on price rows; `get_prices` gains customer context in a port version change (integrations.md already lists `customer_context` as deferred). Not built now |
| Promotions with end dates | Add `effective_to` to `catalogue_prices`; the selection rule already handles end dates in `PriceQuote` |
| Synonyms (D20) | A per-business `catalogue_synonyms` table expanding `query_terms`; the search query is unchanged |
| Semantic search (M10) | An extra candidate finder behind `suggest_products`; the final match still comes from the source's own products |
