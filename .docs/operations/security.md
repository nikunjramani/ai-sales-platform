# Security and privacy plan
- People sign in with email and password (D1, ADR 0019); tenant-scoped roles: administrator, sales operator, approver and viewer; service accounts have narrower scopes.
- Authenticate callbacks with provider-supported verification, replay protection/deduplication and rate limits.
- Store credential references in business tables; keep encrypted secrets in managed secret storage or protected local environment configuration for development.
- Never send credentials to models. Minimize customer data in prompts; redact sensitive logs/traces.
- Treat PDFs and messages as untrusted. Bound file size/pages/time, validate content types, scan/quarantine before parsing, and isolate risky document processing.
- Do not fetch arbitrary user URLs: constrain attachment retrieval to validated provider/object paths to mitigate SSRF.
- Protect portal sessions with secure cookies, CSRF defenses and output escaping.
- Private documents require authorized short-lived downloads. PDF rendering must not execute untrusted HTML/scripts or fetch unrestricted URLs.
- Audit approvals, external writes, administrative changes and support access. Make audit mutation restricted and observable.
- Bound model calls, token budgets, tool access and retries per tenant. Prompt text never grants permissions.
- Define retention, export and deletion policies before real data; include checkpoints, attachments, tracing and backups.
- Keep development, staging and production credentials/data separate.
- Test tenant isolation in the real PostgreSQL runtime role, not only as database owner.
- Document threat model and unresolved findings before pilot; do not claim regulatory certification.
- Bind manager messaging identities to internal memberships with proof of control. Recheck role/revocation at decision time and restrict allowed conversation contexts.
- Provider webhook verification is necessary but not sufficient: validate the acting manager, task/version, recipient binding and expiry. Forwarded actions and uncorrelated text must not approve anything.
- Mobile channel review may expose commercial documents to provider storage; configure permitted channels and disclosure per tenant. Never include reusable action secrets in model input or logs.

## Implemented controls (phase 2, accounts and onboarding, ADR 0019)
- **Passwords:** Argon2id (64 MiB, 3 passes, 4 lanes), at most 4 hashes at once, re-hashed on login when parameters change; 12-128 characters; common passwords and the email name refused. Never logged, never in job payloads, never returned.
- **No account lockout (user answer Q3);** rate limits per IP, email, person and business, counted in PostgreSQL with hashed keys, answer 429 with `Retry-After`.
- **No account enumeration:** one answer for sign-up, login, forgot password and links; unknown addresses still cost one hash.
- **Email links:** 256-bit tokens created by the worker at send time; only SHA-256 stored; single use by compare-and-set; short lifetimes; in the URL fragment and request bodies only; removed from the address bar after use; `no-referrer`.
- **Sessions:** a new session at every login; all sessions revoked after a reset, the others after a change; removing a member takes effect on their next request.
- **CSRF without a session:** sign-up, login, reset and invitation preview accept `application/json` only (a cross-site form cannot send it without a CORS preflight).
- **Least privilege kept (ADR 0002):** the API still cannot insert tenants or memberships. Four owner-owned `SECURITY DEFINER` functions with a fixed `search_path`, EXECUTE for the API role only, trust only `app.subject` and check it against stored rows; the last administrator survives concurrent removals. The worker cannot read people or credentials.
- **Business status:** a trigger allows the API only setting_up -> active; suspension needs owner credentials.
- **Audit:** `admin_events` is append-only (also for the owner) and never holds secrets.
- **Uploads:** logos are checked by their first bytes, size and pixel count before decoding, and re-encoded from pixels only.
- **Email content:** fixed templates; values in subjects lose line breaks (no header injection).
- **API keys:** created in the portal, shown once with `no-store`, audited; each key reads only its own enquiries.
- **Release gate:** production refuses the console mailer, so phase 2 cannot be deployed before a real sender exists (phase 5).

## Implemented controls (orders)
- **Orders:**
  - one per quotation (database);
  - lines and events are immutable for every role (trigger), and order rows change only in progress columns.
- **Customers:** "track my order" answers only the asking contact's orders; the API view never shows staff details.

