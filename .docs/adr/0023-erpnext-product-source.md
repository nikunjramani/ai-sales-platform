# 0023: ERPNext as the first real product source: a synced copy for search, live prices and stock
**Superseded by [ADR 0027](0027-remove-erp-integration.md)** (removed 2026-11-03).

**Date:** 2026-10-29. **Status:** accepted (phase 6); implemented on `feat/phase-6-7-erp-and-orders`, verified against a local ERPNext v16 sandbox (synthetic data). No pilot business yet (user answer): the pilot questions of the plan (§5.6) stay open and are settings, not code. Extends ADR 0003 (ERP authority), ADR 0010 (exact money and units), ADR 0018 (connector framework) and ADR 0020 (our catalogue; its "ERP becomes the authority" amendment is implemented here).

## Context
Phase 6 needed decision D21 (which system). On 2026-10-29 the user chose:
- **ERPNext**, **sandbox only** (no pilot business yet), run as a **local Docker** sandbox;
- the **hybrid** read mode;
- the sync interval **configurable per business**;
- "in stock" as a **setting, default available** (on hand minus reserved);
- **tax-inclusive prices refused**;
- the **chosen warehouses, summed**;
- **enabled sales items of chosen item groups**;
- **one selling price list**;
- phases 6 and 7 in **one PR**.

For everything else in phases 6 and 7 the user said: "for this specific phase you will answer all questions, don't ask me". Claude's choices are marked **(Claude)** here and in ADR 0024, so the user can change them in review.

The spike on the sandbox (plan §5.1, all five checks passed):
1. Products, prices and stock read by item code through the Frappe REST API.
2. "Available" is the Bin's `actual_qty − reserved_qty`, and a submitted sales order raises `reserved_qty`.
3. Our reference fits the Sales Order's `po_no` ("Customer's Purchase Order") and is found by a list filter.
4. A second create with the same reference returns the existing order. The writer looks the reference up first: ERPNext refuses a duplicate `po_no` per customer only under a setting the business may change.
5. Rate limits are off by default on self-hosted Frappe; 429 is mapped when a host adds them.

The accepted rate was kept with `ignore_pricing_rule` (4100 against a list price of 4250).

## Decision
1. **Provider package `adapters/providers/erpnext/`** (entry point `erpnext`), purposes catalogue and orders. One-click Enable shows one form, the JSON schema of `ErpnextForm`, with enums inlined:
   - the address and an API user's key and secret (kept in the SecretStore);
   - price list, warehouses, stock meaning, item groups, tax flag and unit renames;
   - the sync interval (5–1440 minutes, default 15);
   - the order settings (ADR 0024).
   Sending the form makes harmless reads: the logged user, the price list (it must be a selling list), the warehouses, and the customer when orders are written. **(Claude)** In production only public `https://` addresses are accepted, so the platform never calls internal hosts. To change a setting, Disable and Enable again.
2. **Capabilities** (contracts §1): `CatalogueCapabilities(editable, sync, live_prices, live_stock, warehouses, webhooks)` and `OrderCapabilities(create_order, reference_lookup, idempotency_key, reserve_stock, order_status)` on `Capabilities.catalogue` / `.orders`. The core decides by these flags only. The sync interval is the generic config key `sync_minutes`.
3. **Hybrid reader** (`ErpnextCatalogue`):
   - `search_products`, `get_products` and `list_categories` read **our synced copy** (`LocalCatalogue` over the catalogue tables).
   - `get_prices` and `get_availability` ask **ERPNext live**: general selling prices of the chosen list, in force at the moment, in the product's own unit and the business currency only. Unknown SKUs are absent; non-stock items have unknown stock (`None`); a stock item without a Bin is 0. If ERPNext is unreachable the drafting job retries, as with any ERP.
4. **Sync** (`sync_catalogue` job, `application/catalogue_sync.py`, `persistence/catalogue_sync.py`, migration `0014_catalogue_sync`):
   - **(Claude)** A **full** feed every run, not "changed since", so deletions are never missed.
   - Products are matched by SKU (ignoring case). They are created with source `sync`, updated, restored or adopted (a typed product with the same SKU becomes the copy), or **archived** when no longer offered.
   - Prices and stock counts are added only when they changed. Prices are stored in the business currency only; any other currency is an issue. Copied stock is kept at our default location.
   - Each run is recorded (`catalogue_sync_runs`: counts, up to 200 issues, error). A run left "running" by a dead worker is closed as interrupted. Every failure keeps the last good copy and the schedule goes on (the job itself succeeds).
   - "Sync now" queues one manual run at most.
5. **Mapping** (`domain/catalogue_sync.py`):
   - Units are renamed exactly, never converted: the connection's own `CODE=unit` renames first, then the unit words the enquiry reader already knows (`Nos` → each). Anything else is not copied and is reported.
   - Unit codes ERPNext marks "must be whole number" step by 1; others by 0.001.
   - A negative ERP balance means 0 available.
   - Item groups include their sub-groups.
   - **(Claude)** Item Price dates start at 00:00 UTC; descriptions (HTML in ERPNext) are not copied.
6. **One product source per business, switched safely** (generic, `application/lifecycle.py` and `persistence/provider_state.py`). A connection becoming active for an exclusive purpose (catalogue, orders) first switches off the active one that has it (event reason `replaced`). Disable switches the replaced ones back on if nothing else took their place. The first copy is queued in the same transaction.
7. **Portal**: the product list shows the copy (last run, what changed, what was not copied, next run, "Sync now") and stays read-only. Setup forms are prefilled with their schema defaults (generic).

## Alternatives considered
- **Live only:** quoting fails whenever ERPNext is slow or capped, and the vendor's search is weak.
- **Sync only:** prices and stock would be as old as the last copy. Hybrid keeps our search and fresh facts.
- **Incremental sync:** faster, but deletions would need a separate full pass. Full feeds are simple at pilot sizes (refused above 10,000 products).
- **Zoho, Shopify, Odoo, Tally:** see the plan's comparison. ERPNext has a free local sandbox and a reference field for orders.

## Consequences
- A second ERP or shop is a new package with purposes and capabilities; nothing in the core changes.
- Our catalogue tables now hold copies too; the worker may write them (column-level grants in 0014).
- Prices on quotations come from ERPNext even when the copy is older; the copy is for search and the screens.
- Open (pilot questions, plan §5.6): customer-specific prices, tax-inclusive lists, per-customer warehouses, freshness needs, product count, data location.

## Verification
- Unit tests: package tests over a fake Frappe API (`tests/unit/adapters/providers/erpnext/`), mapping and feed rules, sync handler, lifecycle, provider contract.
- PostgreSQL tests: sync store, grants, replace/restore, migration round trip.
- Sandbox: `test_erpnext_sandbox.py` and the writer contract (integration marker) against `infra/erpnext/compose.yaml` seeded by `scripts/seed_erpnext_sandbox.py`.
