# Enquiry intake, job worker and attachment quarantine (M2)
**Status and milestone:** implemented in M2 and verified locally against PostgreSQL. Only the API channel exists; email and Telegram (M7) will produce the same envelope. AI extraction of the enquiry text is M3.

**Business purpose:** accept customer enquiries reliably. Nothing that was accepted may be lost, a retried request must not create a duplicate, slow work happens in the background, and unsafe files never reach later steps.

## Terms in simple language
- **Idempotency key**: an ID the caller chooses for one request. Sending it again means "the same request", so we return the first answer instead of doing the work twice.
- **Inbox event**: our durable record that a message arrived, stored in the standard **envelope** format whatever the channel.
- **Job**: a database row saying "do this work later". The **worker** is a separate process that does the jobs.
- **Lease**: temporary ownership of a job, for 60 seconds by default. If the worker crashes, the lease runs out and another worker takes the job.
- **Fencing token**: a fresh ID given with each lease. Only the current holder can finish the job.
- **Backoff with jitter**: wait longer after each failure (5 s, 10 s, 20 s and so on, up to 10 min), picked at random below that limit so retries don't all arrive at once.
- **Quarantine**: a holding area for uploaded files until they have been checked.
- **API key**: a password for a system, not a person. It belongs to one business and one role.

## Source files and entry points
- **API:** `POST /api/v1/enquiries` and `GET /api/v1/enquiries/{id}` in [api/enquiries.py](../../src/sales_platform/customer_channels/api/enquiries.py), wired in [api/app.py](../../src/sales_platform/api_app.py).
- **Worker:** `python -m sales_platform.worker` ([worker.py](../../src/sales_platform/worker.py)), which runs [JobRunner](../../src/sales_platform/platform/application/jobs.py) with [EnquiryPreparationHandler](../../src/sales_platform/enquiries/application/preparation.py).
- **Use cases:** [application/intake.py](../../src/sales_platform/enquiries/application/intake.py) and [application/api_keys.py](../../src/sales_platform/accounts/application/api_keys.py).
- **Rules:** [domain/intake.py](../../src/sales_platform/enquiries/domain/intake.py), [domain/attachments.py](../../src/sales_platform/enquiries/domain/attachments.py) and [domain/jobs.py](../../src/sales_platform/platform/domain/jobs.py).
- **Storage and database:** [persistence/intake.py](../../src/sales_platform/enquiries/persistence/intake.py), [persistence/jobs.py](../../src/sales_platform/platform/persistence/jobs.py), [persistence/api_keys.py](../../src/sales_platform/accounts/persistence/api_keys.py), [adapters/storage/local.py](../../src/sales_platform/platform/adapters/storage/local.py) and [migration 0003](../../migrations/versions/0001_baseline.sql).
- **Key management:** in the portal, **Settings -> API keys** (phase 2, [accounts and onboarding](accounts-and-onboarding.md)). A key sees only the enquiries and quotations it submitted itself (user answer P1-Q9); another key of the same business gets 404. A suspended business's keys get 403.

## Inputs and validation
- **Header `Authorization`:** `Bearer qk_<32 hex>_<secret>`. A missing, malformed, unknown, revoked or wrong key returns the same 401.
- **Header `Idempotency-Key`:** 8-200 printable characters, chosen by the caller and reused on retry.
- **Body (JSON):**
  - `text`: 1-20,000 characters.
  - `customer_reference`: optional, up to 200 characters.
  - `attachments`: up to 5 items of `{filename, content_type, content_base64}`. Each file is at most 5 MiB decoded and the total is at most 15 MiB.
  - Unknown fields, including `tenant_id`, are rejected.
- Requests larger than about 21 MB by `Content-Length` are refused with 413 before the body is read.

## Execution steps
1. Authenticate the key, which gives the tenant and role, then require `enquiries:submit`.
2. If an inbox event already exists for (tenant, channel, API key, idempotency key):
   - **Same payload fingerprint:** return the original receipt with 200 and `duplicate: true`.
   - **Different fingerprint:** return 409.
