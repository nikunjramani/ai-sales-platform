# 0016: Customer acceptance through the API, with validity, price re-check and automatic revision
Date: 2026-10-23
Extended by [0021](0021-customers-conversations-and-website-chat.md): Customers may also answer with a chat button or a secure link (phase 4, ADR 0021).
Status: accepted; implemented in M6 (commit f545201, [CI run 39907493342](https://github.com/nikunjramani/ai-sales-platform/actions/runs/39907493342) passed)
Changes the M6 scope in the [roadmap](../plan/roadmap.md); builds on [0013](0013-portal-identity-and-approval-core.md) and [0015](0015-quotation-documents-and-delivery.md)
Extended by [0018](0018-connector-framework.md) (phase 1): notices to managers go through the outbox (one per response and account, never repeated on retry); prices come from each business's own product source.

## Context
After M5, an approved quotation is delivered to the API client that sent the enquiry. M6 lets that customer answer it. The user answered every open point on 2026-10-23, using the [clarify-before-building](../../.claude/skills/clarify-before-building/SKILL.md) procedure. Each decision below is the user's, not an assumption.

## Decision
**Scope:** M6 covers customer acceptance only. It creates no ERP sales order and makes no stock reservation; both move to a later milestone.

| Topic | Decision (user, 2026-10-23) |
|---|---|
| How customers answer | Through the intake API, with the same business's API key. The customer names the exact quotation version and content SHA-256 it answers. No acceptance link or staff-recorded acceptance in M6 |
| Answers | Accept, or decline with an optional reason. No purchase order number |
| Validity | A per-business "valid for N days" setting, default **7**, counted **from delivery** (when the approved PDF became available or was sent). "Valid until" is printed on the PDF and returned by the API |
| Re-check at acceptance | The lines are re-priced from the ERP with the M1 calculator. **Any** difference in unit price, line total, subtotal, tax, total or currency refuses the acceptance |
| On a change | Refuse and revise automatically: the system drafts the next version at current prices (system-created, so a manager may approve it). It goes through normal manager approval and is delivered when approved. The customer can then accept the new version |
| Stock | Ignored for acceptance; shown to staff only |
| ERP unavailable | Nothing is recorded; the customer gets "temporarily unavailable, retry" |
| Expiry | After "valid until" the quotation is `expired` and stays closed. The customer must send a new enquiry |
| Withdrawal | A sales operator or approver can withdraw an issued, unanswered quotation with a reason. The customer then sees `withdrawn` |
| Staff information | Portal status (Accepted, Declined, Expired, Withdrawn, with a list filter), plus a short message to managers' linked channels (today the simulated channel) |
| Record | Responses are append-only and include the API key subject, time, version and content hash. A replay returns the original; a different answer to an answered quotation is refused |

## Alternatives (offered and not chosen)
- **Acceptance link or staff-recorded acceptance:** not in M6.
- **No expiry, or one fixed period for everyone:** rejected for a per-business setting.
- **Price tolerance, or honouring the quoted price:** rejected; any change refuses.
- **Stock rules** ("must be in stock", "worse than quoted"): rejected; stock is ignored.
- **Renewing expired quotations:** rejected; expired stays closed.
- **Reservation:** deferred with orders.

## Consequences
- A new tenant setting, quotation statuses for issued, accepted, declined, expired and withdrawn quotations, an append-only response table, and a validity date per delivery.
- The approval graph gains a way to continue after acceptance is refused because prices changed. The new version follows the existing approval and delivery path.
- Orders, reservation and the ERP write contract remain open for a later milestone (open decisions).
- **Implementation details** (no business rule added):
  - "From delivery" is the moment the PDF is prepared for delivery, because the date must be printed on it. A push delivery retried later keeps that date.
  - Withdrawal checks the two roles, so administrators cannot withdraw.
  - If the revised version can no longer be priced, the M4 rule applies: `pricing_failed`, and a sales operator revises.
  - The approval graph keeps the name `quotation-approval-v1`, because steps were only added after `approved`.

## Verification
- **Unit:** domain, service, workflow and API over in-memory fakes, with a controllable clock for expiry.
- **PostgreSQL integration:**
  - accept over HTTP;
  - a price change through to a version 2 approval task;
  - an accept/withdraw race recording exactly one answer;
  - expiry by the worker role;
  - append-only answers and tenant isolation.
- **Migrations:** the full suite twice around a downgrade to empty, and a 0007 -> 0006 -> 0007 round trip with an accepted quotation.
- **Live run:** real API and worker processes (see setup).
