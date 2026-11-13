# Product catalogue (phase 3)

## What it does
A business without an ERP types its products, categories, prices and stock counts into the portal. Enquiries are then matched, priced and accepted from that list exactly as from an ERP: the quotation core does not know the difference.

- **Product source:** each business has one active catalogue connection (ADR 0018). A new business starts with **Our product list** (provider `local_catalogue`). Every demo business has seeded demo products in its own list (`seed.DEMO_CATALOGUES`).
- **Read-only elsewhere:** when the source is not editable in the portal (an ERP, the simulated demo), the screens show "Products come from <source>" and refuse changes.

## Rules (user answers Q1-Q23)
| Topic | Rule |
|---|---|
| Product | SKU, name and unit required; category and staff notes optional; minimum quantity and step default to 1 |
| SKU | 1-64 letters, digits or `- _ . /`, starting with a letter or digit; unique per business ignoring case; never changes (archive and add a new product instead) |
| Unit | `each`, `metre`, `kg` or `box` (the units the enquiry reader understands). Locked once a price or stock count exists |
| Quantities | Up to 3 decimals; the minimum must be a multiple of the step |
| Price | Greater than 0, excluding tax, in the business currency, up to 6 decimals, stored with at least the currency's minor units (4250 → 4250.00). Starts now or at midnight (business time zone) on a later date; never in the past. Each change is a new revision; nothing is overwritten. No end dates (promotions come later) |
| Stock | Optional; one location (`MAIN`); a count "as of" a time (not in the future); "not tracked" = unknown. Stock never blocks a quotation |
| Old counts | A count older than the business's max stock age is "stale": approval asks for a stock check. New businesses get 7 days |
| Archive | Archived products are no longer sold: not found by search, not priced, their category not listed. Accepting an open quotation with one is refused and revised; the revision then cannot be priced ("pricing failed") and goes to a sales operator |
| Delete | Only a product never priced, counted or quoted; otherwise archive |
| Limit | 10,000 active products per business |
| Who | Everyone in the business can see the list; administrators and sales operators change it. Nothing changes while the business is suspended |

## What the quotation shows
- Each line's price evidence: price list `standard`, version = revision number, source `local-catalogue`. The price's "fetched at" is the moment it was read, because our row is the source.
- Each line's stock evidence: the count's "as of" time. A typed count therefore ages, unlike a live ERP reading.
- A price change after a quotation was sent: the customer's acceptance is refused and version N+1 goes to a manager (ADR 0016).

## Search
Customers' words are matched like this: the SKU and its hyphen parts, the name and the category; simple plurals ("fans" → "fan"); only the products sharing the most words. The staff notes are not searched. Typos are not guessed ("did you mean" suggestions need the user's agreement first). The portal's search box finds any part of a SKU or name.

## API (`/api/v1/tenants/{t}/catalogue`, session + CSRF)
| Route | Who | What |
|---|---|---|
| `GET /summary` | all staff | source (name, editable), counts, units, currency, limits, time zone |
| `GET /products` | all staff | `q`, `category_id`, `state` (active/archived/all), `limit` 1-100, `after` (paging); each item with its current price and stock |
| `POST /products`, `PATCH /products/{id}` | editors | 422 `{field, message}`; 409 `{code}`: `sku_taken`, `version_conflict`, `limit_reached`, `source_not_editable` |
| `POST /products/{id}/archive`, `/restore`, `DELETE /products/{id}` | editors | `{expected_version}`; delete 409 `product_referenced` |
| `GET/POST /products/{id}/prices`, `GET/POST /products/{id}/stock` | all / editors | POST needs a `command_id`; the same ID again returns the stored entry (200 instead of 201). 409 `currency_not_set`, `product_archived` |
| `GET /products/{id}/changes` | all staff | who changed what |
| `GET/POST /categories`, `PATCH /categories/{id}` | all / editors | rename or archive (`expected_version`); 409 `category_name_taken` |

Numbers travel as strings; times as ISO 8601 with an offset. Another business's IDs give 404.

## Portal (`#/catalogue`, top bar "Products")
- **Product list:** active/archived/all, search, category filter, "Show more" paging; price and stock per product, with "No price" and "Old count" badges.
- **Product page** (`#/catalogue/new`, `#/catalogue/<id>`): details form; price panel (current, scheduled, history, add a price now or from a date); stock panel (latest count, as-of time, stale badge, history, record a count or "not tracked"); archive/restore/delete; change history.
- **Categories** (`#/catalogue/categories`): add, rename, archive.
- **Setup checklist:** the "Products" step is done when at least one active product has a price; it is not needed to go live.

## Failure behaviour
- The catalogue tables cannot be read: the product source reports a temporary failure and the job retries (never an empty or invented result).
- Two people edit the same product: the second save gets "Someone else changed this product" (row version).
- A lost answer to "Add price" retried with the same request ID stores one price.

## Later (designed to plug in, not built)
Spreadsheet import, ERP/shop connectors and sync, stock movements and reservations, customer prices and discounts, promotions with end dates, synonyms, semantic search, product images. See the plan's §13.

## Development
- Demo: sign in as `sales@local.example` (dev login or demo password) and open **Products**.
