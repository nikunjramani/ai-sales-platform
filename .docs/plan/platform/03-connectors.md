# 3. Connectors: one generic model for every tool
**Goal:** a manager links WhatsApp, Telegram, Instagram, email, a website chat or anything added later, from the portal, for customer enquiries, for approvals, or both. Adding a new tool means **adding one adapter package**. The core, the database and the portal screens stay unchanged.

## Three words, one meaning each
| Word | Meaning | Example |
|---|---|---|
| **Provider** | A kind of tool we support, implemented by an adapter package | `telegram`, `whatsapp_cloud`, `email_postmark`, `web_chat`, `api` |
| **Connection** | One business's configured account on a provider | "Acme's Telegram bot @acme_sales" |
| **Purpose** | What a connection is used for, chosen by the manager | `customer_messages`, `manager_approvals`, `catalogue`, later `payments` |

These replace today's three meanings of "channel" (see [01](01-current-state.md)).

## Provider registry (no provider names in the core)
- **Discovery:** each adapter package registers itself with a standard Python *entry point* (group `sales_platform.providers`). Installing the package makes the provider available; nothing else in the code lists providers. This is the same idea as LangChain's `init_chat_model` for AI models.
- **What a provider declares** (one small class):
  - `key`, `display_name`, `icon`.
  - `purposes` it can serve, e.g. Telegram serves customer messages and manager approvals.
  - `config_schema`: a Pydantic model. The portal renders the setup form **from this schema**, so there is no per-provider portal code. Secret fields are marked write-only.
  - `capabilities`, which the core uses to adapt its behaviour without naming providers:
    - text, attachments and maximum size;
    - buttons, or reply commands only;
    - threads;
    - whether it can message a customer first, and any reply window (e.g. WhatsApp's 24 h);
    - templates required;
    - delivery receipts;
    - how the manager proves control of an account (enrollment code by message, OAuth, email link).
  - The **operations** it implements; unsupported ones return a clear "unsupported":
    - `verify_inbound(request)`: checks the provider's signature on the raw request;
    - `parse_inbound(request)`: returns normalised events;
    - `send(message)`: returns a receipt; raises "rejected" (permanent) or "uncertain" (unknown outcome);
    - `test_connection()`: used by the portal's "Test" button;
    - `setup(connection)` and `teardown(connection)`: e.g. register or unregister the webhook with the provider.
- **Existing adapters become providers:** the API intake and pull delivery become provider `api`; the simulated phone becomes provider `simulated`, development only.

## Connections (per business, stored by us)
- **New table `connections`**, under tenant row-level security. Columns:
  - `id`, `tenant_id`, `provider_key`, `display_name`;
  - `purposes[]`;
  - `config` (JSON, validated by the provider schema);
  - `secrets` (encrypted);
  - `webhook_token` (random, only its hash stored);
  - `status` (`setting_up`, `active`, `error`, `disabled`), `last_error`, `created_by`, `created_at`.
- **Secrets** (provider tokens, passwords):
  - encrypted at rest with a key the database never sees (envelope encryption: a master key from the environment or a cloud key service, [decision D8](07-decisions-needed.md));
  - write-only in the portal: never shown again after saving;
  - never logged.
- **One business may have many connections**, and several on the same provider (two WhatsApp numbers).
- **Today's `connection_id` strings** on `inbox_events` and `channel_bindings` become references to `connections.id`. A migration creates `api` and `simulated` connections for the existing demo data.

## Inbound: one route for all providers
`POST /api/v1/hooks/{connection_id}/{webhook_token}`
1. Find the connection and check the token (constant-time). The business comes **from the connection**, never from the message body. This fixes the current simulated route.
2. `provider.verify_inbound` checks the provider's own signature on the raw body, before any parsing.
3. `provider.parse_inbound` returns normalised **events**:
   - `CustomerMessage` (sender, text, attachments, provider message ID, conversation/thread, reply-to);
   - `ManagerAction` (enroll code, approve/reject/revise with the action reference);
   - `DeliveryStatus` (sent/delivered/failed for an outbound message);
   - `ConsentChange` (stop/start).
4. Each event is de-duplicated in `inbox_events` by (business, connection, provider event ID, event type). It is then routed: customer messages go to [conversations](04-customers-and-conversations.md), manager actions go to the existing `ChannelActionService`, and delivery statuses go to the outbox.

Providers that are polled rather than called (e.g. an IMAP mailbox) use the same events from a scheduled job.

## Outbound: one outbox for every message
- **New `outbound_messages` outbox** with the honest statuses of today's delivery outbox:
  - pending, sending, sent, delivered, failed, unknown;
  - **unknown is never resent automatically.**
- **Every message the system sends goes through it:**
  - clarification questions (today never sent);
  - the quotation PDF;
  - approval requests and notices to managers;
  - later, order and payment updates.
- **One rendering step for all messages:**
  - a message is built once from a **template** (fixed wording, never AI-written; ADR 0017);
  - it is adapted to the provider's capabilities: buttons where supported, otherwise numbered reply commands, otherwise a link;
  - `ApprovalNotifier` and `DeliveryChannel` become thin uses of this single sender, so existing tests keep their meaning.
- **Reply windows and templates** (e.g. WhatsApp) are enforced from capabilities. If a message can't be sent now, it is not queued silently; staff see "needs a template" or "customer must message first".

## Manager approval on any tool (generic)
- **The existing secure flow stays and works for every provider.** The steps are an enrollment code, then a binding to the user, then single-use action references bound to task, version and binding.
- **Linking a manager account:**
  1. The manager opens Connections → "Link my account" and gets a 10-minute code.
  2. They send it to the business's bot, number or address from their own account.
  3. Account linked.
  - Providers with OAuth can replace step 2.
- **Approving:**
  - **Buttons** (Telegram, WhatsApp interactive messages): the button carries the action reference.
  - **Reply commands** where buttons don't exist ("reply APPROVE 4821").
  - **A signed approval link that opens a one-click portal page.** This works on every tool, including SMS and plain email, and is the universal fallback.
- **Approving by a written reply (decided, D10):** the manager may just write "approved" or similar, and the AI decides whether it means approve, reject or revise. Safety rules (confirmed by the user): only from the manager's linked account, only as a reply to one specific approval request, unclear replies get a follow-up question, the system confirms what it recorded, and the existing approval rules still apply.
- **The rules stay in `ApprovalService`:** approver role, no self-approval, current version and hash, one decision per task.

## One-click Enable
Businesses don't configure tools themselves: they press **Enable** and the provider's `provision` operation creates the accounts for that business. Only the steps a platform insists on are left to them (e.g. one Meta pop-up). See [09-one-click-integrations.md](09-one-click-integrations.md).

## Website chat (provider `web_chat`, our own)
- **Embedding:** the manager copies a script snippet into their website. It shows a chat bubble.
- **Connection settings:** allowed website addresses, greeting text, colours; whether the visitor must leave an email or phone (D12).
- **Visitors:** each visitor gets an anonymous session token, held in the browser and scoped to the connection. Messages travel over our public API. The first version polls; live updates can follow.
- **Same rules as other tools:** the visitor is a customer contact. Quotations appear in the chat with a download link, and accept/decline uses signed links (D11).
- **Why first:** it is free and needs no provider approval, which makes it the quickest real channel to build and demo.

## Adding a new tool later (the whole checklist)
1. Create a package, e.g. `quotation-provider-instagram`, with a provider class: schema, capabilities, verify/parse/send/test, and an entry point.
2. Add it to the `providers` extra in `pyproject.toml`, the same pattern as AI providers.
3. Run the shared **provider contract tests**:
   - a signature is required and a forged one is refused;
   - de-duplication works;
   - send outcomes map correctly;
   - business isolation holds;
   - unsupported operations are explicit.
4. Document the provider's account steps for managers.

There is no core, database, worker or portal change.

## Providers: first candidates and what each needs
| Provider | Customer messages | Manager approval | Needs from you/the business | Cost / approval |
|---|---|---|---|---|
| Website chat (ours) | ✅ | via signed links | Nothing external | Free |
| Telegram bot | ✅ | ✅ buttons | Create a bot with BotFather (token) | Free; no review |
| Email (inbound webhook + sending) | ✅ | links (reply approval open, D10) | A mail provider account and a domain (SPF/DKIM) | Usually a free tier |
| WhatsApp Business (Cloud API) | ✅ | ✅ buttons | Meta Business verification, a phone number, approved templates for messages outside 24 h | Meta charges per conversation; review takes days |
| Instagram messaging | ✅ | — (not for managers) | Instagram professional account linked to a Facebook page, Meta app review | Free API; review needed |
| SMS | ✅ | links | An SMS provider and a number | Paid per message |

**Decided (D6):** one third-party messaging provider, not several. **Open (D7):** no single provider covers every tool. The proposal is Twilio (WhatsApp, SMS, email via SendGrid) plus Telegram directly, which is free and simple. Each is still just one adapter package.
