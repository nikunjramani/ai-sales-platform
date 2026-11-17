# 0021: Customers, conversations and website chat
**Status:** accepted (phase 4, user decisions 2026-10-28); implemented on `feat/phase-4-conversations`. Extends ADR 0012 (the interpreter reads a transcript), ADR 0016 (answers may also come from customer action references) and ADR 0018 (first hosted customer tool, consent events).

## Context
Before phase 4 one enquiry was one message. A customer answering our question ("10 meter?") created a new, empty enquiry, and nothing was sent back to customers except the quotation over the API. Phase 4 adds customers, conversations and the first customer tool we host ourselves (website chat). Decisions D13-D15 and Q1-Q14 were answered by the user on 2026-10-28, plus four points asked during the work (plan, user answers (git history)).

## Decision
1. **Model.**
   - A *contact* is one customer of one business.
   - An *identity* is one address of a contact on one connection: a chat visitor ID, or an API `customer_reference` on one API key.
   - A *conversation* is one per identity. Its messages (`conversation_messages`) are append-only, inbound and outbound.
   - API enquiries without a reference get no contact (Q5).
2. **Routing.**
   - Every customer message goes through `IntakeStore.record`. It locks the conversation row, lists the open enquiries and asks an `IntentRouter` (pluggable handlers; later intents are more handlers).
   - The follow-up rule (`domain/conversations.follow_up_target`):
     - an enquiry waiting for our answer joins if our question is less than 7 days old (it is reopened);
     - an enquiry still being read joins without reopening (Q3);
     - review, ready-for-quotation and quoted enquiries never do;
     - the newest wins;
     - only the same identity's enquiries are considered (Q4).
3. **Rounds.**
   - Each reading of an enquiry is one round (`workflow_runs.round`, fresh LangGraph thread). The interpreter gets every customer message separately plus our earlier questions (trusted, outside the data tag).
   - Quotes must lie inside one message. A quantity from a later message goes in `quantity_quote`, which must also come from a customer message.
   - A round that missed a newer message or an uninspected file is kept as history, and the enquiry is read again.
   - The rules reader binds a bare answer to the single open item (Q6). The literal demo conversation needs a model provider.
4. **Customer messages** are fixed templates through the outbox, on push tools only (API customers read status through the API):
   - acknowledgement (per-business setting, on by default, D14);
   - questions;
   - quotation ready (PDF attached, Accept/Decline);
   - accepted, declined and prices-changed confirmations;
   - withdrawn and expired notices, without the staff reason (Q10);
   - STOP and START confirmations.
   An opted-out identity's message fails with the reason (D15). Each outbox message is also a thread row.
5. **Answers by reference.**
   - Each offered version gets one `customer_action_refs` row per identity.
   - Buttons carry its ID, and the presser must be that identity. Tools without buttons get a secure link: a token whose hash is stored, opening it changes nothing, a confirmed POST answers it, single use, expires at "valid until" (Q1).
   - The same answer core as the API (version and hash check, re-pricing, append-only record) consumes the reference in the answer's transaction.
6. **Website chat.**
   - **Provider:** `web_chat` declares the generic capability `hosted_chat`.
   - **Enabling:** an administrator enables it with one click and gets `/chat/<handle>` (from the business name, changeable once, Q13) and a snippet. Allowed websites and a greeting are settings.
   - **Visitors:** they give a name and an email or phone, stored as claimed (D12, Q2).
   - **Anti-bot (Q8):** an empty hidden field and a 2-second form time.
   - **Limits (Q9, Q11):** rate limits in PostgreSQL (Q14); 2,000 characters; 10 messages a minute and 100 a day; 20 chats per hour per IP; files (PDF, text, PNG, JPEG; 5 MB, 5 per message, 20 a day) through quarantine.
   - **Sessions:** the token is `cs_<session>_<secret>`, with only the hash stored and 30 idle days.
   - **Tenant:** always taken from the handle or the session, through lookup policies that reveal one row.
   - **Daily cap:** over 200 chat readings a day, the enquiry goes to review without an AI call.
   - **Widget:** plain TypeScript in a Shadow DOM.
7. **Staff.**
   - Inbox, conversation, contacts (`conversations:view`); viewers see masked email and phone (`contacts:details`, Q12).
   - Manual merge by administrators and sales operators (`contacts:merge`), suggested only on a verified email or phone, with no undo and an admin event (D13).

## Consequences
- **Gains:** follow-ups work on every tool that delivers customer messages, and a second chat tool needs one provider package and no core change.
- **Downgrade cost:** downgrading 0012 loses follow-up rounds and the text of follow-up messages (`enquiries.text` keeps the first message).
- **Release gates:** real visitor text must not reach the free Gemini tier (ADR 0017); retention defaults (D17) are needed before real data; staff replies come later (D16).
- **Deviations from the plan:** one action reference per offer (not per action); customer messages from webhook providers go through `process_inbound` only from phase 5; the conversation evaluation set was not added (the literal case is covered by the live run, the rules variant by PostgreSQL tests).
