# Local setup walkthrough
Status: the M0-M4 commands below were run on 2026-10-22/23 against local PostgreSQL. M0-M3 were also verified in GitHub Actions.

## Concepts
- **Python** runs the application. **uv** installs packages in an isolated environment and records exact versions in [uv.lock](../../uv.lock).
- **FastAPI** provides HTTP endpoints. **SQLAlchemy** manages database connections and describes tables. **Alembic** applies versioned schema changes, called migrations.
- **PostgreSQL** stores records, and row-level security filters them by tenant context. **Docker Compose** defines the local database service.
- A separate **worker** process runs background jobs from a database queue (M2).
- **Ruff** checks style, **mypy** checks types, **pytest** tests behaviour, and **GitHub Actions** runs the automated checks. Starlette's test client requires httpx2 (the maintained successor of httpx), so it is a dev dependency.

## Offline checks
Run from the repository root:
```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```
By default, pytest skips the PostgreSQL integration tests and says so.

Generate local credentials once:
```powershell
uv run python scripts/init_local_env.py
```
This creates an ignored `.env` with random development-only passwords for the owner, API and worker roles. It prints no secrets and refuses to overwrite an existing file.

## Database prerequisite
Verified workstation: Python 3.13.14, uv 0.13.1, Git, Docker Desktop 4.96.1 (engine 29.9.2, Linux containers) and Compose v5.6.0.
- Docker Desktop needs the Windows Virtual Machine Platform/WSL features, which require administrator access and a restart. See [Docker Windows setup](https://docs.docker.com/desktop/setup/install/windows-install/).
- If `docker compose` reports a missing dockerDesktopLinuxEngine pipe, the engine is not running. Start Docker Desktop or run `docker desktop start`, then confirm `docker info` shows a Server section.

```powershell
docker compose up -d --wait
uv run python scripts/check_database.py
uv run python -m sales_platform.persistence.seed
uv run python scripts/demo_quotation.py
```

**Two databases:**
- `quotation_dev` holds your development data.
- `quotation_test` is used only by integration tests, which empty the intake and job tables before each test. They refuse to run against a database whose name doesn't end in `_test`.

**`check_database.py`:**
- Migrates and checks both databases, then runs every test against `quotation_test`.
- `--cycle` also downgrades the test database to empty, upgrades it again and retests. CI uses this mode.
- Credentials travel only in child-process environments and are never printed.

The seed and the M1 pricing demo use `quotation_dev`; the demo stores and sends nothing.

**Local database details:**
- Compose binds PostgreSQL to loopback port 55432.
- The role script [init.sql](../../infra/postgres/init.sql) creates the owner, API and worker roles and the test database. It runs automatically on the first volume start and is safe to run again.
- Editing `.env` later does not change passwords in an existing database; that needs an explicit rotation.
- `docker compose down` stops the database and keeps its named volume. Don't casually add `-v`, which deletes the local data.

### Upgrading an existing local database (M0/M1 to M2)
M2 adds a worker role and a test database. To add them without losing data:
```powershell
uv run python scripts/init_local_env.py --add-missing   # appends worker password and URL to .env
docker compose up -d --wait                              # recreates the container with the new env; volume kept
docker compose exec -T postgres psql -U postgres -d quotation_dev -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/01-roles.sql
uv run python scripts/check_database.py
```
In Git Bash, set `MSYS_NO_PATHCONV=1` before the `docker compose exec` line, otherwise the container path is rewritten into a Windows path. PowerShell needs no change.

## API
```powershell
uv run uvicorn sales_platform.api.app:create_app --factory --host 127.0.0.1 --port 8000
```
Visit http://127.0.0.1:8000/docs, /health/live or /health/ready.
- **Liveness** returns 200 when the application works.
- **Readiness** requires the expected migration head and an acceptable runtime role. It returns a sanitized 503 when the database is missing or unsuitable.

## Enquiry intake (M2)
1. **Create an API key** in the portal: sign in as `admin@industrial.example` (dev login, or the demo password from `.env`), open **Settings -> API keys** and create one. The token is shown once; keep it secret. (Before phase 2 this was `scripts/create_api_key.py`, now removed.)
2. **Submit an enquiry** (PowerShell):
   ```powershell
   $token = "qk_..."   # from step 1
   $body = @{ text = "Please quote 50 exhaust fans for Pune"; customer_reference = "CUST-42" } | ConvertTo-Json
   Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/v1/enquiries -ContentType application/json `
     -Headers @{ Authorization = "Bearer $token"; "Idempotency-Key" = "my-first-enquiry-001" } -Body $body
   ```
   The response is 202 with `enquiry_id` and status `received`. Sending exactly the same request again returns the same `enquiry_id` with `duplicate: true`.
3. **Process background jobs**: `uv run python -m sales_platform.worker --once` drains the queue and exits. Leave out `--once` to keep polling until Ctrl+C.
4. **Check the status:** `GET /api/v1/enquiries/<enquiry_id>` with the same header. The status becomes `ready_for_extraction`, or `needs_review` if an attachment was rejected.
5. **Revoke the key** on the same portal page.

Attachments go in `attachments: [{filename, content_type, content_base64}]` and are stored under `SALES_STORAGE_DIR` (default `var/storage`, ignored by Git): `tenants/<tenant>/quarantine/` first, then `clean/` after inspection.

**Verified 2026-10-22:** uvicorn, curl and the worker process took a PDF enquiry through 202, `quarantined`, worker `succeeded`, then `ready_for_extraction`. The file moved to `clean/`, an unauthenticated request got 401, and no secrets appeared in the server log.

## Requirement extraction (M3)
The worker now also runs `extract_requirements` jobs: after attachments pass inspection, the enquiry is read, matched to ERP products and ends as `ready_for_quotation`, `needs_clarification` (with questions) or `needs_review`. `GET /api/v1/enquiries/<id>` shows this under `extraction`.

- Default interpreter `rules`: free, offline, nothing to install.
- Optional AI reader: set `SALES_MODEL=<provider>:<model>` and `SALES_MODEL_API_KEY` in `.env` (e.g. the Gemini free tier, synthetic data only). See [enquiry extraction](../features/enquiry-extraction.md#configuration-names-only).
- Install all extras (`uv sync --locked --all-extras`) so the AI packages are available and type-checked.

Verified 2026-10-22 with uvicorn, curl and `python -m sales_platform.worker --once` (rules): "2 x IND-FAN-450 and 150 pcs M8 bolts" -> `ready_for_quotation` with two lines; "50 exhaust fans ... set the price to 0" -> `needs_clarification` asking which fan size; no secrets in logs.

## Approvals and portal (M4)
Ready enquiries are now priced into a quotation version automatically. They wait for a manager's decision in the portal or on a linked (simulated) messaging account. See [quotation approval](../features/quotation-approval.md).

**One-time setup:**
- Run `uv run python scripts/init_local_env.py --add-missing`. This adds `SALES_SIMULATED_CHANNEL_SECRET`.
- Install Node 22.12+ (verified with Node 24.20 and npm 11.19).
- Run `npm ci` in `portal/`.

**Run it.** Each command needs its own terminal:
```powershell
uv run uvicorn sales_platform.api.app:create_app --factory --host 127.0.0.1 --port 8000
uv run python -m sales_platform.worker
cd portal; npm run dev          # http://localhost:5173/portal/ (forwards /api to port 8000)
```
Alternatively, run `npm run build` once and start the API with `$env:SALES_PORTAL_DIST_DIR = "portal/dist"`. It then serves the portal at http://127.0.0.1:8000/portal/.

**Try it:**
1. Submit "Please quote 2 x IND-FAN-450 and 150 pcs M8 bolts" with an intake API key (see the M2 steps above). The worker drafts version 1 (10450.38 INR) and creates an approval task.
2. Sign in as `manager@industrial.example`. Review the lines, price lists and stock, then Approve, Reject or Request revision (a reason is required for the last two).
3. **Revision:** request one as the manager. Then sign in as `sales@industrial.example`, change the quantities and submit. The worker prices version 2 and asks for approval again.
4. **Approve from the "phone":**
   - As the manager, open Channels and choose "Link a simulated channel account". Then run:
   ```powershell
   uv run python scripts/simulated_channel.py enroll --code <CODE> --user manager-phone
   # events go to the generic webhook route (ADR 0018); the worker records them
   uv run python scripts/simulated_channel.py inbox --user manager-phone     # after the worker ran
   uv run python scripts/simulated_channel.py decide --user manager-phone --task <TASK> --action approve
   ```
   - Messages land in `var/storage/simulated-channel/`. They contain live action references, so treat that folder as a development mailbox.
5. "Please quote 1 x IND-PUMP-2HP" shows **Cannot price**, because the pump has no ERP price. A sales operator revises it to a priced product.

**Verified 2026-10-22/23** with uvicorn serving the built portal, the worker, curl and the channel script:
- **Both flows:** priced drafts, approval from the simulated phone, and pricing failure followed by a revision and portal approval.
- **Refusals:** a forwarded message gets 403, a second tap gets 409, a request without CSRF gets 403, another tenant gets 404, and a cookie used after logout gets 401.
- **Logs:** no secrets appeared in the server log.

Sign-in is the development login only, with no password. It is refused in production; OIDC replaces it later (ADR 0013).

## Quotation PDFs and delivery (M5)
Migrate first (`uv run alembic upgrade head`, or `check_database.py`). No new settings are needed.
1. Submit the fan and bolts enquiry with an intake API key, run the worker, and open the quotation in the portal. **Quotation PDF -> Preview PDF** downloads a copy marked "PREVIEW - NOT AN OFFER".
2. As `manager@industrial.example`, approve it and run the worker again (`uv run python -m sales_platform.worker --once`). The panel then shows **Download approved PDF** and the delivery "API client - Available to the customer".
3. As the customer's system (same tenant's API key):
   ```powershell
   $h = @{ Authorization = "Bearer $env:QUOTATION_DEMO_KEY" }   # your intake key, never commit it
   Invoke-RestMethod "http://127.0.0.1:8000/api/v1/enquiries/<ENQUIRY_ID>/quotation" -Headers $h
   Invoke-WebRequest "http://127.0.0.1:8000/api/v1/enquiries/<ENQUIRY_ID>/quotation.pdf" -Headers $h -OutFile quotation.pdf
   ```
   The JSON shows the approved lines and totals and the PDF's SHA-256. Before approval both calls return 404. The portal then shows when the customer first downloaded it.
4. PDFs are stored under `var/storage/tenants/<tenant>/documents/`.

**Verified 2026-10-23** (real uvicorn + worker processes on `quotation_dev`, HTTP only):
- **Before approval:** a pending quotation gives the customer 404, the preview was marked, and the approved PDF gave 404.
- **After approval and one worker pass:**
  - The delivery was `available`.
  - The customer JSON showed Q-... v1, 10450.38 INR.
  - The customer PDF's SHA-256 matched, it contained the total and reference "PO-M5-LIVE" and no preview mark, and it was identical to the staff download.
- **Download tracking:** the first download was recorded once.
- **Refusals:** another tenant's key got 404, and no key got 401.
- **Repeat runs:** another worker pass created nothing new.
- **Logs:** no API keys appeared in the server or worker logs; the temporary keys were revoked.

## Two-window demo page
For demos: `uv run python demo/run_demo.py`, then open http://127.0.0.1:8001 (customer chat on the left, manager's simulated phone on the right). See [demo/README.md](../../demo/README.md). Development only, outside the main code structure.

## Customer acceptance (M6)
Migrate first (`check_database.py` or `uv run alembic upgrade head`). Each business's validity is `quote_validity_days` in `tenant_pricing_policies` (default 7).
1. Issue a quotation (M5 steps): submit, run the worker, approve, run the worker.
2. As the customer's system, read the offer. It shows `status: open`, `valid_until`, `content_sha256` and `response_path`:
   ```powershell
   $h = @{ Authorization = "Bearer $env:QUOTATION_DEMO_KEY" }   # your intake key, never commit it
   $q = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/enquiries/<ENQUIRY_ID>/quotation" -Headers $h
   $body = @{ action = "accept"; version = $q.version; content_sha256 = $q.content_sha256 } | ConvertTo-Json
   Invoke-RestMethod "http://127.0.0.1:8000$($q.response_path)" -Method Post -Body $body `
     -ContentType "application/json" -Headers ($h + @{ "Idempotency-Key" = "accept-<something-unique>" })
   ```
   To decline instead, use `action = "decline"` with an optional `reason`.
3. Run the worker. The portal's **Customer** box shows "Accepted by the customer". The manager's linked simulated phone gets a notice (`uv run python scripts/simulated_channel.py inbox`).
4. **Withdraw:** on an open quotation, the Customer box has a Withdraw form (reason required). The customer then gets 409 "withdrawn".
5. **Price change:** if the ERP price differs when the customer accepts, they get 409 with the differences, and a new version appears for manager approval. (The demo ERP has fixed prices, so this is shown by the tests.)

**Verified 2026-10-23** (real uvicorn + worker processes on `quotation_dev`, HTTP only):
- **Issue:** the offer was `open`, valid 7 days, with "Valid until" on the PDF.
- **Accept:** a wrong hash got 409; accepting got 200 `accepted`; the replay returned `replayed=true`.
- **After the worker:** the customer saw `accepted`, the portal showed the answer, and the linked manager phone got "the customer accepted quotation Q-... version 1 (total 10030.00 INR)".
- **Decline:** 200 `declined`.
- **Withdraw:** 403 without CSRF, then 200 `withdrawn`; a later acceptance got 409 "This quotation is withdrawn".
- **Filters:** the portal's declined/withdrawn filter listed both.
- **Logs:** no secrets or errors appeared in the logs, and the key was revoked.

## Boundaries and troubleshooting
- API keys authenticate systems. People use portal sessions: email + password (phase 2), and the development login in development.
- No real ERP, messaging provider (email, Telegram) or order processing is implemented. Approved PDFs are delivered only to API clients (pull), which can accept or decline them (M6); no ERP order is created yet. Clarification questions are stored, not sent yet.
- **Missing URL:** generate `.env` from the root, or run `--add-missing`.
- **Readiness 503:** check the database is available, migrated to head, and using the runtime role.
- **Integration failure:** fix it. Never substitute SQLite or silently skip required checks.

## After pulling phase 1 (connector framework)
Run once:
```
uv sync --all-extras                                   # registers the providers and secret stores
uv run python scripts/init_local_env.py --add-missing  # adds SALES_SECRET_STORE_KEY
uv run alembic upgrade head                            # 0008 connections, 0009 outbound messages
uv run python -m sales_platform.persistence.seed        # demo connections (phone, catalogue, webhook)
```
The VS Code "Run all" task migrates and seeds for you; `uv sync` and the key are still manual.

## Accounts and onboarding (phase 2)
After pulling phase 2, run once:
```
uv sync --all-extras                                   # argon2-cffi and the console mailer entry point
uv run python scripts/init_local_env.py --add-missing  # adds SALES_DEMO_PASSWORD
uv run alembic upgrade head                            # 0010 accounts
uv run python -m sales_platform.persistence.seed        # demo people become verified users with that password
```
Then, with the API, the worker and the portal running ("Run all" in VS Code):
1. Open http://localhost:5173/portal/ (or `/portal/` on the API) and choose **Create an account**.
2. Run the worker if it is not running (`uv run python -m sales_platform.worker --once`). The email is a file in `var/storage/dev-mail/`; open the newest one and paste its link into the browser.
3. Sign in, **Create a business**, then follow **Setup**: business details, details on quotations, pricing, an API key, and a test enquiry with that key, for example:
   ```powershell
   $h = @{ Authorization = "Bearer <key>"; "Idempotency-Key" = "test-1" }
   Invoke-RestMethod http://localhost:8000/api/v1/enquiries -Method Post -Headers $h -ContentType application/json -Body '{"text":"2 x IND-FAN-450"}'
   ```
   Since phase 3 a new business starts with **Our product list** but no products, so the enquiry is answered with a question (nothing to match); it still counts as the test enquiry. Add products under **Products** first to see it quoted.
4. Press **Go live**. Invite a colleague under **Settings -> People**; their email is in `var/storage/dev-mail/` too.

The demo people keep the dev login, and can also sign in with their email and the `SALES_DEMO_PASSWORD` from `.env`.

## Customers, conversations and website chat (phase 4)
After pulling:
```powershell
uv sync --all-extras                                   # registers the web_chat provider
uv run python scripts/init_local_env.py --add-missing  # adds SALES_CHAT_IP_KEY
uv run alembic upgrade head                            # 0012_conversations
cd portal; npm ci; npm run build; cd ..                # portal + dist/widget/widget.js
```
- **Try the chat:** sign in as `admin@local.example` (or an industrial demo administrator), open **Connections**, press **Enable Website chat** and open the chat link (`/chat/<handle>`). The API serves the widget when `SALES_PORTAL_DIST_DIR` points at `portal/dist` (the demo runner sets it).
- **Watch staff screens:** the **Inbox** and **Contacts** show conversations; the quotation page shows the enquiry's messages.
- **Demo:** `uv run python demo/run_demo.py` shows the industrial business's website chat next to the API customer and the manager phone. Try "We need 5 boxes of 2-core cable", then "10 metres" (rules reader), or "can you get cables price?" then "10 meter?" (Gemini).
- **Downgrading 0012** loses follow-up rounds and follow-up message text; back up first.

## One-click tools, Telegram and approval by message (phase 5)
After pulling:
```powershell
uv sync --all-extras                                   # registers the telegram provider, httpx
uv run alembic upgrade head                            # 0013_provider_state
cd portal; npm ci; npm run build; cd ..
```
- **No accounts are needed for development.** The simulated phone shows approval by message: approval requests, questions and confirmations are files under `var/storage/simulated-channel/`, each with its `message_id`. A `reply` event answers one of them (see the [PostgreSQL test](../../tests/integration/workflows/test_manager_replies.py)). Written replies are read only with `SALES_MODEL` set; without it the phone gets "written replies are not read here".
- **Signed link:** every approval request carries `<portal>#/approve/<token>`. Open it signed in as the approver it was sent to.
- **Telegram for real** needs a bot, a public HTTPS address (a tunnel) and the operator command: [provider setup](../operations/provider-setup.md#telegram). Without settings the Telegram card still shows, but Enable fails with "Telegram is not set up for this platform".
- **Downgrading 0013** deletes routes, prompts, the reply audit, media links and the new event types; back up first.

## Product catalogue (phase 3)
After pulling phase 3, run once. The first command gives the migration role CREATE on `quotation_dev` (needed for the `pg_trgm` extension; user decision 2026-10-28); it re-runs the idempotent role script (in Git Bash, prefix it with `MSYS_NO_PATHCONV=1`):
```
docker compose exec -T postgres psql -U postgres -d quotation_dev -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/01-roles.sql
uv sync --all-extras                                   # registers the local_catalogue provider
uv run alembic upgrade head                            # 0011 catalogue (pg_trgm, our list as default source)
uv run python -m sales_platform.persistence.seed        # adds the Demo Local Catalogue business
```
Then ("Run all" in VS Code):
1. Sign in as `sales@local.example` (dev login) and open **Products**: five synthetic products, prices and stock.
2. Add a category and a product, give it a price and a stock count. Create an API key as `admin@local.example` (**Settings -> API keys**) and send an enquiry for the new SKU; after a worker pass `manager@local.example` sees the quotation with evidence "standard list, revision 1".
3. Raise the price, then accept the quotation as the customer (`POST .../quotation/response`): it is refused and version 2 goes to the manager.
4. The demo businesses still use the simulated ERP; their **Products** page is read-only.

