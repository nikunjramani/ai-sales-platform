# Website chat (phase 4)
Our own chat for a business's website: provider `web_chat`, capability `hosted_chat`. Decisions: [ADR 0021](../adr/0021-customers-conversations-and-website-chat.md).

## For the business
1. **Enable:** an administrator opens **Connections** and presses **Enable Website chat**. They get:
   - a chat link `https://<our address>/chat/<handle>` (the handle comes from the business name, with a number added if taken);
   - a snippet for their website: `<script src="https://<our address>/chat/widget.js" data-chat="<handle>" async></script>`.
2. **Settings:**
   - websites allowed to show the chat (at most 10, e.g. `https://www.example.com`; other websites get "not found");
   - a greeting (300 characters);
   - the address can be changed **once**, and the old link then stops working;
   - the chat can be switched off and on.
3. **Before Go live** visitors can chat, but nothing is pushed to them (phase 2 rule).

## For the visitor
- **Start:** a name plus an email **or** a phone number with country code. These are stored as claimed, not verified (D12, Q2).
- **Messages:** write messages (2,000 characters) and attach files: PDF, text, PNG or JPEG; 5 MB each, 5 per message, 20 a day. Files go through the same quarantine as API attachments; without OCR, unreadable files send the enquiry to review.
- **Answers:** our questions and the quotation card appear in the chat. **Download PDF**, then **Accept** or **Decline** after a confirmation.
- **Consent:** `STOP` stops messages and `START` resumes them.

## Rules and protection
| What | Rule |
|---|---|
| Bots (Q8) | a hidden field must stay empty; the start form must be open at least 2 seconds; no third-party captcha |
| Limits (Q9, Q14; PostgreSQL counters) | 10 messages a minute and 100 a day per chat; 20 new chats an hour per IP address per business; 20 files a day per chat |
| AI budget | over 200 chat readings per business per day, the enquiry goes to review without an AI call |
| Session | token `cs_<session>_<secret>`, kept in the visitor's browser for that chat only; only its SHA-256 is stored; 30 idle days |
| Business | always from the handle or the session (lookup policies reveal one row), never from the request body |
| Unknown, disabled, suspended, wrong website | the same 404 |
| IP addresses | stored only as an HMAC with `SALES_CHAT_IP_KEY` |
| Text | shown with `textContent` in a Shadow DOM; never HTML |
| Hosted page | CSP `default-src 'self'`; framed only by the chat's allowed websites |

## Public API (`/api/v1/public/chat/{handle}`)
`GET config`; `POST sessions`; `POST messages` (Bearer + `Idempotency-Key`; a replay gives 200 `duplicate`, other text 409); `GET messages?after=<seq>`; `POST actions/{ref}`; `GET documents/{message}.pdf`; `POST session/end`. CORS echoes only allowed websites, without credentials.

## Code
- **Backend:** `adapters/providers/web_chat`, `application/chat.py` (`HostedChatService`), `persistence/chat.py`, `api/public_chat.py`.
- **Admin:** `application/connections.py` (enable, settings, address, on/off) and `persistence/connections.SqlHostedChatAdmin`.
- **Widget:** `portal/src/widget/` (built by `npm run build` into `portal/dist/widget/widget.js`, about 3 KB gzip; served by the API when `SALES_PORTAL_DIST_DIR` is set).

## Adding another chat-like tool
A new provider package with `hosted_chat=True` (or, for an outside tool, webhooks in phase 5). The core, portal and routes do not change.
