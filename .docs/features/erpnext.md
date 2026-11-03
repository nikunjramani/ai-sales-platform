# ERPNext product source (phase 6)
**Status and milestone:** implemented on `feat/phase-6-7-erp-and-orders` and verified against a local ERPNext v16 sandbox with synthetic data. No pilot business yet. [ADR 0023](../adr/0023-erpnext-product-source.md).

**Business purpose:** a business that keeps its products in ERPNext quotes from ERPNext. Prices and stock are never retyped, and quotations always use ERPNext's current price and stock.

**Terms in simple language**
- *Product source:* where a business's products come from (our own list, the demo, or an ERP). A business has exactly one.
- *Sync (copy):* every N minutes the worker copies ERPNext's sellable items into our catalogue tables. Customers' words are matched against this copy.
- *Live check:* when a quotation is priced, the price and stock of the matched products are asked from ERPNext itself.
- *Available stock:* on hand minus what existing sales orders reserved (or on hand only, if the business chose that).

**Actual source files/symbols**

| What | Where |
|---|---|
| Package (provider, form, client, reader and feed, order writer) | `adapters/providers/erpnext/` (`ErpnextProvider`, `ErpnextForm`, `FrappeClient`, `ErpnextCatalogue`, `build_feed`, `ErpnextOrderWriter`) |
| Feed, mapping and run records | `domain/catalogue_sync.py` |
| Sync port and SQL store | `ports/catalogue_sync.py`, `persistence/catalogue_sync.py` (`SqlCatalogueSync`) |
| Job handler and service | `application/catalogue_sync.py` (`CatalogueSyncHandler`, `CatalogueSyncService`) |
| One source per business | `application/lifecycle.py` (`EXCLUSIVE_PURPOSES`), `persistence/provider_state.py` (`_replace_others`, `_restore_replaced`) |
| API | `GET/POST /api/v1/tenants/{t}/catalogue/sync` in `api/catalogue.py` |
| Portal | `features/catalogue/ui/components/CatalogueSyncPanel.tsx`; prefilled setup forms in `features/connections` |
| Migration | `0014_catalogue_sync` |

**Entry point and caller:**
- An administrator presses Enable on ERPNext under Connections and fills the form. The form is checked and the connection becomes active and the business's product source (the previous one is switched off).
- The first `sync_catalogue` job is queued at the same moment.
- Each run queues the next. "Sync now" on the product list queues one extra.

**Inputs and validation:**
- Required: the ERPNext address (https in production, public hosts only), API key and secret, the selling price list and the warehouses that count.
- Optional: stock meaning, item groups (empty = all; sub-groups included), "prices include tax" (then no prices are used), unit renames `CODE=unit`, interval 5–1440 minutes, and the order settings ([orders](orders.md)).

**Execution steps and state changes (one sync run)**
1. The run is recorded as running.
2. The worker reads, through the API user: Price List, enabled sales Items without variants (of the chosen groups), UOM, general selling Item Prices, and the Bins of the chosen warehouses.
3. Items are mapped to our words. A unit without a known name, or an SKU we cannot store (spaces), is skipped and listed as an issue.
4. One transaction applies the feed: products created, updated, adopted, restored or archived; prices and stock counts added only on change; change records; the run's counts and issues; the next scheduled job.

**Outputs:** our catalogue copy (source `sync`, read-only in the portal), a run record, and the quotation lines' evidence `price_source = erpnext` with the price list and the Item Price's `modified` as version.

**Tenant/auth boundary:**
- Every row is scoped to the business (forced RLS). The API key belongs to one business's connection and lives in the SecretStore.
- Reading the sync status needs `catalogue:view`; "Sync now" needs `catalogue:manage`; Enable and Disable need `integrations:manage`.

**External effects and idempotency:**
- Sync only reads ERPNext. A repeated run with the same data adds nothing.
- Order writes are described in [orders](orders.md).

**Failures/recovery:**
- An unreachable ERPNext, a refused key or a renamed price list makes the run fail with the error shown. The last good copy is kept and the schedule goes on.
- A dead worker's run is closed as interrupted at the next start.
- Live checks that fail make the drafting job retry (never a guessed price).
- Disable switches the previous product source back on.

**Configuration and secrets (names only):** connection secrets `api_key` and `api_secret`. Config `erpnext.*` and the generic `sync_minutes`. Test-only environment variables: `SALES_TEST_ERPNEXT_URL`, `_KEY`, `_SECRET`, `_WAREHOUSE`, `_CUSTOMER`, `_SKU`.

**Tests and reproducible example:**
- Unit: `tests/unit/adapters/providers/erpnext/test_erpnext.py` (fake Frappe API), `tests/unit/domain/test_catalogue_sync.py`, `tests/unit/application/test_catalogue_sync.py`.
- PostgreSQL: `tests/integration/persistence/test_catalogue_sync.py`.
- Sandbox: [provider setup, ERPNext](../operations/provider-setup.md#erpnext) and `tests/unit/adapters/providers/erpnext/test_erpnext_sandbox.py`.

**Known limitations:**
- One price list, with no customer-specific prices.
- Tax-inclusive lists are not used.
- Item Price days start at 00:00 UTC.
- Settings change only by Disable and Enable again.
- No webhooks: changes arrive with the next run.
- Descriptions are not copied.
- Pilot questions (plan §5.6) are open.

**Related ADRs:** 0003, 0010, 0018, 0020, 0023, 0024.
