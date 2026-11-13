# Telegram (phase 5)
Decision: [ADR 0022](../adr/0022-one-click-providers-and-approval-by-message.md). Package: [adapters/providers/telegram/](../../src/sales_platform/providers/telegram/provider.py). **Not live-verified:** phase 5 had no accounts. Updates are taken from the Bot API documentation ([fixtures](../../tests/data/providers/telegram/updates.json)), and the first live run must confirm the points marked "verify" in the phase 5 plan (git history).

## In short
- **Customers** write to the business's **own** Telegram account. Our one bot is connected to it as a Telegram Business chatbot and answers there.
- **Managers** approve in our bot's **own chat**, shared by managers of every business (user answer P5-2). Every message there starts with the business's name.
- **Cost:** free. Telegram Premium may be needed for the business account to connect a bot (P5-3, to be verified).

## What the business does (administrator)
1. Connections → Add a tool → **Telegram** → Enable.
2. Follow the steps shown (the code is valid for 10 minutes; Enable again for a new one):
   1. Open Telegram on the account customers should write to.
   2. Send `/link <code>` to our bot.
   3. Settings → Telegram Business → Chatbots → add our bot. We recommend **new chats only** (exclude existing contacts).
   4. Allow **Reply to messages**.
3. The card turns **Active**: "Customers can write to @your_account".

If replies are switched off, or the bot is removed in Telegram, the connection shows **Needs attention** with the reason. Adding the bot again with "Reply to messages" makes it active again.

## Customers
- Their messages become enquiries and follow-ups exactly like website chat, with the same limits:
  - 10 messages a minute and 100 a day per customer;
  - 200 AI readings per business per day (shared by all customer tools);
  - over a limit, a message is kept but goes to review unread.
- **Files:** PDF, text, PNG and JPEG up to 5 MB are read. Voice, video and stickers are noted ("not read") and skipped.
- **Answers:** questions, the quotation PDF with **Accept / Decline** buttons, and the confirmations go back in the same chat.
- **24 hours:** Telegram lets the bot reply only within 24 hours of the customer's last message. A quotation approved later fails with "The customer must message first". When the customer writes again, staff press **Send again** on the conversation.
- **The owner's own messages:** what the business types itself in a chat is kept in the conversation as "Staff, on the tool". The AI reader sees it as our side's words, never as the customer's.
- **Edited messages** are ignored (only logged).

## Managers (approvers)
1. **Link:** Connections → Linked accounts → **Link my account on Telegram** → **Open the link** on the phone (within 10 minutes) → press Start.
2. **Reviews** arrive from our bot with:
   - **Approve / Reject / Change** buttons. Reject and Change ask for the reason; your reply to that question records it.
   - "Decide in the portal: <link>", which needs sign-in as you.
3. **Writing instead** works only when the business has an AI model set up. Reply to the review message; the bot asks "You want to approve Q-… version N? Reply YES or NO" (with Yes/No buttons), and nothing is recorded before YES. See [approval by message](approval-by-message.md).
4. **Writing without replying** to a message gets a hint: reply to the approval message, or use its buttons.

## Operator setup (once)
See [provider setup](../operations/provider-setup.md#telegram): create the bot with BotFather, store its settings with `python -m sales_platform.ops platform-config set telegram`, then `python -m sales_platform.ops platform-setup telegram` (sets the webhook).

## Disable
Connections → Telegram → Disable… (type the name). Our routes are removed at once. **The business must still remove our bot** in Settings → Telegram Business → Chatbots: we cannot do that for them.
