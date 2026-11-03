# Customer acceptance (M6)
**Status:** M6 is complete: verified locally (unit, PostgreSQL integration, migration cycles, live run) and in [CI run 39907493342](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39907493342). Every rule below was decided by the user on 2026-10-23 ([ADR 0016](../adr/0016-customer-acceptance.md)). M6 creates no ERP order and reserves no stock; both come in a later milestone.

**Business purpose:** the customer can say yes or no to the exact quotation they received. Nothing is accepted at a price that no longer holds. Staff can see every answer and can take back a quotation sent by mistake.

## Terms in simple language
- **Issued quotation / offer:** an approved version whose PDF reached the customer (M5). It can be answered until **valid until**.
- **Valid until:** when the PDF is prepared for delivery, plus the business's `quote_validity_days` (default 7). It is printed on the PDF.
- **Answer:** accept, or decline with an optional reason. The answer names the version and its content fingerprint (SHA-256), so it can never apply to different content.
- **Re-check:** when the customer accepts, the lines are priced again from the ERP. If any amount differs, the acceptance is refused and a revised version goes to a manager.
- **Withdrawal:** a sales operator or approver closes an unanswered quotation, with a reason.
- **Expiry:** at valid until, an unanswered quotation closes. It is not renewed; the customer sends a new enquiry.

## Customer statuses
| Status | Meaning |
|---|---|
| `open` | Can be accepted or declined |
| `accepted` / `declined` | Answered (closed) |
| `expired` | Validity ended without an answer (closed) |
| `withdrawn` | Withdrawn by staff (closed) |
| `being_revised` | Prices changed at acceptance; a revised version is with a manager or a sales operator |
| `closed` | The manager rejected the revised version |

## Source files
- **Domain:** [domain/acceptance.py](../../src/sales_platform/quotations/domain/acceptance.py) holds statuses, outcomes, `price_differences`, `valid_until`, `customer_status` and the errors.
- **Ports:** [ports/acceptance.py](../../src/sales_platform/quotations/ports/acceptance.py) (`ResponseStore`). Notices to managers are outbox messages (template `response_notice`, one per response and account; phase 1, [connections](connections.md)).
- **Application:** [application/acceptance.py](../../src/sales_platform/quotations/application/acceptance.py) holds `QuotationResponseService` (respond, withdraw, expire), `QuotationExpiryHandler` and `ResponseNotificationHandler`.
- **Workflow:** the `await_customer` and `apply_customer` steps in [workflows/quotation.py](../../src/sales_platform/quotations/workflows/quotation.py).
- **Persistence:** [persistence/acceptance.py](../../src/sales_platform/quotations/persistence/acceptance.py) and [0007_acceptance.py](../../migrations/versions/0001_baseline.sql). The expiry job is queued in [persistence/documents.py](../../src/sales_platform/quotations/persistence/documents.py).
- **API:** [api/enquiries.py](../../src/sales_platform/customer_channels/api/enquiries.py) (customer) and [api/portal.py](../../src/sales_platform/quotations/api/portal.py) (staff).
- **Portal:** `CustomerPanel`, `customerService.ts`, `useCustomerAnswer.ts` and new list filters.

## Flow
1. **Delivery (M5):** the PDF is prepared with valid until printed on it. When the delivery is `available`/`sent`, an `expire_quotation` job is queued to run at valid until, in the same transaction. The approval run pauses at `await_customer`.
2. **Customer answers:** `POST /api/v1/enquiries/{id}/quotation/response` with an `Idempotency-Key`.
   1. A replay with the same key returns the first result; a different answer under that key gives 409.
   2. The version and hash must match the offer the customer holds (otherwise 409).
   3. The offer must be open: answered, withdrawn, being revised or past valid until gives 409 with the status. Past valid until also records `expired` at once.
   4. **Decline:** recorded.
   5. **Accept:** the lines are re-priced as the workflow's system identity.
      - Identical amounts: recorded as `accepted`.
      - Any difference: recorded as `refused_changed`. Version N+1 is stored (`created_by` the system, so a manager may approve it) and the status becomes `revision_submitted`. The response is 409 with the differences.
      - Lines that can no longer be priced: `pricing_failed` with the issues; a sales operator revises (as in M4).
      - ERP down: 503, nothing recorded.
   6. Recording happens under a row lock on the quotation. It re-checks that the offer is still open, then writes the answer, the status, the revised version, a `resume_quotation` job and (for accept/decline) `notify_response`, all in one transaction.
