# 0003: Existing ERP remains authoritative
**Partly superseded by [ADR 0027](0027-remove-erp-integration.md):** our own product list is the authority until an ERP is chosen.

Date: 2026-10-22
Status: accepted; partially implemented (M1: ProductSource port, simulated ERP, shared contract suite). Real sandbox adapter pending (M7). **Amended by [0020](0020-local-catalogue.md) (2026-10-28):** a business without an ERP keeps its products in our own catalogue, which is then its authoritative source; when an ERP is connected, the ERP is authoritative and our list is read-only.

## Context
Businesses already manage stock and pricing elsewhere.

## Decision
Read current business facts through ERP adapters. Store immutable quote evidence and optional disposable caches, not a replacement stock ledger. Use a simulated ERP for development and a real sandbox before release.

## Alternatives considered
Require catalogue/stock migration; let AI estimate facts; treat cached stock as a reservation.

## Consequences
Vendor mappings and capabilities vary; unsupported or unavailable writes require explicit exception paths.

## Verification
Same ERP contract suite for simulated and real implementations; stale data and unknown-price tests.
