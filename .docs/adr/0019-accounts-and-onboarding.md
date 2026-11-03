# 0019: Accounts, sign-up and business onboarding
Date: 2026-10-27
Status: accepted; implemented and merged (PR #7, phase 2 of the [platform plan](../plan/platform/README.md))
Extends [0002](0002-tenant-isolation.md), [0011](0011-durable-intake-and-jobs.md), [0013](0013-portal-identity-and-approval-core.md) and [0018](0018-connector-framework.md). Supersedes the line in 0013 that OIDC is required before any non-development deployment (user decision D1: email + password only). Plan: [phase 2](../plan/platform/implementation/phase-2-accounts.md), answers in its [user answers](../plan/platform/implementation/phase-2-accounts.md#user-answers-2026-10-27).

## Context
Until phase 2 a business existed only if a developer seeded it, people signed in through a development login, and API keys came from a script with owner credentials. The platform plan needs self-service: anyone signs up, creates a business, invites colleagues and connects their system, without scripts (D1, D3). Two such businesses must stay fully isolated.

## Decision
| Topic | Decision |
|---|---|
| Identity | Email + password, built in (D1, D2). A password login produces the same `VerifiedIdentity` and session cookie as every other login (ADR 0013). People are `users` rows; their `subject` is `user:<uuid>` (seeded demo people keep their email). The dev login stays for development only |
| Passwords | Argon2id, 64 MiB, 3 passes, 4 lanes, at most 4 hashes at once (user answer). 12 to 128 characters, a bundled list of common passwords (SecLists, MIT) and the email name refused (Q1, Q25) |
| Guessing | No account lockout (Q3). Rate limits per IP, email, person and business (Q4), counted in PostgreSQL so every API process shares them (Q5) |
| Enumeration | Sign-up, login, forgot password and every email link give one answer whatever the cause, and unknown addresses still pay for one password hash (equal timing). An existing address gets a notice instead of a second account (Q7) |
| Email links | Verification (24 h), reset (30 min), invitation (7 days) (Q6). The worker creates the single-use token at send time; only its SHA-256 is stored; the token travels in the URL fragment and in request bodies only. A crash after handing an email over records `unknown` and is never resent automatically |
| Mailers | A `Mailer` port chosen by entry point (`sales_platform.mailers`, `SALES_MAILER`). Phase 2 ships `console` (files under `var/storage/dev-mail/`); production refuses it until a real sender exists (phase 5) |
| Businesses | `create_business` makes the business, administrator and approver roles for the creator, the pricing row (currency and tax may be "set later", Q14), an empty PDF profile and an audit row. Country and time zone are required (Q15) |
| Status | `setting_up` → Go live → `active` (one-way, Q12); `suspended` only with owner credentials (Q13). While setting up nothing is pushed to customers, but API pull and manager messages work (Q11, Q27). A suspended business's keys are refused, its jobs wait and its settings are read-only |
| Go live | A checklist computed from real rows: details, PDF details, complete pricing, an approver, a customer connection and a test enquiry through each of them. Products are shown but not required until phase 3 |
| Access changes | The runtime role still cannot insert tenants or memberships (ADR 0002). Four `SECURITY DEFINER` functions owned by the owner role, with a fixed `search_path`, do it: `create_business`, `accept_invitation`, `change_member_roles`, `remove_member`. They trust only `app.subject` and check it against stored rows; the last administrator can never be removed, also under concurrent changes (row lock) (Q8, Q22, Q26) |
| Invitations | Several roles per invitation, administrators may invite administrators, only the invited address can accept, re-inviting replaces the open invitation (Q9). Only administrators see members (Q10) |
| API keys | Created and revoked in the portal (intake keys only), shown once, audited; `scripts/create_api_key.py` removed (Q17). A key sees only the enquiries and quotations it submitted (P1-Q9) |
| PDF | Renderer `quotation-pdf-v2` prints logo, legal name, address and tax ID when set (Q16). Profile text is checked with the renderer's own character rule when saved; the logo is re-encoded from pixels |
| Audit | `admin_events`, append-only, in the same transaction as each change, never secrets |

## Implementation notes
- **Migration `0010_accounts`:** users, credentials, email links, account notices, invitations, admin events, tenant profiles, rate-limit counters, the status trigger, nullable pricing values, portal key grants, per-subject session policies and the four functions. It downgrades; passwords, links, invitations, audit, profiles and statuses are lost then, and incomplete pricing rows are deleted.
- **Two tables beyond the shared contracts:** `account_notices` (emails without a link) and `rate_limit_counters` (Q5). Recorded in the contracts change log.
- **Narrow policies on global tables:** the API sees a person only through `app.subject`, `app.user_email`, `app.user_id`, `app.account_token_sha256` or membership of the current business. The worker never reads users or credentials.
- **Jobs without a business:** `jobs.tenant_id` may be NULL only for `send_account_email`; `ClaimedJob.require_tenant()` guards every other handler.

## Consequences
- Production stays blocked until a real mailer exists (phase 5) and hosting is chosen (D24).
- Rate limits cost one small database write per limited request.
- A business going live is final; stopping one is the operator's suspension.
- Retention settings (D17) come with phase 4, where data that needs them arrives (Q20).
