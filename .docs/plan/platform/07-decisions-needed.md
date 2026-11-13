# 7. Decisions
The user answered most questions on 2026-10-23. **Decided** rows are binding. **Open** rows still need an answer before the phase in the last column; their recommendation is only a proposal. **Blocking for** refers to [08-phases.md](08-phases.md).

## Accounts and onboarding
| # | Question | Answer | Blocking for |
|---|---|---|---|
| D1 | How do people sign in? | **Decided:** sign-up and login with email (email + password) only. Other login options can be added later behind the same login port | Phase 2 |
| D2 | Which identity service? | **Follows from D1:** none; email + password is built into our existing login and sessions | — |
| D3 | Who may create a business? | **Decided:** anyone, no restrictions (self-service sign-up) | Phase 2 |
| D4 | Billing and plans? | **Decided:** out of scope | — |
| D1-D3 details | Passwords, limits, invitations, Go live, PDF details, API keys | **Decided (user, 2026-10-27):** phase 2 Q1-Q27, see phase 2 plan, user answers (git history). Notable: no account lockout (rate limits only, counters in PostgreSQL); demo people get a password from `.env`; API keys see only their own enquiries | Phase 2 |

## Connectors
| # | Question | Answer | Blocking for |
|---|---|---|---|
| D5 | Which tools first? | **Delegated to Claude** ("whatever is best and easiest to integrate"). Proposal: 1) website chat (ours, free); 2) the messaging provider's channels (WhatsApp, SMS) and email; 3) Telegram; 4) Instagram. Each is one adapter package (see D6/D7). **Changed (user, 2026-10-29, phase 5):** after website chat, **Telegram first**; phase 5 builds the generic framework plus Telegram only (no SendGrid, Twilio or Meta code yet, no live accounts); the others follow as packages later, see phase 5 user answers (git history) | Phase 4-5 |
| D6 | Meta directly or a third-party messaging provider? | **Decided:** use a third-party provider now, and **only one** that covers as much as possible; no collection of several providers | Phase 5 |
| D7 | Which provider? | **Decided (user, 2026-10-23): we provide the integrations ourselves, one click per business** ([09](09-one-click-integrations.md)). Twilio for WhatsApp, SMS and email (SendGrid), one subaccount per business; our own website chat and email addresses; Instagram, Messenger and Telegram connect directly (Twilio does not offer them). Each is one adapter package | Phase 5 |
| D8 | Where are tool secrets kept? | **Decided:** a cloud secret manager. Which cloud follows the hosting choice (D24); development uses a local stand-in behind the same port | Phase 1 |
| D9 | Can businesses share one bot or number? | **Decided:** no; each business gets its own accounts. With one-click Enable ([09](09-one-click-integrations.md)) **we create them** for the business. **Exception (user, 2026-10-29, P5-2):** manager approvals on Telegram come from our one platform bot, shared by managers of all businesses (never customers) | Phase 3 |
| D10 | How does a manager approve by message? | **Decided (user, 2026-10-23):** the manager may simply write a reply such as "approved", and the **AI decides** whether it means approve, reject or revise. **Safety rules (confirmed by the user, 2026-10-23):** 1) only the manager's own linked account counts; 2) only as a reply to one specific approval request, tied to that quotation and version; 3) if the AI is unsure, it asks ("approve, reject or request a revision of Q-123?") instead of deciding; 4) the system confirms what it recorded ("Approved Q-123 version 2, total 10,450 INR"); 5) the existing rules still apply (approver role, no self-approval, one decision per request, no outdated version). Buttons and the portal keep working too. **Details (user, 2026-10-29, phase 5):** every AI-read decision is confirmed first ("You want to approve Q-… version N?" [Yes]/[No] or an exact YES/NO reply); without a model (rules mode) free-text replies are ignored, buttons and the reason reply after a button still work; the AI sees only the reply, quotation number and version; signed links need sign-in | Phase 5 |

## Customers and conversations
| # | Question | Answer | Blocking for |
|---|---|---|---|
| D11 | How does a customer accept in chat? | **Decided:** buttons where the tool supports them; otherwise a secure link (confirmed by the user 2026-10-28, phase 4 Q1: GET never changes anything, single use, expires at valid until) | Phase 4 |
| D12 | Website chat: must visitors give contact details? | **Decided:** yes, an email or phone number is required. **No verification code (user, 2026-10-28, phase 4 Q2):** stored as claimed, never used for merge suggestions or sending | Phase 4 |
| D13 | Same person on two tools: merge automatically? | **Decided (user, 2026-10-28):** never automatically; administrators and sales operators merge by hand, suggested when the same verified email or phone appears; no undo in phase 4 | Phase 4 |
| D14 | Send a "we got your enquiry" message? | **Decided (user, 2026-10-28):** a per-business setting, on by default | Phase 4 |
| D15 | Opt-out wording and STOP handling | **Decided (user, 2026-10-28):** platform text in phase 4 (editable per business later); `STOP`, `UNSUBSCRIBE`, `OPT OUT` as the whole message opt out, `START` opts back in; always honoured | Phase 4 |
| D11-D15 details | Follow-ups, limits, files, wording, privacy, chat link | **Decided (user, 2026-10-28):** phase 4 Q1-Q14, see phase 4 plan, user answers (git history). Notable: rapid messages join an enquiry still being read (Q3); customers are told about withdrawn/expired quotations (Q10); chat visitors may send files (Q11); viewers see masked contact details (Q12); chat rate limits use the PostgreSQL counters (Q14) | Phase 4 |
| D16 | Staff typing replies in the portal | **Decided:** yes in the future, not part of this plan | — |
| D17 | How long are conversations and files kept? | **Decided:** each business's manager chooses (a business setting). Open: the default and the minimum/maximum allowed | Before real data |

## Product data
| # | Question | Answer | Blocking for |
|---|---|---|---|
| D18 | Which product sources first? | **Decided (user, 2026-10-23, changed from "spreadsheet import"): products are added by hand in the portal for now.** Spreadsheet import, ERP and shop connectors come in later phases | Phase 3 |
| D19 | May we store a business's catalogue (change ADR 0003)? | **Decided by D18:** yes. Products entered in our portal are stored by us; an ERP stays the authority when one is connected later (ADR 0003 to be amended in phase 3) | Phase 3 |
| D18-D19 details | Product fields, SKU, units, prices, stock, archive, permissions, default source | **Decided (user, 2026-10-28):** phase 3 Q1-Q23 all as proposed, see phase 3 plan, user answers (git history). Notable: only the four known units; prices > 0, excluding tax, one currency, now or future; 7-day stock age for new businesses; archived products cannot be quoted; administrators and sales operators edit | Phase 3 |
| D20 | Product synonyms per business | **Open (user not sure).** Recommendation: later; start with built-in full-text and similarity search | — |
| D21 | Which ERP or shop systems do businesses use? | **Decided (user, 2026-10-29): ERPNext first**, sandbox only until a pilot business exists ([ADR 0023](../../adr/0023-erpnext-product-source.md)). Others are more provider packages | Phase 6 |

## Sequencing and hosting (not answered yet)
| # | Question | Recommendation |
|---|---|---|
| D22 | Build follow-up messages now (API only) or with the conversation model (phase 4)? | **Done with phase 4** (built once; user started phase 4 on 2026-10-28) |
| D23 | Platform phases before orders (M6b)? | Yes: platform phases replace and extend M7; orders after phase 4 |
| D24 | Hosting (providers need a public HTTPS address for webhooks; it also decides the cloud for D8) | A tunnel (ngrok or Cloudflare) for development now; choose the cloud before real businesses use it |