3. Write each attachment to `tenants/<tenant>/quarantine/<attachment id>`.
4. In **one transaction**, insert the inbox event (envelope plus fingerprint), the enquiry (`received`), the attachment rows (`quarantined`) and a `prepare_enquiry` job. If another request wins the race, delete our files and return its receipt. If anything fails, delete our files and return 503; the caller retries with the same key.
5. Return **202** after the commit, with `enquiry_id`, `correlation_id` and the `X-Correlation-ID` header.
6. The worker claims the job, inspects each quarantined file, moves clean files to `clean/` and marks rejected ones. The enquiry becomes `ready_for_extraction` if every file is clean (or there are none), otherwise `needs_review`.

## Outputs
**202 or 200:**
```json
{"enquiry_id": "...", "status": "received", "duplicate": false,
 "correlation_id": "...", "received_at": "2026-10-22T07:24:46Z"}
```
**GET:** returns the status, channel, customer reference and attachments (filename, type, size, status, rejection reason). The enquiry text and file bytes are not returned.

**Error codes:**

| Code | Meaning |
|---|---|
| 401 | Missing or invalid key |
| 403 | The key's role lacks the permission |
| 404 | Unknown enquiry, or one belonging to another business |
| 409 | Idempotency key reused with a different payload |
| 413 | Request too large |
| 422 | Invalid input |
| 503 | Nothing was stored; retry with the same key |

## Tenant and authorization boundary
- The tenant comes only from the API key.
- Every table uses forced RLS on `tenant_id`, and child rows use composite `(tenant_id, id)` foreign keys.
- The API role can read and insert intake rows for its context, insert jobs but not read them, and read only the API key it presents.
- The worker role can claim jobs for any tenant but must set the job's tenant to touch anything else.
- Storage keys include the tenant and are generated by the server; filenames are never used as paths.

## External effects and idempotency
- No external systems are called.
- Intake is idempotent per idempotency key.
- The preparation handler is idempotent: decided attachments are skipped, and a file already moved to `clean/` is recognised after a crash.
- Each job runs at least once and may run more often after a crash; its effects happen once.

## Failures and recovery
- **Database or storage failure during intake:** returns 503 and the files are removed, so the caller retries safely.
- **Worker crash:** the lease expires and another worker reclaims the job with attempt n+1. The crashed worker's late completion is rejected by fencing.
- **Handler error:** retried with backoff up to 5 attempts, then `dead` with the error type only (no customer data).
- **Missing data:** a missing enquiry or bad payload makes the job `dead` immediately.

## Configuration and secrets (names only)
- `SALES_DATABASE_URL` (API role).
- `SALES_WORKER_DATABASE_URL` (worker role).
- `SALES_STORAGE_DIR` (default `var/storage`).
- `POSTGRES_WORKER_PASSWORD` (Compose).
- API key tokens are shown once by the creation script; store them in a secret manager.

See [configuration](../development/configuration.md).

## Tests and example
See [testing](../development/testing.md). To try it locally, follow the [setup guide](../development/setup.md#enquiry-intake-m2).

## Known limitations
- Content inspection is not malware scanning; a scanner is a release gate.
- Only the API channel exists.
- There is no cleanup job for quarantine files orphaned by a process crash before commit.
- Dead jobs have no operator screen; inspect the `jobs` table.
- The body limit relies on `Content-Length`, so a proxy limit is needed in deployment.
- The worker polls every 2 seconds.

**Related ADRs:** [0002](../adr/0002-tenant-isolation.md), [0004](../adr/0004-durable-execution.md), [0011](../adr/0011-durable-intake-and-jobs.md).

## Follow-ups (phase 4)
A message with the same API key and `customer_reference` continues the customer's open enquiry under the agreed rules; the answer is `202` with the existing `enquiry_id` and `continued: true` (a replay returns the same). See [conversations](conversations.md).
