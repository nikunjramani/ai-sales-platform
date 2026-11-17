# 0022: One-click providers, platform webhooks and approval by message
**Status:** accepted (phase 5, user decisions 2026-10-29); implemented on `feat/phase-5-providers`. **Not live-verified:** there were no company accounts in this phase (user answer), so every provider path is checked against fixtures written from the providers' documentation and against fakes. Extends ADR 0007 (channel-independent approval), ADR 0013 (one approval core), ADR 0017 (the generic model loader) and ADR 0018 (connector framework). Supersedes ADR 0015's "email approval deferred" (D10 covers it; an email package plugs into the same pieces).

## Context
Phase 5 was planned as six provider packages (SendGrid email, Twilio WhatsApp and SMS, Telegram, Instagram, Messenger). The user narrowed it on 2026-10-29 to the **generic framework plus Telegram**: "for now don't implement SendGrid related code; make the code so that we can easily integrate any third-party tool". Decisions in phase 5 plan, user answers (git history) and [07 decisions](../plan/platform/07-decisions-needed.md) (D5, D9, D10).

## Decision
1. **Provider contract** (`ports/providers.py`):
   - One-click lifecycle: `provision`, `resume`, `deprovision`.
   - Platform webhooks: `verify_platform_inbound`, `routing_keys`, `unrouted`, plus GET `handshake`.
   - `fetch_file` for customer files, `enrollment_hint`, `provider_page`, `platform_setup`, a `cost` note and an optional `platform_config_model`.
   - `ProviderDefaults` gives every optional operation an explicit `OperationUnsupported`, so a package writes only what its tool can do.
   - Capabilities `self_service`, `platform_webhooks`, `attachments_by_url`.
   - `RenderedMessage` carries `purpose`, `link` (the signed approval link) and `attachment_url`; `SendReceipt` may name `routing_keys`.
2. **One-click Enable** (`application/lifecycle.py`):
   - Enable creates the connection (`setting_up`) and calls `provision`. The `ProvisionResult` (ready, needs_action with one `PendingAction`, working with a poll, failed) is applied in one transaction: status, public config, secrets (stored in the SecretStore, references on the connection), routes and the pending step.
   - One-time values (a link code) are returned once and never stored.
   - Pending steps have four kinds: `open_url`, `provider_page`, `show_steps`, `fill_form`. Steps that return data go to `POST .../actions/{id}`; setup events from the tool are `ConnectionUpdate` events handled by the worker; `poll_connection` asks again.
   - A tool with costs needs the administrator's "may incur costs" tick (P5-11).
3. **Platform webhooks** (`POST|GET /api/v1/hooks/p/{provider_key}`):
   - The provider checks the request with company-level secrets, then names the routing keys present in it.
   - `connection_routes` (globally unique key, may expire) resolves each key to one connection. The lookup policy reveals only the exact key presented.
   - A key taken by another business rolls the whole step back (`RouteTaken`).
   - Genuine requests are always answered 200, even when nothing claims them (the provider may answer the sender generically) or when they cannot be read. Only a bad signature is 401.
   - Connections still being set up accept only setup events.
4. **Customer messages from webhooks** take the website chat road (`application/customer_inbox.py`):
   - They go through `IntakeStore.record` with the follow-up router.
   - The website chat limits apply: 10/min and 100/day per sender. Over a limit the message is stored but held for review, with no AI reading.
   - One daily reading cap of 200 per business is shared by all push customer tools.
   - Files: PDF, text, PNG and JPEG up to 5 MB, downloaded into quarantine; other kinds are skipped and noted.
   - STOP and START become consent events.
   - What the business typed on its own account is a staff row of the conversation; the reader sees it as our side's context, never as the customer's words.
