# 0011: Durable intake, PostgreSQL job queue, attachment quarantine and API keys
Date: 2026-10-22
Status: implemented in M2; verified locally and in CI ([CI run 39741408875](https://github.com/nikunjramani/ai-sales-platform/actions/runs/39741408875))
Extended by [0019](0019-accounts-and-onboarding.md) (phase 2): keys are created and revoked in the portal (the script is removed), and a key reads only the enquiries it submitted.
Extended by [0018](0018-connector-framework.md) (phase 1): each API key is an `api` connection; the envelope (schema 2) carries `connection_ref` and `reply_to`; inbox events are deduplicated per connection, provider event ID and event type.

## Context
M2 must accept enquiries only after durable receipt, recognise replays, survive worker crashes and isolate unsafe attachments. Real identity (OIDC) arrives in M4, but the intake API needs authentication now.

## Decision
- **Authentication:** per-tenant API keys (service accounts).
  - Token: `qk_<key id>_<secret>`. Only the SHA-256 hash of the 256-bit secret is stored, and comparison runs in constant time.
  - The tenant and role come from the key, never from the request body.
  - A new `intake_service` role grants only `enquiries:submit` and `enquiries:view`.
  - Keys are created and revoked with owner credentials (`scripts/create_api_key.py`). The runtime role can only read the key whose ID it presents, through the RLS policy `api_key_lookup`.
- **Receipt:** one transaction stores the inbox event (canonical envelope), the enquiry, its attachment rows and a `prepare_enquiry` job. The API answers 202 only after that commit.
  - The idempotency key is the caller's `Idempotency-Key`. It is unique per tenant, channel and connection (the API key).
  - A replay with the same payload returns the original result (200). The same key with a different payload returns 409.
  - Races are settled by `INSERT ... ON CONFLICT DO NOTHING` on that unique key.
- **Attachments:**
  - Bytes are written to a tenant-scoped quarantine area under server-generated keys before the transaction. If the transaction fails or loses the race, they are deleted.
  - The worker inspects each file: allowed type, magic bytes matching the declared type, size, and no encrypted or active-content PDFs. Clean files move to the clean area; rejected files stay quarantined and the enquiry goes to `needs_review`.
  - Local disk storage is used in development, behind the `ObjectStorage` port.
- **Jobs:** a PostgreSQL table is the queue.
  - Workers claim jobs with `FOR UPDATE SKIP LOCKED` and lease them, so a crashed worker's job is reclaimed after the lease expires.
  - Every claim gets a fresh lease token. Completion is fenced by that token, so a late worker cannot overwrite a newer attempt.
  - Failures retry with capped exponential backoff and full jitter. Permanent errors and exhausted attempts become `dead`.
  - Handlers must be idempotent.
- **Roles:** a separate `quotation_worker` database role. It sees the `jobs` table across tenants (policy `worker_queue`); every other table still requires the job's tenant context. The API role can insert jobs but not read or change them.
- **Testing:** integration tests run only against a database whose name ends in `_test` (`quotation_test`), because they truncate intake tables.

## Alternatives
- **Authentication:** an unauthenticated development API, waiting for OIDC, or static shared secrets in environment variables.
- **Queue:** a separate broker such as Redis, RabbitMQ or Celery. Rejected for now: it adds infrastructure and loses the shared transaction with business data.
- **Worker database role:** letting the API role run jobs, or using a BYPASSRLS worker.
- **Attachments:** storing bytes in PostgreSQL, or trusting declared content types.

## Consequences
- Inbox, enquiry and job are atomic, and attachment files are cleaned up on failure. An orphaned quarantine file is possible only if the process dies between writing the file and the transaction; a cleanup job is a follow-up.
- Polling a PostgreSQL queue suits pilot volumes. Revisit with measurements in M8.
- Content inspection is not malware scanning. An antivirus or sandbox scanner is a release gate before real customer files are accepted.
- Request size is enforced from `Content-Length`; deployment must also cap body size at the proxy.
- An existing local volume needs the idempotent role script re-run (see setup).

## Verification
Unit, API and PostgreSQL integration tests are listed in [testing](../development/testing.md), with results in the [handoff](../handoff.md). A manual end-to-end run with uvicorn, curl and the worker process is recorded in the handoff.
