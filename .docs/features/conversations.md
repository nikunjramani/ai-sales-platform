# Customers and conversations (phase 4)
Decisions: [ADR 0021](../adr/0021-customers-conversations-and-website-chat.md) and the phase 4 user answers (git history).

## In simple words
- **Contact:** one customer of a business.
- **Identity:** the customer's address on one tool, for example a website chat visitor or an API `customer_reference` sent with one API key.
- **Conversation:** every message to and from one identity, in order.
- **Follow-up:** when a customer writes again, the system decides whether the message continues their open enquiry or starts a new one. "cables price?" followed by "10 meter?" becomes **one** enquiry and one quotation.

## Which enquiry a message continues
`domain/conversations.follow_up_target`, applied inside the conversation's row lock (`persistence/conversations.py`):

| The customer's open enquiry is... | The message... |
|---|---|
| waiting for an answer to our question, asked less than 7 days ago | continues it; it goes back to `received` and is read again |
| still being read (`received`, `ready_for_extraction`) | joins it (Q3); the running round is redone with every message |
| in review, ready for quotation, quoted, or the question is 7+ days old | starts a new enquiry |
| none, or the API call had no `customer_reference` | starts a new enquiry (never continued, Q5) |

Only the same identity's enquiries count (Q4). If several qualify, the newest wins. The API answers `202` with the existing `enquiry_id` and `continued: true`, and a replay returns the same answer.

## Reading rounds
- **One round per reading:** each reading of an enquiry is one round (`workflow_runs.round`) with a fresh LangGraph thread. Earlier rounds stay as history.
- **What the reader sees:** every customer message separately, marked `[[message N]]` for models. Marker-like customer text is neutralised. Our earlier questions sit outside the customer data tag.
- **Verbatim check:** each message and document is a separate source, and our questions are never a source. A quantity from a later message is checked through `quantity_quote`.
- **Re-reading:** a round that finishes after the customer wrote again, or while a file is still in quarantine, is history only. The enquiry is prepared and read again.
- **Rules reader:** a bare answer ("10 metres") completes the single open item from the previous round (Q6). The literal demo needs a model provider.
- **Daily cap:** website chat enquiries over 200 readings a day go to review ("Daily reading limit reached") without a model call.

## What customers are sent
Fixed wording (`domain/messages.CUSTOMER_WORDING`), through the outbox, on push tools only. API customers read status, questions and quotations through the API as before.

| When | Message |
|---|---|
| A new enquiry, if the business setting is on (D14; Inbox page) | Thanks, we are preparing your quotation. |
| Our reading needs answers | We need a bit more information: + one line per question |
| The approved quotation is delivered | Your quotation {number} (version {v}) is ready: total {total} {currency}, valid until {date}. + PDF + Accept/Decline |
| Accepted / declined / prices changed | the confirmations of Q7 |
| Withdrawn / expired (Q10) | Quotation {number} has been withdrawn... / has expired (it was valid until {date})... (never the staff reason) |
| STOP, UNSUBSCRIBE, OPT OUT / START as the whole message (D15) | You will not receive more messages from {business}... / You will receive messages from {business} again. |

An opted-out identity gets nothing but the opt-out confirmation. Its other messages are recorded as `failed` with the reason "The customer opted out of messages".

## Accept/Decline from a chat
- **Buttons:** with "quotation ready", one reference per offered version and customer identity (`customer_action_refs`). The presser must be that identity; one answer per offer.
- **Secure link** (tools without buttons, Q1): `/q/<token>` shows the quotation and changes nothing. Accept/Decline with confirmation posts to `/api/v1/public/actions/<token>`. Single use, expires at "valid until".
- **Answer core:** the same as the API's (version and hash check, re-pricing, a revision when prices changed).

## Portal
| Screen | Who | What |
|---|---|---|
| Inbox (`#/inbox`) | every staff role | conversations, newest first, refreshed every 15 s; administrators switch the acknowledgement |
| Conversation | every staff role | the whole thread, with honest send status for our messages and the customer's consent |
| Contacts, contact | every staff role (viewers see masked email/phone, Q12) | identities, conversations, enquiries |
| Merge | administrators, sales operators (D13) | never automatic; suggested only for the same verified email/phone; confirmation; no undo; admin event |
| Quotation page | `conversations:view` | the enquiry's messages instead of the first text only |
Staff replies come later (D16).

## Limits and failure behaviour
- Two answers at once reopen the enquiry once (conversation row lock). Two first messages of a new customer create one contact (advisory lock).
- A customer's tool that is gone or disabled: nothing is queued, and the reason is logged.
- Downgrading migration 0012 loses follow-up rounds and the text of follow-up messages.