5. **Approval by message (D10)** (`application/manager_replies.py`, `domain/manager_replies.py`). All three ways in reach the same `ChannelActionService` and `ApprovalService` checks:
   - **Buttons.** Approve decides. Reject/Change ask "reply with the reason" (a `reason` prompt), and the reply completes it with the button's action (no AI).
   - **Written replies.** A reply must come from the linked account and reply to one of our messages about that request (the provider message ID → `outbound_messages.action_ref_id`). Without a model, replies are not read (P5-5). With one, `classify_manager_reply` runs the reader on the reply text, quotation number and version only (P5-6). Deterministic guards follow: conditional, mixed, negated or questioning words never approve; another quotation number makes it unclear; a reason must be copied from the reply. Then the manager is asked "You want to … Q-… version N? YES / NO" (a `confirm` prompt, P5-13). Only an exact YES (reply or button) records it, NO cancels, other text is read as a new reply. If the reader fails on the last attempt, the manager is asked; nothing is decided on a failure.
   - **Signed link.** The review's own single-use token opens `#/approve/{token}` in the portal, which needs sign-in as the manager it was sent to (P5-14). A POST with CSRF decides.
   - Every decision is confirmed to the manager ("approved Q-… version 2, total …", D10 rule 4). A request decided, changed or expired meanwhile gets its state instead.
   - Every written reply is audited (`manager_reply_classifications`, append-only, with the text).
6. **Telegram** (`adapters/providers/telegram/`): one platform bot.
   - **Customers** write to the business's own account; our bot is connected to it as a Telegram Business chatbot. Replies are allowed only within 24 hours of the customer's last message, and a refused send fails visibly ("the customer must message first").
   - **Managers** use our bot's own chat (P5-2, accepted under D9). They link with a deep link (`/start <route>_<code>`), get buttons and the link, and may reply in words.
   - **Enable:** a link code (10 minutes) sent as `/link CODE` from the business account, then the business adds the bot. The steps recommend new chats only and say Telegram Premium may be needed (P5-3).
   - **Routing keys:** `bc:`, `owner:`, `link:`, `r:`, `m:` (`mapping.py`).
   - `httpx` is allowed only inside provider packages.
7. **Also generic, for future tools:**
   - Short-lived public PDF links (`GET /api/v1/media/{token}`, 15 minutes, token hash only, hash re-checked) for tools that fetch attachments by URL.
   - Provider-hosted setup pages.
   - The `fill_form` and `open_url` screens.
   - Company-level settings in the SecretStore's platform namespace, with an operator command (`python -m sales_platform.ops platform-config set|show`, `platform-setup`).
   - Email helpers (`adapters/providers/email_common`: threading, new text, automatic mail, DKIM/SPF-aligned trust).
8. **Resend.** Administrators and sales operators (`messages:resend`) can send a failed customer message again. A copy becomes a new message, linked by `resent_from_id`, once per failed message. An `unknown` message is never resent.
9. **Migration `0013_provider_state`:**
   - New tables: `connection_routes`, `approval_reply_prompts`, `manager_reply_classifications`, `media_links`.
   - New columns: `outbound_messages.action_ref_id` and `resent_from_id`; `connections.pending_action`.
   - New event types and connection event kinds.
   - Grants: both roles manage routes and prompts; the worker updates connections and consumes customer action references; the API inserts outbox rows (Resend) and looks up a link token.

## Consequences
- **A new tool is one package and one entry point.** Its cards, steps, webhooks, buttons and replies work without core or portal changes; the contract test runs it.
- **Approval by message is never faster than safe.** Every AI-read decision costs the manager one YES. Without a model, written replies do nothing but say how to answer.
- **One bot for everyone.** The bot is shared by every business (manager approvals only) and has Telegram's limits (about 30 messages a second; a 429 is retried). This is an accepted D9 exception.
- **No live run.** Nothing in this phase has been checked against Telegram's real servers. Field names and behaviours marked "verify" in the plan (inline keyboards in business chats, Premium) stay to be confirmed in the first live run.
- **Still open** (no package needs them yet): P5-1, P5-4, P5-7, P5-8, P5-9, P5-10 and P5-12.

## Verification
See [handoff](../handoff.md), phase 5 section:
- unit tests;
- PostgreSQL tests (lifecycle, routes, roles, Resend, the reply flow through the webhook and worker, the signed link);
- portal tests;
- the migration cycle.
