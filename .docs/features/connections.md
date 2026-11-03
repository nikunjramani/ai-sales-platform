# Connections (phases 1, 4 and 5)
Decisions: [ADR 0018](../adr/0018-connector-framework.md) (framework), [ADR 0021](../adr/0021-customers-conversations-and-website-chat.md) (website chat), [ADR 0022](../adr/0022-one-click-providers-and-approval-by-message.md) (one-click tools, platform webhooks, approval by message). Adding a tool: [adding providers](../development/adding-providers.md). Tools: [website chat](website-chat.md), [Telegram](telegram.md). Approving in a tool: [approval by message](approval-by-message.md).

## What it does
Each business's tools are **connections**. A connection is one use of a **provider**, the tool package (`api`, `simulated`, `web_chat`, `telegram`, later email, WhatsApp, ERPs). The core decides by what a connection is for (its purposes) and what its provider can do (capabilities), never by the provider's name.

| Today | Connection | Purposes |
|---|---|---|
| Each API key | `api`, account `api-key:<id>` (created and disabled together with the key) | customer messages (pull) |
| The demo businesses' simulated phone and demo catalogue | `simulated`, account `simulated-local` (seed, development only) | manager approvals, product data |
| Website chat (phase 4) | `web_chat`, one click | customer messages |
| Telegram (phase 5) | `telegram`, one click and one step in Telegram | customer messages, manager approvals |

## One-click tools (phase 5)
1. **Enable.** An administrator presses Enable on the tool's card (Connections, "Add a tool"). The cards come from the installed providers, so a new tool appears without a portal change.
   - A tool that may cost money needs the tick "I understand this may incur costs". Telegram is free.
2. **We set it up.** The provider creates what the business needs and answers in one of four ways:
   - **ready**: the tool works now;
   - **one step left** (`pending`): the business must do something;
   - **working**: the tool is still reviewing; we ask again later (`poll_connection`);
   - **failed**: with the reason.
3. **The step.** The portal shows it in one of four ways:
   - **steps** to follow in the tool itself, with a one-time code shown only in this answer (press Enable again for a new one);
   - **a page to open** (e.g. a login);
   - **a setup page** our API serves for the package;
   - **a form** built from the package's schema. Secret fields are password inputs and are never shown again.

   The list is asked again every 5 seconds while a tool is being set up. When the tool confirms (a setup event on its webhook), the step disappears and the card shows what the tool reported, e.g. "Customers can write to @acme".
4. **Test and Disable.**
   - **Test** asks the tool whether it still works.
   - **Disable** needs the tool's name typed. It removes the tool's routes and says what the business must still do in the tool itself (e.g. remove our bot).

## Flows
- **Per-connection webhooks** (`POST|GET /api/v1/hooks/{connection_id}/{webhook_token}`):
  1. The token is checked against its stored SHA-256.
  2. The provider verifies the signature on the raw body.
  3. The provider parses events. The business is always the connection's.
  4. Customer messages go to the conversation at once. Other events are stored once, each with a `process_inbound` job.
  5. A GET is a verification handshake.
- **Platform webhooks** (`POST|GET /api/v1/hooks/p/{provider_key}`, phase 5) serve tools with one callback URL for everyone (one bot, one app):
  1. The provider checks our company-level secret.
  2. The request's own routing keys name the business (`connection_routes`, exact match).
  3. A genuine request is always answered 200, even when no business claims it or it cannot be read. Only a bad secret gets 401.
- **Customer messages from webhooks:** the same road as website chat, with the same limits; see [conversations](conversations.md).
- **Outbound:** quotations, approval requests, questions and notices are rows in `outbound_messages`, sent by the `send_message` job:
  - The status is honest: `unknown` is never resent automatically.
  - Single-use tokens (approval buttons, the signed link, Yes/No) are created only at send time.
  - Each quotation delivery mirrors its message's status.
- **Resend (phase 5):** administrators and sales operators can send a failed customer message again from the conversation ("Send again"):
  - a copy becomes a new message, once per failed message;
  - an `unknown` message is never resent.
- **Product data:** each business's active catalogue connection is its product source. With none, an enquiry goes to review with "No product source is connected".

## Portal
**Connections** (`#/connections`; `#/channels` redirects there):
- Administrators see every tool, with what it reported, its last error and its pending step. They can enable website chat and one-click tools, and test or disable them.
- Approvers see the tools they can link and "Link my account on <tool>". The one-time code works only on that tool. Tools that offer a link (Telegram) show "Open the link" instead of a code to type.
- Linked accounts show the tool's name.

## Company-level settings (operators)
Settings shared by every business, such as our bot's token, live in the secret store's platform namespace and never in the business portal. See [provider setup](../operations/provider-setup.md).

## Development
- **Signing and tokens:** `SALES_SIMULATED_CHANNEL_SECRET` (development) is, through the seed, the demo phone's signing secret, stored in the local secret store. The demo phone's webhook token is derived from it.
- **The simulated phone (phase 5)** receives every manager message as a file with its `message_id`. A `reply` event answers one of them by that ID, which exercises approval by message without a real tool.
- **Tools:** `scripts/simulated_channel.py` and the demo page both use the generic hook route (the old alias was removed in ADR 0026 step 2).
- **Secret store:** `SALES_SECRET_STORE_KEY` encrypts the local secret store. `init_local_env.py --add-missing` creates it.

## Security
- See [security](../operations/security.md#implemented-controls-phase-1-connector-framework-in-progress).
- **Phase 5:**
  - platform requests are verified with company secrets before anything is read;
  - routes are exact keys, unique across businesses;
  - a key of another business rolls a setup step back;
  - one-time codes are stored only as hashes;
  - secrets go only to the secret store.

## Not yet
Email, WhatsApp, SMS, Instagram and Messenger packages (each one package when its accounts exist; open points P5-1, P5-4, P5-7 to P5-10 and P5-12 are asked with the first that needs them).
