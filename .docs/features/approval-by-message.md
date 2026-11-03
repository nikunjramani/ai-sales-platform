# Approval by message (D10, phase 5)
Decision: [ADR 0022](../adr/0022-one-click-providers-and-approval-by-message.md); D10 safety rules in [07 decisions](../plan/platform/07-decisions-needed.md). Code: [application/manager_replies.py](../../src/sales_platform/quotations/application/manager_replies.py), [domain/manager_replies.py](../../src/sales_platform/quotations/domain/manager_replies.py), [application/approval_links.py](../../src/sales_platform/quotations/application/approval_links.py).

## Three ways to decide in a tool
All three use the same approval core as the portal. It checks the approver role, no self-approval, one decision per request and the current version.

| Way | What happens | Needs AI? |
|---|---|---|
| **Button** | Approve decides at once. Reject / Change ask "reply with the reason"; that reply records it | No |
| **Written reply** to the review (or to any later message about it) | The AI suggests a meaning; the deterministic guards check it; then **"You want to … ? YES / NO"**. Only an exact YES (or the Yes button) records it | Yes (without a model, written replies are not read) |
| **Signed link** "Decide in the portal" | Opens `#/approve/<token>`; sign in as the manager it was sent to; one click on the version shown | No |

After every decision the manager gets what was recorded, e.g. "Demo Industrial: approved quotation Q-1A2B3C4D version 2, total 10450.38 INR." If the request was decided, changed or expired meanwhile, they get that state instead.

## The safety rules in code (D10)
1. **Only the linked account counts.** The reply comes from the active binding on that connection; a forwarded message from someone else is refused.
2. **Only a reply to one request.** The tool's ID of the replied message points to our outbound message, which belongs to one approval request (one task, one version). With no reply-to, the manager is told how to answer.
3. **When unsure, ask.** The guards turn a suggested approval into "Do you want to approve, reject or request a revision …?" when the reply:
   - is conditional, mixed, negated or doubtful ("approve if…", "ok but…", "not", "wait", "maybe", "change"…);
   - asks a question;
   - names another quotation number;
   - is empty.

   A reason must be copied from the reply; otherwise the manager's whole reply is the reason. If the AI is unavailable on the last attempt, the manager is asked; nothing is decided on a failure.
4. **Confirm what was recorded.** First "You want to …? YES / NO" (user answer P5-13; NO cancels, other text is read as a new reply), then "approved / rejected / revision requested …".
5. **The existing rules still apply**: approver role, no self-approval, one decision per request, no outdated version.

## What the AI sees
Only the reply text (new words only, at most 1,000 characters), the quotation number and the version (P5-6). Never prices, customer data, tokens or IDs. The reply sits inside a `<manager_reply>` data tag, and the answer must be strict JSON. The model is the business-wide `SALES_MODEL` (no new settings).

## Audit
Every written reply is recorded in `manager_reply_classifications` (append-only):
- the text and its SHA-256 (kept until the retention setting D17 is decided);
- what the AI suggested;
- which guard fired;
- the outcome (confirmation asked, clarify asked, not read, closed).

Decisions record their evidence as `<tool>:<connection>:<event>`, plus `:ai` for confirmed AI readings or `approval-link:<reference>`.

## Evaluation
[evals/manager_replies.jsonl](../../evals/manager_replies.jsonl) holds 80 synthetic replies (clear approvals, rejections, revisions, ambiguous and injection attempts, quoted history, emoji).
- Unit tests check that the guards alone stop an approval for every case marked `guard_blocks`, even with a reader that always says "approve", and let every clear approval through.
- With a model: `uv run python scripts/evaluate_replies.py --model <provider>:<model>`. The hard gate is **zero false approvals**; the target is 90 % exact.
- **Not run yet in phase 5** (no live model run was requested).

## Without a model (rules mode, P5-5)
- Buttons, the reason reply after a button, the signed link and the portal all work.
- A written reply only gets: "written replies are not read here. Please use the buttons on the approval message or open its link."