## Implemented controls (phase 5, one-click tools and approval by message)
- **Platform webhooks:**
  - the company secret is checked (constant time) before anything is read;
  - the business comes only from exact routing keys, unique across businesses (a key of another business rolls a setup step back);
  - genuine requests are answered 200, so tools do not retry them; only bad secrets get 401.
- **Setup:**
  - one-time link codes are stored only as SHA-256 route keys (10 minutes);
  - provider secrets go to the secret store, and connections keep references;
  - company settings live in the platform namespace, are set by an operator command and are never shown in the portal.
- **Approval by message (D10):**
  - only the linked account, only a reply to one request;
  - deterministic guards (conditional, negated, questioning or other-quotation replies never approve);
  - the AI sees only the reply, number and version, inside a data tag;
  - every AI-read decision needs the manager's exact YES;
  - Yes/No and approval buttons carry single-use tokens created at send time;
  - the signed link needs sign-in as the intended manager, and a POST with CSRF decides;
  - every written reply is audited (append-only).
- **Customer messages from tools:**
  - website chat limits per sender;
  - one daily reading cap per business;
  - files type- and size-checked into quarantine;
  - the business's own messages are never read as the customer's.
- **Public PDF links:** random token (hash stored), 15 minutes, one document, content hash re-checked, `no-store`.
- **HTTP clients** only inside provider packages (architecture test). **Not live-verified** (no accounts in phase 5).

## Implemented controls (phase 1, connector framework)
- **Provider secrets** live in a secret store (D8), never in the database. `connections.secret_refs` holds references only. The development `local` store encrypts each secret with AES-256-GCM, bound to its tenant and reference, so a copied or renamed file does not decrypt. Production uses `none` until hosting is chosen (D24); `local` is refused there.
- **Generic webhooks** (`/api/v1/hooks/{connection}/{token}`):
  - Only the token's SHA-256 is stored. It is compared in constant time, with a dummy hash when the connection does not exist.
  - Unknown connection, wrong token, inactive connection or missing provider all answer the same 404.
  - Size limits apply before reading further.
  - The provider verifies the signature on the raw bytes **before** parsing.
  - The tenant always comes from the connection; provider bodies cannot name one (the simulated body forbids extra fields).
  - Events are deduplicated per connection, provider event ID and type.
- **Development-only providers** (`simulated`) are skipped by the registry in production.
- **Disabled connections** get no approval requests, and their webhooks answer 404. API keys disable their `api` connection when revoked.
- **Worker grants (0008):** the worker may record approval decisions, because manager actions arriving on webhooks are routed in the `process_inbound` job. The grants are column-level, and the checks in `ApprovalService` are unchanged.
- **Known weaknesses, accepted:**
  - P1-Q9: any API key of a business can read every API enquiry and quotation of that business. This is unchanged; the user decides it in phase 2 with API key management.

## Implemented controls (M6)
- **Answers bind to exact content:** the customer names the version and SHA-256 it answers; anything else gets 409. Only `intake_service` API keys of the same business can answer (`quotations:respond`). Staff cannot accept for the customer.
- **No acceptance at changed prices:** acceptance re-prices from the ERP. Any difference refuses it, and the revised version needs a fresh manager approval. ERP failures record nothing.
- **Exactly one closing answer:**
  - A row lock on the quotation, plus a partial unique index, means a racing accept, withdrawal or expiry produces one outcome. This is tested on PostgreSQL.
  - Idempotency keys are unique per tenant: replays return the first result, and a different answer under a used key is refused.
  - Answers are append-only (grants plus trigger).
- **Withdrawal:** a sales operator or approver only, with CSRF and a reason.

## Implemented controls (M5)
- **Only approved content reaches customers:**
  - Delivery is queued only for the approved current version.
  - Rendering re-checks that an approve decision exists for exactly that version and content hash.
  - Customer API routes show only delivered quotations, and other tenants or unknown IDs get the same 404.
