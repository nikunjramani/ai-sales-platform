# Adding a provider (a tool: email, Telegram, WhatsApp, an ERP...)
**Status:** the framework is implemented (phase 1, [ADR 0018](../adr/0018-connector-framework.md); one-click tools and platform webhooks in phase 5, [ADR 0022](../adr/0022-one-click-providers-and-approval-by-message.md)). The working examples are [`api`](../../src/sales_platform/providers/api/__init__.py), [`simulated`](../../src/sales_platform/providers/simulated/provider.py), [`web_chat`](../../src/sales_platform/providers/web_chat/__init__.py) and the first real tool, [`telegram`](../../src/sales_platform/providers/telegram/provider.py).

A provider is one package. It never changes the API, the worker, the application services, the database or the portal. The core asks what a connection is **for** (its purposes) and what its provider **can do** (capabilities), never which provider it is. An architecture test enforces this.

## Words
| Word | Meaning |
|---|---|
| Provider | The tool package, e.g. `telegram`. One per tool, shared by all businesses |
| Connection | One business's use of a provider: a row in `connections` with purposes, settings, secret **references** and status |
| Purpose | What the connection is used for: `customer_messages`, `manager_approvals`, `catalogue` |
| Capabilities | What the provider can do: push or pull delivery, webhooks, buttons, attachments, limits |