3. **Workflow:**
   - Accepted, declined, withdrawn and expired answers end the run with that status.
   - `refused_changed` continues with `request_approval` for the revised version: approval, delivery, then the customer can answer the new version.
   - If the revision could not be priced, it continues with `await_revision`.
4. **Expiry job:** at valid until it records `expired` if the version is still unanswered, otherwise it does nothing. It retries if it runs early.
5. **Manager notice:** for accept/decline, every active linked channel of a current approver gets a short message (today, the simulated outbox file `notice-<id>.json`).
6. **Withdrawal:**
   - `POST /api/v1/tenants/{t}/quotations/{id}/withdrawal` with `{command_id, reason}` and CSRF.
   - Only the sales operator and approver roles may withdraw.
   - Only an issued, open quotation can be withdrawn; replays are safe.

## API
| Route | Who | Notes |
|---|---|---|
| `GET /api/v1/enquiries/{id}/quotation` | API key (`enquiries:view`) | Adds `status`, `valid_until`, `content_sha256` and `response_path` (M6) |
| `POST /api/v1/enquiries/{id}/quotation/response` | API key with `quotations:respond` (intake_service role) | `{action: accept\|decline, version, content_sha256, reason?}` plus `Idempotency-Key`. Returns 200 `{outcome, version, recorded_at, replayed}` |
| `GET /api/v1/tenants/{t}/quotations/{id}/customer` | `quotations:view` | Offered version, customer status, valid until, all answers, `can_withdraw` |
| `POST /api/v1/tenants/{t}/quotations/{id}/withdrawal` | sales operator or approver | `{command_id, reason}`; 409 if not issued or not open |

Errors from the answer route:
- **404:** nothing issued yet. Unknown IDs and other tenants' enquiries get the same answer.
- **409:**
  - not the offered version or hash;
  - `{status}` when the quotation is not open;
  - `{outcome: refused_changed, differences, revised_version}` when prices changed;
  - an idempotency key reused for a different answer.
- **503:** prices could not be checked; nothing was recorded.

## Tenant and auth boundary
- `quotation_responses` is under forced tenant RLS and append-only (grants plus the `reject_change()` trigger).
- At most one closing answer per quotation (partial unique index). Idempotency keys are unique per tenant.
- Staff cannot accept on the customer's behalf: only `intake_service` keys have `quotations:respond`.
- The API role may additionally update `quotations.pricing_issues`. The worker role may read and insert answers (for expiry).

## Configuration
`tenant_pricing_policies.quote_validity_days` (default 7, at least 1). There is no portal screen to change it yet; it is set in the database or the seed.

## Tests
- **Unit:**
  - `tests/unit/domain/test_acceptance.py`
  - `tests/unit/application/test_acceptance.py` (accept, decline, replay/conflict, wrong version, price change through re-approval and acceptance of v2, a rejected revision closes, unpriceable lines, ERP failure, expiry by job and before it, withdrawal roles, nothing issued, tenant and permission)
  - API tests in `tests/unit/api/`
  - the workflow waiting for the customer
- **PostgreSQL** (M6 section of `tests/integration/workflows/test_quotation.py`):
  - accept over HTTP ending the run;
  - a price change creating a version 2 approval task;
  - an accept/withdraw race recording exactly one answer;
  - expiry by the worker role;
  - append-only answers and tenant isolation.
- **Portal:** `customerService.test.ts` and `CustomerPanel.test.tsx`.

## Known limitations
- No ERP order, no stock reservation (a later milestone).
- No acceptance link or other customer channels yet; answers come only through the API.
- No portal screen for the validity setting.
- Approval runs that finished at "approved" before M6 (development data only) do not resume for answers. Their answers are still recorded, but a price-change revision there would not request approval automatically.
- Expiry depends on the worker running: an answer after valid until is refused and recorded as expired even if the job has not run yet.
