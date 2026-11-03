# Provider setup (company level, once per platform)
Some tools need settings shared by every business: our own bot, app or master account. These are **company-level settings**:
- They live in the secret store's **platform namespace** (`SecretStore.put_platform`, name `config`).
- They never go in `.env` for production and are never shown in the business portal.
- The provider package's own `platform_config_model` checks them.

Decision: [ADR 0022](../adr/0022-one-click-providers-and-approval-by-message.md).

## The operator command
```powershell
# Store (or replace) a provider's settings from a JSON file (or from stdin)
uv run python -m sales_platform.ops platform-config set <provider_key> --file settings.json
# Show them, secrets masked
uv run python -m sales_platform.ops platform-config show <provider_key>
# One-time company setup with the tool (e.g. register our webhook URL)
uv run python -m sales_platform.ops platform-setup <provider_key>
```
- `platform-setup` needs `SALES_PUBLIC_BASE_URL`: our public HTTPS address, which can be a tunnel in development (D24).
- The command uses the configured secret store (`SALES_SECRET_STORE`; locally the encrypted files under `SALES_STORAGE_DIR/secrets/platform/`).
- Delete `settings.json` after storing it.

## Telegram
1. **Create the bot.** In Telegram, talk to @BotFather → `/newbot`. Keep the token.
2. **Turn on Business mode** in BotFather (Bot Settings → Business Mode), so businesses can connect the bot to their accounts.
3. **Choose a webhook secret**: 16-256 characters, letters, digits, `_` and `-`. Telegram sends it with every update, and we refuse requests without it.
4. **Store the settings:**
   ```json
   {"bot_token": "123456:ABC...", "bot_username": "your_quote_bot", "webhook_secret_token": "a-long-random-secret"}
   ```
   Then run `platform-config set telegram --file settings.json`.
5. **Register the webhook:** `platform-setup telegram`. It calls `setWebhook` with `<SALES_PUBLIC_BASE_URL>/api/v1/hooks/p/telegram`, the secret and the updates we read (`message`, `callback_query`, `business_connection`, `business_message`, `edited_business_message`, `deleted_business_messages`).
6. **Check:** Connections → Telegram → Enable in a test business, then follow the steps (see [Telegram](../features/telegram.md)).

**Rotating the token or secret:** store new settings, then run `platform-setup` again.

**Limits:** one bot serves every business, at about 30 messages a second in total. Telegram answers 429 when we go faster, and the send job retries later.

## ERPNext
ERPNext needs **no company-level setup**: each business enables it under Connections with its own ERPNext address and API user (phase 6, [ERPNext](../features/erpnext.md)).

**Local sandbox (development and the integration tests, synthetic data only):**
```powershell
docker compose -p quotation-erpnext -f infra/erpnext/compose.yaml up -d   # ERPNext v16 on :8090
# wait until http://localhost:8090/api/method/ping answers {"message":"pong"} (first start ~10 min)
uv run python scripts/seed_erpnext_sandbox.py     # company, 3 items, prices, stock, customer; prints a key
```
In the portal, enable ERPNext with:
- address `http://localhost:8090` (http is accepted outside production);
- the printed key and secret;
- price list `Standard Selling`;
- warehouse `Stores - PS`;
- for orders, "Write orders to ERPNext" and customer `Sandbox Customer`.

If the sandbox answers "Encryption key is invalid" or 502 right after the first start, restart it (`... restart backend queue-short queue-long scheduler frontend websocket`) and run the seed again: it is safe to repeat.

To run the sandbox tests, set `SALES_TEST_ERPNEXT_URL`, `SALES_TEST_ERPNEXT_KEY` and `SALES_TEST_ERPNEXT_SECRET`, then `uv run pytest --run-integration tests/unit/ports/test_erp_writer_contract.py tests/unit/adapters/providers/erpnext/test_erpnext_sandbox.py`. They create sales orders, so never point them at a real ERPNext.

**A business's real ERPNext:**
- Use a dedicated API user with read access to Item, Item Price, Price List, Bin, Warehouse, UOM and Item Group, plus Customer and Sales Order (create) when orders are written.
- Try orders on a test company first.
- Production accepts only public `https://` addresses.

## Other tools
Email (SendGrid), WhatsApp and SMS (Twilio), Instagram and Messenger are not built yet. The company tasks they will need (Meta business verification, App Review, Twilio ISV profile, SendGrid domain authentication, privacy pages) are listed in the [phase 5 plan, section 8](../plan/platform/implementation/phase-5-one-click-providers.md#8-company-setup-once-by-us--start-now-they-take-weeks). They take weeks and are started only when the user decides to add those tools.