- **Document integrity:** each PDF's SHA-256 is recorded when it is rendered and re-checked on every read. A missing or changed file is never served or sent; the delivery fails visibly and downloads get 500. Document rows are append-only, and the storage key changes with the content, so nothing is overwritten.
- **Safe rendering:**
  - ReportLab builds the PDF from data, with no HTML, scripts or URL fetching.
  - Customer text is escaped before ReportLab's markup parser, so it is printed, never interpreted.
  - Unsupported characters are refused, not replaced.
  - Internal evidence (price lists, stock checks) stays out of customer PDFs.
- **Downloads:** always `attachment` with `Cache-Control: no-store` and `nosniff`. Staff need a session with `quotations:view`; customers need an API key with `enquiries:view` for that tenant.
- **No duplicate sends:** a compare-and-set lets only one worker hand a document over. An uncertain outcome is recorded as `unknown` and never retried automatically.
- **Least privilege:** the worker may update only delivery progress columns, and the API may update only `first_downloaded_at`.

## Implemented controls (M4)
- **Portal sessions:**
  - 256-bit random tokens in `HttpOnly; SameSite=Strict; Path=/api` cookies (Secure in production);
  - only SHA-256 hashes stored, an 8-hour lifetime and server-side logout;
  - state changes need an `X-CSRF-Token` derived from the session;
  - the React app never stores tokens.
- **Dev login:** it asks for no password, so configuration refuses it in production, and it only admits stored members. Since phase 2 people sign in with email and password (ADR 0019).
- **Access checks:**
  - Every portal request re-resolves membership for the tenant in the URL; non-members get 404.
  - Every decision re-checks the approve permission, so people who were removed or demoted cannot act on old pages or messages.
  - Every decision also re-checks the content hash and current version (no stale approvals) and forbids approving a version you created.
- **One decision per task**, taken under a row lock:
  - Replays return the original decision; conflicting actions get 409.
  - Decisions and versions are append-only, enforced by grants and triggers.
- **Channel events:**
  - Verified by provider signature (HMAC-SHA256, constant-time) before parsing.
  - Action references are random, stored hashed, single use and expire after 24 h. They only work for the bound account and connection, so forwarded messages fail.
  - Binding an account requires a 10-minute one-time code sent from it; bindings are revocable.
  - All rejections share one generic message.
- **Narrow RLS additions:**
  - A person may list only their own memberships and tenant names (`app.subject`).
  - Sessions are visible only by token hash.
  - The worker gained read access to memberships, tenants and pricing policies, still tenant-scoped.
- **Simulated outbox:** it holds live references in plain files under the git-ignored storage folder, so it is for development only.

## Implemented controls (M3)
- Model output is untrusted: strict JSON schemas with no extra fields, verbatim quote checks against the customer's text, product choices limited to ERP candidates, template (not model-written) customer questions. Prices, stock and permissions never come from a model.
- Customer content is wrapped in data tags; attempts to close the tag are neutralised. Invalid answers go to human review.
- LangGraph checkpoints have no tenant column: only the worker role can access them, always through a tenant-scoped workflow_runs row; API and public access revoked. Document text is not stored in checkpoints.
- Default interpreter is offline (no AI). Any hosted model (e.g. the Gemini free tier, whose prompts may be used to improve Google's products) is for synthetic data only until its data handling is reviewed for real customer data.

## Implemented controls (M2)
- API keys: 256-bit secrets, only SHA-256 hashes stored, constant-time comparison, one generic 401 for every failure, per-tenant scope and role, revocation. Tokens are printed once and never logged.
- Tenant identity for intake comes only from the key; request bodies with unknown fields (including `tenant_id`) are rejected.
- Uploads are size-limited, stored under server-generated tenant keys (never filenames), quarantined until inspected, and rejected on type mismatch, encrypted PDFs or active PDF content. This is not malware scanning: a scanner is a release gate before real files.
- Job payloads hold references only; job errors record exception types only, never customer text.
- Separate database roles: the API cannot read the job queue; the worker sees only the queue across tenants and needs tenant context for everything else.
- Integration tests refuse to run unless pointed at a `*_test` database.