## Steps
1. **Create the package** `src/sales_platform/providers/<key>/` (or a separate installable package). Provider SDKs are imported only here. Add each new third-party package to the adapters allow-list in [test_architecture.py](../../tests/unit/test_architecture.py); every version must be at least 14 days old.
2. **Implement `Provider`** ([ports/providers.py](../../src/sales_platform/integrations/ports/providers.py)). Subclass `ProviderDefaults`: every operation you do not write then raises `OperationUnsupported` explicitly.
   - `key`, `display_name`, `purposes`, `capabilities`, and `dev_only` (true means the provider is skipped in production). Phase 5: `cost` (a `CostNote`; with costs, Enable needs the administrator's tick) and `platform_config_model` (a Pydantic model of company-level settings, or None).
   - `verify_inbound(conn, secrets, request)`: check the signature or token on the **raw bytes** and raise `InboundRejected`. It runs before parsing.
   - `parse_inbound(conn, request)`: return normalised events, each with a stable `provider_event_id`:
     - `CustomerMessage`, with `files` to fetch later and `skipped` kinds; `from_business` marks what the business typed itself;
     - `ManagerAction` (enroll, decision, or confirm Yes/No);
     - `ManagerReply` (written text plus the ID of the message it answers);
     - `CustomerAction` (Accept/Decline buttons);
     - `ConnectionUpdate` (setup progress);
     - `DeliveryStatusUpdate` and `ConsentChange`.

     Never read a business or tenant from the body: the business is always the connection's. Raise `InvalidInbound` for content you cannot understand.
   - `send(conn, secrets, message)`:
     - Return `SendReceipt(SENT or AVAILABLE, provider_message_id)`. The ID must be **unique per message**.
     - Raise `SendRejected` for a permanent refusal.
     - Raise `SendUncertain` when the message may have been accepted; it is never resent.
     - Any other error means nothing was handed over, and the message is retried.
   - `catalogue_reader(conn, secrets)`: only for `Purpose.CATALOGUE`; it returns an `ProductSource`.
   - `test(conn, secrets)`: a quick check of the connection.
   - **One-click tools** (`Capabilities(self_service=True)`):
     - `provision` returns a `ProvisionResult`: ready, needs_action with one `PendingAction`, working with `poll_after`, or failed. It may carry public config, secrets (stored for you; the connection keeps references), routes to add or remove and a public `detail`. Never write the database yourself.
     - `resume(conn, secrets, action_id, submission)` continues after a step, a setup event (`ConnectionUpdate`) or a poll (`action_id="poll"`).
     - `deprovision` returns what the business must still do in the tool.
   - **One callback URL for the whole platform** (`Capabilities(platform_webhooks=True)`):
     - `verify_platform_inbound(secrets, request)` checks with company secrets (`secrets.get_platform(key, "config")`);
     - `routing_keys(request)` names the keys present (never guessed). Add your keys as routes in `provision`/`resume` results, or in `SendReceipt.routing_keys` for replies to a sent message;
     - `unrouted` may answer a sender no business claims.
   - **Optional:**
     - `handshake` (a verification GET);
     - `fetch_file` (customer files, with a size limit);
     - `enrollment_hint` (how a manager links, e.g. a deep link);
     - `provider_page` (HTML for a pop-up SDK);
     - `platform_setup` (operator one-time setup).
   - `send` receives `message.purpose` (customer or manager), `message.link` (the signed approval link) and, for tools with `attachments_by_url`, `message.attachment_url`. Render `message.actions` with `message.action_reference` as buttons when you have them.
   - Anything the provider does not support raises `OperationUnsupported`, never a fake success.
3. **Secrets:** read them only through `secrets.get(conn.tenant_id, conn.secret_refs[name])`. Never store them in `config`, log them or put them in errors.
4. **Register the factory** in `pyproject.toml`, then run `uv sync`:
   ```toml
   [project.entry-points."sales_platform.providers"]
   telegram = "sales_platform.providers.telegram:build"
   ```
   `build(environment: ProviderEnvironment) -> Provider`. The provider's key must equal the entry point name.
5. **Add a contract sample** in [test_provider_contract.py](../../tests/unit/providers/test_provider_contract.py): a body the provider accepts and how to sign it. Platform-webhook tools give `platform_sign` and `platform_secrets`. Tools whose `send` calls a real API set `network=True` and test sends in their package with recorded answers (see the [Telegram tests](../../tests/unit/providers/telegram/test_telegram.py), which use `httpx.MockTransport`). A test fails until the sample exists. Catalogue providers also run the [ERP contract suite](../../tests/unit/catalogue/ports/test_erp_contract.py).
6. **Fixtures and sandbox:** fixtures under `tests/data/providers/<key>/` (synthetic IDs), say whether they were recorded or written from the docs; a sandbox test that runs only when sandbox credentials are set. HTTP clients (`httpx`) are allowed only inside `adapters/providers/` (architecture test).
7. **Document:** the configuration reference (if the provider adds company-level settings, they are generic and never provider-named), a feature note, and the handoff.

## What happens without any core change
- **Inbound:** callbacks arrive on `POST /api/v1/hooks/{connection_id}/{webhook_token}`. They are verified, stored once and routed by event type: manager actions go to the same approval core the portal uses.
- **Outbound:** quotations, approval requests and notices are queued in the outbox and sent by `send_message`. The approval token is added at send time; the PDF is attached and hash-checked.
- **Product data:** a business's catalogue connection becomes its product source for matching, pricing and acceptance.
- **Portal:** the Connections page lists the new tool, and approvers can link an account on it.

- **Phase 5:**
  - **Enable cards:** `GET /connections/available` lists the tool for administrators, with its purposes and cost note. The portal renders its pending step (steps, a page, a provider page or a form from its schema), Test and Disable.
  - **Platform webhooks:** `/api/v1/hooks/p/<key>` with routing by key.
  - **Customer messages** go to conversations; customer buttons go to the acceptance core.
  - **Approval by message:** buttons, reason prompts, written replies read by the AI with a YES confirmation, the signed link and the recorded confirmations ([approval by message](../features/approval-by-message.md)).
  - **Email:** a future email package reuses [email_common](../../src/sales_platform/providers/email_common/__init__.py) (threading, new text, automatic mail, DKIM/SPF trust).

## How a second tool of a kind is added
A second chat or email tool is one more package with its own key and entry point. Nothing else changes: its settings live in its own platform config, and its keys in `connection_routes` under its own provider key.

## Product sources and order destinations (phases 6 and 7)
- **Product source:** declares purpose `catalogue` and `Capabilities(catalogue=CatalogueCapabilities(...))`, and implements `catalogue_reader`. With `sync=True` it also implements `fetch_catalogue` (a full `CatalogueFeed` in our words) and keeps its interval under the config key `sync_minutes`.
- **Order destination:** declares purpose `orders` with `OrderCapabilities(create_order=True, ...)` and implements `order_writer` (an `OrderWriter`). It raises `OperationUnsupported` when the business has not turned order writing on.

The core does the rest, by flags only: it copies the catalogue, checks prices live, writes orders with intent before call, reconciles by reference (if `reference_lookup`) and follows statuses (if `order_status`).

An ERP or shop (ERPNext, Odoo, Shopify...) would be one more package with `catalogue_reader` (`catalogue/ports/sources.py`) that passes the product source contract suite. None is built ([ADR 0027](../adr/0027-remove-erp-integration.md)).

## Hosted customer tools (phase 4)
A provider with `Capabilities(hosted_chat=True)` (website chat is the first) is served by our own public chat API and widget: administrators enable it with one click (`POST /connections/hosted-chat`), it gets a public handle, and the core routes its visitors' messages like any customer message. Nothing in the core or portal names the provider; `GET /connections/available` lists what can be enabled.
