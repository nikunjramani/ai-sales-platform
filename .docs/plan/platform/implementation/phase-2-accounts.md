# Implementation plan, phase 2: accounts and onboarding
**Status:** all open questions answered by the user on 2026-10-27 (see [User answers](#user-answers-2026-10-27)); implementation in progress on branch `feat/phase-2-accounts`. Where an answer differs from a proposal below, the answers section wins. It follows the binding [shared contracts](00-shared-contracts.md); where this plan needs something the contracts do not list, it says so in [Proposed changes to shared contracts](#15-proposed-changes-to-shared-contracts). Every value marked **(to confirm)** is a proposal only and must be confirmed by the user before code is written (AGENTS.md "Ask, do not assume").

Background: [02 accounts and onboarding](../02-accounts-and-onboarding.md), [07 decisions](../07-decisions-needed.md), [08 phases](../08-phases.md). Paths are relative to `src/sales_platform/` unless they start with another top-level folder.

---

## 1. Goal
A manager opens the portal, **signs up with email and password**, verifies the email, **creates a business**, fills in settings (business details, PDF details, pricing, quotation validity), **invites colleagues**, **creates an API key** and sends a **test enquiry**, then presses **Go live**. No script is needed at any step. Two self-created businesses stay fully isolated.

Phase 2 is verified by (from [08](../08-phases.md)): "New business from zero to a test enquiry without scripts; isolation tests between two self-created businesses".

## 2. Scope
### In scope
| Area | What is built |
|---|---|
| People | `users` table (global, not tenant-owned), `user_credentials` (Argon2id hash), email normalisation |
| Sign-up and login | Sign-up, email verification, login/logout on the **existing** session + CSRF system, change password, forgot/reset password |
| Abuse protection | Rate limits per IP and per email, temporary account lockout, enumeration-safe answers, equal timing |
| Business creation | `create_business(...)` SECURITY DEFINER function (owner-owned, only `quotation_app` may run it) |
| Business status | `tenants.status`: `setting_up`, `active`, `suspended`; Go live gate |
| Settings | Business (name, country, time zone), PDF details (`tenant_profiles`: legal name, address, tax ID, logo), pricing (incl. `quote_validity_days`) |
| Members | Invite (email, roles, expiry, single-use token), list, change roles, remove; never remove the last administrator; audit trail |
| API keys | Create (shown once) and revoke from the portal; `scripts/create_api_key.py` removed |
| Setup checklist | Computed from real database state; drives the portal home until live |
| Email | `Mailer` port, console (dev) adapter, `send_account_email` job |
| Dev tools | Dev login and the demo seed stay (development/test only) |
| Database | Migration `0010_accounts` with full downgrade; readiness head updated |
| Permission | `members:manage` (administrator) |

### Out of scope
- Other login methods (Google, OIDC, magic links, 2FA/passkeys). D1: email + password only; they can be added later behind the same `VerifiedIdentity` port.
- Billing and plans (D4).
- A real email sender. SendGrid comes in phase 5 (D7); phase 2 ships the console adapter. **Consequence:** phase 2 cannot be used in production, because production refuses the console mailer (see §8).
- Changing the email address of an account, deleting an account, deleting a business, data retention settings (D17; see Q20).
- Platform operator screens (suspending a business is an owner-credential script only, if confirmed: Q13).
- Product data (phase 3), conversations and website chat (phase 4), connectors' Enable cards (phase 5).

## 3. Decisions used and open questions
### Decisions used (binding)
| # | Decision | Effect on this plan |
|---|---|---|
| D1 | Email + password sign-up and login only | One `password` identity provider; dev login stays for development |
| D2 | No external identity service | Hashes, tokens and sessions are ours |
| D3 | Anyone may sign up; nothing is sent to customers before Go live | No invite-only sign-up; status `setting_up` gates customer sends |
| D4 | Billing out of scope | No plan limits |
| D8 | Secrets in a secret manager behind a port (phase 1) | A future real mailer reads its API key through `SecretStore`, not a setting |
| D17 | Each manager chooses retention; default/min/max open | Not built in phase 2 (Q20) |
| D24 | Hosting not chosen | The public portal URL used in email links is a setting; dev uses localhost |
| ADR 0002 | Least privilege, forced RLS, non-owner runtime roles | Narrow definer functions for access changes (§7.3) |
| ADR 0013 | Provider-independent login, BFF session cookie, CSRF header | Password login produces a `VerifiedIdentity` and reuses `SessionService` unchanged |
| ADR 0014 | Portal feature/layer structure | New `account` and `business` features |
| 00-shared-contracts | Names, tables, migration `0010_accounts`, job `send_account_email`, permission `members:manage`, portal features `account` and `business` | Used as written |
| [02](../02-accounts-and-onboarding.md) | Creator gets administrator **and** approver; quotation validity default 7 days (M6 user decision) | `create_business` inserts both roles; validity default 7 |

### User answers (2026-10-27)
The user answered every question on 2026-10-27. Where the answer differs from the proposal, it is in **bold** and the plan below is changed accordingly.

| # | Answer |
|---|---|
| Q1 | 12 characters minimum, 128 maximum, common-password list and "contains the email name" refused (as proposed) |
| Q2 | Yes: sign-in before verification, but no business creation or invitation acceptance until verified (as proposed) |
| Q3 | **No lockout.** Only the rate limits (Q4) slow down guessing. Dropped from the plan: `failed_attempts`/`locked_until`, `LockoutPolicy`, `after_failed_login`, the `LOCKED_OUT` email purpose and its template |
| Q4 | Rate limits as proposed |
| Q5 | **PostgreSQL counter table** (shared by all API processes, survives restarts) instead of in-memory counters. New global table `rate_limit_counters`, keys stored as SHA-256 only; `RateLimiter` port with a SQL adapter |
| Q6 | Verification 24 h, reset 30 min, invitation 7 days (as proposed) |
| Q7 | Same "check your email" answer; the existing owner gets a notice (as proposed) |
| Q8 | No owner rights; `tenants.created_by` recorded (as proposed) |
| Q9 | Invitations as proposed |
| Q10 | Member list: administrators only (as proposed) |
| Q11 | Status gate and Go-live checklist as in §6 (as proposed) |
| Q12 | No way back from `active` to `setting_up` (as proposed) |
| Q13 | Suspension only by an operator script with owner credentials (as proposed) |
| Q14 | Pricing "set later" with visible suggestions (as proposed) |
| Q15 | Country and time zone both required (as proposed) |
| Q16 | PDF: legal name, address (required for Go live), optional tax ID with label, optional logo PNG/JPEG ≤ 1 MB (as proposed) |
| Q17 | Only intake keys from the portal; `scripts/create_api_key.py` removed (as proposed) |
| Q18 | Email normalisation as proposed (no Gmail dot/plus folding) |
| Q19 | `SALES_MAIL_FROM`, `SALES_PUBLIC_PORTAL_URL`; console mailer in development; production blocked until phase 5 (as proposed) |
| Q20 | Retention setting in phase 4 (as proposed) |
| Q21 | No total limit; 5 creations per day per person (as proposed) |
| Q22 | Members may leave by themselves, except the last administrator (as proposed) |
| Q23 | Profile: display name and password only (as proposed) |
| Q24 | **Seeded demo people also get a password**, read from the new setting `SALES_DEMO_PASSWORD` (development/test only; `init_local_env.py --add-missing` fills it with a random value; never in Git). Dev login stays |
| Q25 | Bundled offline common-password list (as proposed) |
| Q26 | Narrow SECURITY DEFINER functions (as proposed) |
| Q27 | API pull delivery allowed while `setting_up` (as proposed) |
| Argon2id | t=3, m=64 MiB, p=4, at most 4 concurrent hashes (as proposed) |
| P1-Q9 (from phase 1) | **An API key sees only the enquiries and quotations it submitted itself** (status, quotation JSON/PDF, response). Another key of the same business gets 404 |

Commits: on `feat/phase-2-accounts`, one commit per work item after checks pass, pushed, with a draft PR to `main`; the user merges.

### Open questions (answered 2026-10-27; kept for reference)
Every row has a proposal. None of these proposals may be coded until confirmed.

| # | Question | Proposal **(to confirm)** | Why it matters |
|---|---|---|---|
| Q1 | Minimum password length | 12 characters, maximum 128, no composition rules, refuse a bundled list of common passwords and passwords containing the email's local part. Alternative: 15 (the current NIST SP 800-63B-4 minimum when a password is the only factor) | Security vs. ease of sign-up |
| Q2 | May someone log in before verifying the email? | Yes, but until verified they cannot create a business or accept an invitation; a banner offers "resend email" | Without it, sign-up looks broken if the email is slow |
| Q3 | Account lockout | 10 failed passwords in a row lock the account for 15 minutes; success resets the counter; locked answers look exactly like wrong-password answers; the person gets a "too many attempts" notice email | Lockout can be abused to lock someone out, so it is temporary |
| Q4 | Rate limits | Login: 30 attempts / 10 min per IP and 10 / 10 min per email. Sign-up: 10 / hour per IP. Forgot password: 5 / hour per email, 20 / hour per IP. Resend verification: 1 / minute and 5 / day per user. Invitations: 50 / day per business. Invitation preview/accept: 30 / 10 min per IP. Create business: 5 / day per user | Stops guessing and email flooding |
| Q5 | Where rate-limit counters live | Per account lockout in PostgreSQL (durable, shared by all API processes); IP/email throttles in memory per API process behind a `RateLimiter` port, replaced by a shared store when hosting is chosen (D24). Alternative: a PostgreSQL counter table now | With several API processes, in-memory limits multiply |
| Q6 | Token lifetimes | Email verification 24 h, password reset 30 min, invitation 7 days | Shorter is safer, longer is friendlier |
| Q7 | Sign-up with an email that already has an account | Same "check your email" answer; we email the existing owner "someone tried to sign up with your address; log in or reset your password" | Prevents finding out who has an account |
| Q8 | Is there an "owner" with special rights? ([02](../02-accounts-and-onboarding.md) marks it "proposed") | No special rights in phase 2. `tenants.created_by` records the creator. The "never remove the last administrator" rule prevents lock-out | Owners complicate transfers and removal |
| Q9 | Invitations | One invitation may carry several roles (e.g. sales_operator + approver); administrators may invite administrators; only the invited email can accept; re-inviting the same email revokes the older open invitation | Defines who can join and as what |
| Q10 | Who may see the member list | Only people with `members:manage` (administrators) | Emails of colleagues are personal data |
| Q11 | What `setting_up` blocks, and which checklist items Go live needs | See §6 table | D3 says "nothing is sent to customers until Go live", but the test step needs enquiries to work |
| Q12 | Can a live business go back to `setting_up`? | No | Keeps the status simple |
| Q13 | Suspension | Status exists; only an operator script with owner credentials sets/clears it; while suspended: intake refused, nothing sent, portal read-only | Needed for abuse handling, but no operator UI yet |
| Q14 | Pricing "set later" | Creation form asks currency and tax rate, each may be "set later". The pricing screen asks the rest. **Pre-filled suggestions** (to confirm each): minor units from the ISO 4217 table for the chosen currency; rounding `half_up`; price freshness 15 min and stock freshness 5 min (the demo seed values). The policy only counts as set when every field is saved by the manager | We must not invent commercial rules; pre-fills are visible and editable |
| Q15 | Country and time zone | Country (ISO 3166-1 alpha-2) required at creation; time zone (IANA name) pre-filled from the browser and required | Needed for PDFs and later for dates in messages |
| Q16 | PDF details | Legal name and address required for Go live; tax ID and its label (e.g. "GSTIN", "VAT no.") optional; logo optional (PNG/JPEG ≤ 1 MB, shown ≤ 60 mm wide). Contact email/phone on the PDF: not in phase 2 | What appears on customer documents |
| Q17 | API keys from the portal | Only `intake_service` keys can be created in the portal; `scripts/create_api_key.py` is removed (AGENTS.md: remove replaced code); key name 1-100 characters | Today the script can create keys for any role |
| Q18 | Email normalisation | Trim, Unicode NFKC, lower-case the **whole** address, reject anything without exactly one `@`, ≤ 254 characters, ASCII local part; internationalised domains stored in punycode. No Gmail dot/plus folding | Two spellings of one address must not become two accounts |
| Q19 | Sender address and link base | `SALES_MAIL_FROM` and `SALES_PUBLIC_PORTAL_URL` settings; dev: `no-reply@localhost.example` and `http://localhost:8000/portal/` | Real values need the domain (D24) |
| Q20 | Retention setting (D17) | Add it with phase 4 (conversations and files), where it is enforced; not in phase 2 | A setting with no effect would mislead managers |
| Q21 | Businesses per person | No total limit; creation throttled (Q4) | D3 says anyone, no restrictions |
| Q22 | Leaving a business | A member may remove themselves (same last-administrator rule) | Otherwise people stay forever |
| Q23 | Profile | Change display name and password only; email change later | Email change needs re-verification flows |
| Q24 | Demo people | Seeded demo people keep using the dev login; they get no password | No shared demo passwords in code |
| Q25 | Common-password list | Bundle a small offline list (top 10 000, MIT-licensed source) in the adapter; no online breach API | Online checks send password hashes prefixes to a third party |
| Q26 | How member access changes are written | Through narrow SECURITY DEFINER functions (`accept_invitation`, `change_member_roles`, `remove_member`), so the runtime role still cannot insert memberships directly (today's test `test_runtime_role_cannot_change_access_or_policy`). Pricing settings: a column UPDATE grant under tenant RLS (the second statement of that test then becomes allowed for the own tenant only) | Changes an accepted least-privilege property (ADR 0002) |
| Q27 | API pull delivery while `setting_up` | Allowed: the API client is the business's own system and the "test" step needs it | See Q11 |

## 4. Current state (code review with file:line)
### Backend
| Finding | Where | Consequence for phase 2 |
|---|---|---|
| Roles are `administrator`, `sales_operator`, `approver`, `viewer` plus `intake_service` for keys | `domain/tenancy.py:16-22` | Invitations may assign only the four people roles |
| No `members:manage`; administrators have `tenant:manage` but no approve | `domain/tenancy.py:25-33`, `:37-68` | Add `MANAGE_MEMBERS` to administrator; creator also gets `approver` |
| `TenantContext` holds a plain `subject` string | `domain/tenancy.py:83-100` | Keep it; users map to subjects (§7.1) |
| Resolver re-reads stored roles on every request; non-member = same error as unknown tenant | `application/tenancy.py:120-124`, `api/auth.py:96-110` | Removing a member takes effect on their next request; no session change needed |
| Sessions start from any `VerifiedIdentity`; token SHA-256 stored; CSRF derived from token | `application/sessions.py:27-28`, `:53-57`, `:59-65` | Password login only needs to produce a `VerifiedIdentity` |
| Sessions can only be revoked by their own token | `application/sessions.py:71-73`, `ports/identity.py:111-124`, `persistence/sessions.py:164-170` | Password reset/change needs "revoke all sessions of this person" (new port method + RLS policy) |
| `VerifiedIdentity.provider` max 32 characters | `ports/identity.py:92-99` | `"password"` fits |
| Dev login admits only subjects that already have a membership | `adapters/identity/dev.py:143-147` | A person with zero businesses can never use dev login; fine for demos |
| Auth config only knows `dev` / `disabled`; session lists tenants with roles and permissions | `api/auth.py:45-47`, `:55-67`, `:137-158` | Extend config (password + dev flags) and add `user` and tenant `status` to the session |
| Cookie flags `HttpOnly; SameSite=Strict; Path=/api`, Secure in production | `api/auth.py:113-122` | Reused by password login |
| CSRF check only for signed-in non-GET requests | `api/auth.py:77-94` | Unauthenticated POSTs (sign-up, login, reset) need the JSON-only content-type rule instead (§11) |
| Composition root wires dev login from `DEMO_MEMBERSHIPS`; ERP is the in-memory demo | `api/app.py:103-118`, `:92` | New businesses have no products until phase 3 (and phase 1's per-tenant source resolution) |
| Portal router error mapping pattern (`guarded`) | `api/portal.py:410-441` | Reuse the same pattern in `api/business.py` |
| Tenant transaction sets `app.tenant_id` transaction-locally | `persistence/tenancy.py:20-32` | All new tenant-scoped queries use it |
| Tenants list by `app.subject` through narrow policies | `persistence/tenancy.py:48-65`, migration `0005_approvals.py:274-283` | Same mechanism lists a user's businesses; add `status` |
| No pricing policy row → `PricingPolicyNotConfigured` | `persistence/tenancy.py:72-98`, `ports/tenancy.py:11-12` | Reuse for "set later" |
| Delivery **inner-joins** the pricing policy to get `quote_validity_days` | `persistence/documents.py:265-306` | A business must always have a policy row, even before pricing is set (§7.4) |
| API keys: SHA-256 of a 256-bit secret, created/revoked with **owner** credentials only | `application/api_keys.py:218-232`, `persistence/api_keys.py:214-239`, `scripts/create_api_key.py:26` | Portal creation needs an INSERT grant for the runtime role (tenant-scoped) |
| Narrow GUC lookup policy pattern (`api_key_lookup`) | `migrations/versions/0003_intake.py:156-161` | Same pattern for user/email/token lookups |
| Tenants: forced RLS, runtime SELECT only | `0001_tenants.py:22-29` | Status/name updates need column grants; creation goes through `create_business` |
| Memberships and policies: runtime SELECT only | `0002_memberships_policies.py:44-55` | Q26 |
| `reject_change()` append-only trigger | `0005_approvals.py:247-256` | Reused for `admin_events` |
| Session RLS: lookup/revoke only by presented token hash | `0005_approvals.py:258-273`, `:286` | Add per-subject revoke policy |
| Worker may read tenants, memberships, policies | `0005_approvals.py:296` | Worker also reads `tenant_profiles` for PDFs |
| `quote_validity_days` default 7, ≥ 1 | `0007_acceptance.py:46-54`, `persistence/models.py:99` | Shown on the pricing screen |
| Models: `Tenant` has only id/name/created_at; policy columns all NOT NULL | `persistence/models.py:37-42`, `:64-100` | New columns; "set later" makes commercial columns nullable |
| `UserSession` is not tenant-owned | `persistence/models.py:271-286` | Users follow the same "global, narrow lookup" pattern |
| Readiness requires exactly one head | `persistence/database.py:5`, `:260-273` | Set `EXPECTED_REVISION = "0010_accounts"` |
| Jobs need a tenant: `jobs.tenant_id NOT NULL`, `ClaimedJob.tenant_id: UUID`, `queue_job(tenant_id: UUID)` | `0003_intake.py:129-151`, `ports/jobs.py` (`ClaimedJob`), `persistence/jobs.py:39-63` | Sign-up and reset emails have no tenant: contract change (§15) |
| Job kinds | `domain/jobs.py:19-27` | Add `SEND_ACCOUNT_EMAIL` |
| PDF shows only `tenant_name` (title and author) | `domain/documents.py:117-130`, `adapters/documents/pdf.py:69`, `:134`, `:179` | Add seller details from `tenant_profiles` |
| PDF text must fit cp1252, otherwise the render fails | `adapters/documents/pdf.py:32`, `:36-44` | Validate profile text with the same rule when saved, so a profile can never break all PDFs |
| Approved PDF is rendered once; first stored document wins | `application/delivery.py:181-221` | Profile is captured at first render; later edits do not change issued PDFs |
| Seed: demo subjects are emails, dev/test only, owner inserts with tenant context | `persistence/seed.py:26-38`, `:49-84` | Seed also creates `users` rows and marks demo tenants `active` |
| Settings: `auth_provider` `dev`/`disabled`; production refuses dev | `config.py:36`, `:45-57` | New settings (§8) |
| Roles in database: `init.sql` creates 3 roles NOBYPASSRLS; runtime roles have no CREATE on `public` | `infra/postgres/init.sql:12-26` | Limits search-path tricks against definer functions |
| Upload precedent: base64 inside JSON, no multipart | `api/enquiries.py:72`, `:212-214` | Logo upload uses the same approach (no `python-multipart`) |

### Tests
| Finding | Where | Consequence |
|---|---|---|
| In-memory fakes implement ports for unit tests | `tests/unit/conftest.py:1-8` | Add fakes for every new port |
| Integration tests truncate a fixed table list; tenants are never truncated | `tests/integration/conftest.py:27-33`, `:68-71` | Self-created tenants need cleanup (§10.2) |
| API keys in tests are created with owner credentials | `tests/integration/conftest.py:101-110` | Keep the persistence helper for tests, or move it to `tests/support` |
| Test asserts runtime cannot insert memberships or update policies | `tests/integration/persistence/test_tenancy.py:54-69` | Q26 decides how it changes |
| Third-party allow-list per layer | `tests/unit/test_architecture.py:21-30` | Add `argon2` and `PIL` to adapters |
| CI cycle: upgrade, check, downgrade to base, upgrade, retest | `scripts/check_database.py:58-67` | Covers 0010 downgrade automatically |

### Portal
| Finding | Where | Consequence |
|---|---|---|
| `AuthConfig.provider` is `"dev" \| "disabled"`; `Permission` union lacks new permissions | `portal/src/features/auth/model/types.ts:16-26` | Extend |
| Repository has `devLogin` only and keeps the CSRF token in sync | `features/auth/api/authRepository.ts:7-38` | Password login must call `setCsrfToken` the same way |
| Login error says "not a member of any business" | `features/auth/hooks/useLoginForm.ts:22-24` | Wrong for password login; message must be generic |
| Login page navigates to `/quotations` and mixes dev text | `features/auth/ui/LoginPage.tsx:11-13`, `:27-30` | Move to `account`; home decides where to go |
| `auth/ui` has no `pages/components` split | `features/auth/ui/*` | Small inconsistency; new features use `ui/pages` + `ui/components` |
| Route table: public auth routes + one layout | `app/router.tsx:16-30` | Add public account routes, signed-in-without-business routes, tenant routes |
| "You are not a member of any business." dead end | `app/layout/AppLayout.tsx:20-24` | Replace with a redirect to "Create your business" |
| Navigation is fixed | `app/layout/TopBar.tsx:6-10` | Add Setup and Settings links (permission-aware) |
| Current tenant: stored choice or first membership | `features/tenant/hooks/useCurrentTenant.ts:14-26` | Add `status`; after creating/accepting, select the new tenant |
| Switcher always navigates to `/quotations` | `features/tenant/ui/TenantSwitcher.tsx:15` | Navigate to home (`/`) so setting-up businesses open Setup |
| Architecture test enforces layers, index-only imports, fetch only in `shared/api` | `portal/tests/architecture.test.ts:14-23`, `:96-109` | New features follow it; add one rule (§10.4) |

## 5. Design overview
```
Browser (portal)                         API (quotation_app role)                         Worker (quotation_worker)
sign-up ── POST /auth/signup ──► SignupService: normalise email, check password,
                                 hash (Argon2id), insert user + credential +
                                 email_verifications row + send_account_email job ──► AccountEmailHandler: mint token,
                                                                                     store SHA-256, Mailer.send (console)
verify link (#/verify-email?token=…) ─ POST /auth/verify-email ─► consume token, mark verified
login ── POST /auth/login ─► LoginService ─► VerifiedIdentity(subject, "password") ─► SessionService.start (unchanged)
create business ── POST /businesses ─► BusinessService ─► SELECT create_business(...)  (SECURITY DEFINER, owner)
settings / members / invitations / API keys / setup / go-live ── /tenants/{t}/... (TenantContext, permissions, audit)
```
Key ideas, in simple words:
- **A user is a person; a subject is the stable name sessions and memberships use.** New users get the subject `user:<uuid>`. Demo people keep their email as subject. This keeps every existing `subject` column (approvals, versions, bindings) working.
- **Tokens in emails are minted by the worker at send time**, like approval action references today (`0005_approvals.py:303-306`). The database only ever stores their SHA-256. Job payloads hold references only.
- **Access changes go through narrow database functions** (Q26), so a bug in API code cannot add a membership for an arbitrary person.
- **Setup checklist reads real state** (rows and counts), never a stored "done" flag.

## 6. Business status and Go live (Q11, Q12, Q13, Q27: all to confirm)
| Action | `setting_up` | `active` | `suspended` |
|---|---|---|---|
| Sign in, settings, members, invitations, API keys | allowed | allowed | read-only (no changes) |
| Enquiry intake (API keys; later connections) | allowed (needed for the test step) | allowed | refused (generic 403) |
| Reading, matching, drafting, approval | allowed | allowed | jobs paused: handlers raise a retryable "business suspended" error until the job dies |
| API pull delivery (`deliver_quotation` → `available`) | allowed (Q27) | allowed | refused (`failed`, reason "business suspended") |
| Messages sent to customers through providers (phase 1 outbox, purpose `customer_messages`) | **refused**: recorded `failed`, reason `business_not_live`, never sent | allowed | refused |
| Customer accept/decline through the API | allowed (test) | allowed | refused |
| Go live button | shown when checklist complete | — | — |

One domain function decides this, so no gate is scattered: `domain/tenancy.py` `may_contact_customers(status) -> bool`, `accepts_intake(status) -> bool`, `allows_changes(status) -> bool`. Gate points: `EnquiryIntakeService.submit`, phase 1 `OutboundService.enqueue`/`SendMessageHandler`, `QuotationDeliveryHandler`, `QuotationResponseService`, `BusinessService`/`MemberService` writes. The exact phase 1 class names follow the phase 1 plan.

### Setup checklist (computed, Q11 to confirm which are required)
| Key | Done when (SQL, tenant context) | Required for Go live (proposal) |
|---|---|---|
| `business_details` | `tenants.name`, `country`, `time_zone` not null | yes |
| `pdf_details` | `tenant_profiles.legal_name` and `address` not null | yes (Q16) |
| `pricing` | policy row complete (all commercial columns not null) | yes |
| `approver` | a membership with role `approver` exists | yes (otherwise nothing can be approved) |
| `product_source` | phase 1 catalogue source resolves for this tenant / phase 3 product count > 0 | **no in phase 2** (shown "coming in a later update"); becomes required in phase 3 (to confirm) |
| `customer_channel` | an active connection with purpose `customer_messages` exists (phase 1), which includes the `api` connection behind an active intake API key | yes |
| `test_enquiry` | for each active customer connection, at least one `inbox_events` row | yes |

Go live = `POST /tenants/{t}/go-live`: under a row lock on the tenant, recompute the checklist; if anything required is missing answer 409 with the missing keys; else set `active`, `went_live_at`, write an `admin_events` row. Already active → 200 (idempotent).

## 7. Work items (in order)
Each item lists files, signatures and rules. Item W0 is a gate.

### W0. Ask the open questions
Ask Q1-Q27 with the clarify-before-building skill; record answers in [07](../07-decisions-needed.md), this plan and the handoff. Re-plan anything an answer changes.

### W1. Domain (standard library only)
**`domain/tenancy.py`** (changes)
- `Permission.MANAGE_MEMBERS = "members:manage"`; add to `Role.ADMINISTRATOR` in `ROLE_PERMISSIONS`. (`integrations:manage` comes from phase 1.)
- `class TenantStatus(StrEnum): SETTING_UP = "setting_up"; ACTIVE = "active"; SUSPENDED = "suspended"`.
- `PEOPLE_ROLES: frozenset[Role]` = the four people roles (never `intake_service`).
- `may_contact_customers(status: TenantStatus) -> bool`, `accepts_intake(status) -> bool`, `allows_changes(status) -> bool` (table in §6).

**`domain/accounts.py`** (new; contract name)
```python
class InvalidEmail(ValueError): ...
def normalize_email(raw: str) -> str                      # Q18 rules; raises InvalidEmail
def user_subject(user_id: UUID) -> str                     # "user:<uuid>"

class PasswordProblem(StrEnum): TOO_SHORT, TOO_LONG, TOO_COMMON, CONTAINS_EMAIL
@dataclass(frozen=True)
class PasswordPolicy: min_length: int; max_length: int     # values from Q1
def password_problems(password: str, *, email: str, policy: PasswordPolicy,
                      common: frozenset[str]) -> list[PasswordProblem]

@dataclass(frozen=True)
class User: user_id: UUID; subject: str; email: str | None; display_name: str | None;
            email_verified: bool; disabled: bool

@dataclass(frozen=True)
class LockoutPolicy: max_failures: int; lock_for: timedelta # Q3
def after_failed_login(failures: int, now: datetime, policy: LockoutPolicy) -> tuple[int, datetime | None]

class AccountEmailPurpose(StrEnum):
    VERIFY_EMAIL, PASSWORD_RESET, INVITATION, ACCOUNT_EXISTS, PASSWORD_CHANGED, LOCKED_OUT
TOKEN_LIFETIME: Mapping[AccountEmailPurpose, timedelta]     # Q6 (only for token purposes)

class InvitationStatus(StrEnum): PENDING, ACCEPTED, REVOKED, EXPIRED
@dataclass(frozen=True)
class Invitation: invitation_id: UUID; tenant_id: UUID; email: str; roles: frozenset[Role];
                  invited_by: str; created_at: datetime; expires_at: datetime | None;
                  accepted_at: datetime | None; revoked_at: datetime | None
    def status(self, now: datetime) -> InvitationStatus
def validate_people_roles(roles: Iterable[Role]) -> frozenset[Role]   # non-empty, subset of PEOPLE_ROLES

class LastAdministrator(Exception): ...
def check_administrator_remains(roles_by_subject: Mapping[str, frozenset[Role]],
                                subject: str, new_roles: frozenset[Role]) -> None  # new_roles empty = removal

class SetupItemKey(StrEnum): BUSINESS_DETAILS, PDF_DETAILS, PRICING, APPROVER,
                             PRODUCT_SOURCE, CUSTOMER_CHANNEL, TEST_ENQUIRY
@dataclass(frozen=True)
class SetupItem: key: SetupItemKey; done: bool; required: bool
def missing_for_go_live(items: Sequence[SetupItem]) -> list[SetupItemKey]
```
The database functions enforce the last-administrator rule too; the domain function gives the same answer earlier with a clear message.

**`domain/jobs.py`**: `JobKind.SEND_ACCOUNT_EMAIL = "send_account_email"`.

**`domain/documents.py`**: `SellerDetails(legal_name: str, address_lines: tuple[str, ...], tax_id_label: str | None, tax_id: str | None, logo_png: bytes | None)`; `DocumentSource.seller: SellerDetails | None = None`; move the "printable in the PDF font" rule here as `printable_text(value, field) -> str` (raises `UnrenderableContent`) so the renderer and the profile validation share it.

**`domain/messages.py`** (phase 1): add `TemplateKey` values `account_verify_email`, `account_password_reset`, `account_invitation`, `account_exists`, `account_password_changed`, `account_locked_out`. Plain text only. `render_template` removes CR/LF from anything that goes into a subject line (header injection).

### W2. Ports
**`ports/accounts.py`** (new; contract names `UserStore`, `PasswordHasher`, `InvitationStore`, `Mailer`, plus additive ones)
```python
class PasswordHasher(Protocol):
    def hash(self, password: str) -> str
    def verify(self, password_hash: str, password: str) -> bool      # constant-time, never raises on mismatch
    def needs_rehash(self, password_hash: str) -> bool
    @property
    def dummy_hash(self) -> str                                         # used when the email is unknown

@dataclass(frozen=True)
class LoginRecord: user: User; password_hash: str | None; failed_attempts: int; locked_until: datetime | None

class UserStore(Protocol):
    def sign_up(self, user_id: UUID, email: str, display_name: str | None, password_hash: str) -> bool
        # one transaction: users + user_credentials + email_verifications + job; False if the email exists
        # (then queues an ACCOUNT_EXISTS notice instead, subject to rate limit)
    def find_for_login(self, email: str) -> LoginRecord | None
    def record_failed_login(self, user_id: UUID, now: datetime, policy: LockoutPolicy) -> bool  # True if now locked
    def record_successful_login(self, user_id: UUID, new_hash: str | None) -> None
    def by_subject(self, subject: str) -> User | None
    def update_profile(self, subject: str, display_name: str) -> None
    def verify_email(self, token_sha256: str, now: datetime) -> bool   # consume + mark verified, atomic
    def request_verification(self, subject: str) -> None               # revoke open rows, new row + job
    def request_password_reset(self, email: str) -> None               # no-op for unknown emails
    def reset_password(self, token_sha256: str, password_hash: str, now: datetime) -> str | None
        # consumes token, sets hash, clears lockout, returns subject (for session revoke) or None
    def change_password(self, subject: str, password_hash: str) -> None

class InvitationStore(Protocol):
    def invite(self, context: TenantContext, email: str, roles: frozenset[Role]) -> UUID   # + job + audit
    def list(self, context: TenantContext) -> list[Invitation]
    def resend(self, context: TenantContext, invitation_id: UUID) -> bool
    def revoke(self, context: TenantContext, invitation_id: UUID) -> bool
    def preview(self, token_sha256: str, now: datetime) -> InvitationPreview | None
    def accept(self, token_sha256: str, user_id: UUID) -> UUID          # calls accept_invitation(); raises InvitationNotUsable

class MemberStore(Protocol):          # additive
    def list(self, context: TenantContext) -> list[Member]
    def change_roles(self, context: TenantContext, user_id: UUID, roles: frozenset[Role]) -> None   # change_member_roles()
    def remove(self, context: TenantContext, user_id: UUID) -> None                                  # remove_member()

class BusinessStore(Protocol):        # additive
    def create(self, user_id: UUID, business: NewBusiness) -> UUID                                   # create_business()
    def get(self, context: TenantContext) -> BusinessSettings
    def update_details(self, context: TenantContext, details: BusinessDetails) -> None
    def update_profile(self, context: TenantContext, profile: ProfileUpdate, expected_version: int) -> int
    def set_logo(self, context: TenantContext, storage_key: str | None, sha256: str | None) -> None
    def update_pricing(self, context: TenantContext, policy: PricingSettings, expected_version: int) -> int
    def setup_state(self, context: TenantContext) -> list[SetupItem]
    def go_live(self, context: TenantContext) -> list[SetupItemKey]                                  # [] = done
    def status(self, tenant_id: UUID) -> TenantStatus

class ApiKeyAdminStore(Protocol):     # additive
    def create(self, context: TenantContext, key: NewApiKey, name: str, role: Role) -> None
    def list(self, context: TenantContext) -> list[ApiKeySummary]
    def revoke(self, context: TenantContext, key_id: UUID) -> bool

class AccountEmailStore(Protocol):    # additive, worker side
    def prepare(self, purpose: AccountEmailPurpose, ref_id: UUID, tenant_id: UUID | None,
                token_sha256: str | None, expires_at: datetime | None) -> AccountEmailWork | None
        # compare-and-set pending -> sending; None if consumed, revoked or already handed over
    def finish(self, purpose, ref_id, tenant_id, status: Literal["sent", "failed", "unknown"]) -> None

@dataclass(frozen=True)
class AccountEmail: to: str; template: TemplateKey; values: Mapping[str, str]; idempotency_key: str
class MailRejected(Exception): ...
class MailUncertain(Exception): ...
class Mailer(Protocol):
    def send(self, message: RenderedAccountEmail) -> None   # raises MailRejected / MailUncertain

@dataclass(frozen=True)
class RateDecision: allowed: bool; retry_after: timedelta
class RateLimiter(Protocol):          # additive
    def hit(self, key: str, limit: int, window: timedelta) -> RateDecision
```
**`ports/identity.py`**: `SessionStore.revoke_all_for(subject: str, keep_token_sha256: str | None) -> int`.
**`ports/tenancy.py`**: `TenantMembership` gains `status: TenantStatus`.
**`ports/jobs.py`**: `ClaimedJob.tenant_id: UUID | None` plus `def require_tenant(self) -> UUID` (raises `PermanentJobError`); all existing handlers call `require_tenant()` (§15).
**`ports/documents.py`**: `LogoNormalizer` protocol: `normalize(data: bytes, declared_type: str) -> bytes` (returns PNG; raises `InvalidLogo`).

### W3. Migration `0010_accounts` (see §9 for downgrade and data)
All new tables use the naming convention in `persistence/models.py:27-34`. `_TENANT_FILTER` is the existing expression (`0002_memberships_policies.py:44`). GUCs used (all transaction-local, set with `set_config(..., true)`):

| GUC | Set by | Meaning |
|---|---|---|
| `app.tenant_id` | existing | current business |
| `app.subject` | existing | signed-in person |
| `app.user_email` | sign-up, login, forgot password | the normalised email being looked up |
| `app.user_id` | login, verify, reset, change password | the user whose credential row may be read/changed |
| `app.account_token_sha256` | verify, reset, invitation preview/accept | hash of a presented email token |
| `app.account_ref_id` | worker | the token row being emailed |

#### 3.1 `users` (global, not tenant-owned)
| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `subject` | varchar(200) NOT NULL UNIQUE | `user:<uuid>` for sign-ups; legacy subjects kept |
| `email` | varchar(254) NULL | as typed (display) |
| `email_normalized` | varchar(254) NULL, partial UNIQUE where not null | CHECK `email_normalized = lower(email_normalized)`; NULL only for legacy non-email subjects |
| `display_name` | varchar(100) NULL | |
| `email_verified_at` | timestamptz NULL | |
| `disabled_at` | timestamptz NULL | operator only |
| `created_at`, `updated_at` | timestamptz NOT NULL default now() | |

RLS: `ENABLE` + `FORCE`. Policies:
- `users_self` FOR SELECT, UPDATE TO quotation_app USING `subject = NULLIF(current_setting('app.subject', true), '')`.
- `users_email_lookup` FOR SELECT TO quotation_app USING `email_normalized = NULLIF(current_setting('app.user_email', true), '')`.
- `users_signup` FOR INSERT TO quotation_app WITH CHECK `email_normalized = NULLIF(current_setting('app.user_email', true), '') AND subject = 'user:' || id::text`.
- `users_by_id` FOR SELECT, UPDATE TO quotation_app USING `id = NULLIF(current_setting('app.user_id', true), '')::uuid` (verify/reset).
- `users_tenant_directory` FOR SELECT TO quotation_app USING `EXISTS (SELECT 1 FROM memberships m WHERE m.subject = users.subject AND m.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)` (member list; memberships RLS also applies inside).
- `users_worker` FOR SELECT TO quotation_worker USING `id = NULLIF(current_setting('app.user_id', true), '')::uuid`.
- `users_owner` FOR ALL TO quotation_owner USING (true) WITH CHECK (true) (seed and definer functions; the functions' own WHERE clauses do the checking).

Grants: app `SELECT, INSERT (id, subject, email, email_normalized, display_name), UPDATE (display_name, email_verified_at, updated_at)`; worker `SELECT (id, email, display_name)`.

#### 3.2 `user_credentials`
`user_id` uuid PK FK `users.id`; `password_hash` varchar(255) NOT NULL (Argon2 encoded string; CHECK `password_hash LIKE '$argon2id$%'`); `password_changed_at` timestamptz NOT NULL; `failed_attempts` integer NOT NULL default 0 CHECK ≥ 0; `locked_until` timestamptz NULL.
RLS forced; policy `credentials_by_user` FOR ALL TO quotation_app USING/WITH CHECK `user_id = app.user_id`. Grants: app `SELECT, INSERT, UPDATE (password_hash, password_changed_at, failed_attempts, locked_until)`. **Worker: nothing.**

#### 3.3 `email_verifications` and `password_resets` (same shape)
`id` uuid PK; `user_id` FK users; `email_normalized` varchar(254) NOT NULL (the address being proven); `token_sha256` char(64) NULL UNIQUE; `email_status` varchar(16) NOT NULL default `pending` CHECK in (`pending`,`sending`,`sent`,`failed`,`unknown`); `expires_at` timestamptz NULL (set when minted); `sent_at`, `consumed_at`, `revoked_at` NULL; `created_at`.
Partial unique index: one open row per user (`consumed_at IS NULL AND revoked_at IS NULL`).
Policies: `own_rows` FOR SELECT, INSERT, UPDATE TO quotation_app USING/WITH CHECK `user_id = app.user_id`; `token_lookup` FOR SELECT, UPDATE TO quotation_app USING `token_sha256 = app.account_token_sha256`; `worker_send` FOR SELECT, UPDATE TO quotation_worker USING `id = app.account_ref_id`.
Grants: app `SELECT, INSERT (id, user_id, email_normalized), UPDATE (consumed_at, revoked_at)`; worker `SELECT, UPDATE (token_sha256, expires_at, email_status, sent_at)`.

#### 3.4 `invitations` (tenant-owned)
`id`, `tenant_id` (FK, composite unique `(tenant_id, id)`); `email` varchar(254); `email_normalized` varchar(254) NOT NULL; `roles` text[] NOT NULL CHECK `cardinality(roles) >= 1 AND roles <@ ARRAY['administrator','sales_operator','approver','viewer']`; `invited_by` varchar(200) NOT NULL; `token_sha256` char(64) NULL UNIQUE; `email_status` (as 3.3); `expires_at`, `sent_at`, `accepted_at`, `accepted_by` (varchar 200), `revoked_at`, `revoked_by` NULL; `created_at`.
Partial unique index `(tenant_id, email_normalized) WHERE accepted_at IS NULL AND revoked_at IS NULL`.
Policies: `tenant_isolation` (standard); `invitation_token_lookup` FOR SELECT TO quotation_app USING `token_sha256 = app.account_token_sha256` (preview only; accepting goes through `accept_invitation`).
Grants: app `SELECT, INSERT (id, tenant_id, email, email_normalized, roles, invited_by), UPDATE (revoked_at, revoked_by, email_status)` (resend resets `email_status` to pending); worker `SELECT, UPDATE (token_sha256, expires_at, email_status, sent_at)`.

#### 3.5 `tenants` (new columns)
`status` varchar(16) NOT NULL default `'setting_up'` CHECK in (`setting_up`,`active`,`suspended`); `country` char(2) NULL CHECK `country ~ '^[A-Z]{2}$'`; `time_zone` varchar(64) NULL; `created_by` varchar(200) NULL (legacy rows); `went_live_at` timestamptz NULL; `updated_at` timestamptz NOT NULL default now().
Trigger `tenants_status_guard` BEFORE UPDATE: if `NEW.status <> OLD.status` and not (`OLD.status = 'setting_up' AND NEW.status = 'active'`) and `current_user <> 'quotation_owner'` → raise. (Suspension only with owner credentials, Q13.)
Grants: app `UPDATE (name, country, time_zone, status, went_live_at, updated_at)` (row access still limited by `tenant_isolation`). Runtime keeps **no INSERT** on tenants.

#### 3.6 `tenant_profiles` (tenant-owned, one row per tenant)
`tenant_id` PK FK; `legal_name` varchar(200) NULL; `address` varchar(500) NULL (≤ 4 lines, validated in the service); `tax_id_label` varchar(32) NULL; `tax_id` varchar(64) NULL; `logo_storage_key` varchar(300) NULL; `logo_sha256` char(64) NULL; CHECK both logo columns null or both set; `version` integer NOT NULL default 1; `updated_by` varchar(200) NULL; `updated_at`.
RLS `tenant_isolation`. Grants: app `SELECT, UPDATE (legal_name, address, tax_id_label, tax_id, logo_storage_key, logo_sha256, version, updated_by, updated_at)`; worker `SELECT`.

#### 3.7 `tenant_pricing_policies` (changes, "set later")
Drop NOT NULL on `currency`, `minor_units`, `rounding_mode`, `tax_rate`, `max_price_age_seconds`, `max_stock_age_seconds`. Add `ck_tenant_pricing_policies_complete`: either all six are NULL or all six are NOT NULL. Existing checks stay (they pass on NULL). Add `updated_by` varchar(200) NULL.
Grants: app `UPDATE (version, currency, minor_units, rounding_mode, tax_rate, max_price_age_seconds, max_stock_age_seconds, quote_validity_days, updated_by, updated_at)` (Q26).

#### 3.8 `memberships` (changes)
FK `fk_memberships_subject_users` (`subject` → `users.subject`). Runtime role still has **no INSERT/DELETE** (Q26): all writes go through the definer functions in 3.11.

#### 3.9 `admin_events` (tenant-owned, append-only audit)
`id`, `tenant_id`, `actor_subject` varchar(200) NOT NULL, `action` varchar(48) NOT NULL CHECK in (`business_created`, `went_live`, `status_changed`, `details_changed`, `profile_changed`, `logo_changed`, `pricing_changed`, `member_invited`, `invitation_resent`, `invitation_revoked`, `invitation_accepted`, `member_roles_changed`, `member_removed`, `api_key_created`, `api_key_revoked`), `target` varchar(200) NULL (user id, invitation id, key id), `details` jsonb NOT NULL default `'{}'` (old/new values; never tokens, hashes or passwords), `created_at`.
RLS `tenant_isolation`; trigger `admin_events_immutable` using `reject_change()`. Grants: app `SELECT, INSERT`; worker none.

#### 3.10 Other changes
- `api_keys`: add `created_by`, `revoked_by` varchar(200) NULL. Grants: app `INSERT (id, tenant_id, name, role, secret_sha256, created_by)`, `UPDATE (revoked_at, revoked_by)` (tenant_isolation limits rows).
- `jobs`: `tenant_id` becomes NULL-able; CHECK `ck_jobs_tenant`: `tenant_id IS NOT NULL OR kind = 'send_account_email'`; policy `account_email_jobs` FOR INSERT TO quotation_app WITH CHECK `tenant_id IS NULL AND kind = 'send_account_email'`. Worker's existing `worker_queue` sees them.
- `user_sessions`: policies `session_list_for_subject` FOR SELECT and `session_revoke_for_subject` FOR UPDATE, TO quotation_app, USING `subject = app.subject` (revoke all sessions after reset/change).

#### 3.11 Definer functions
All four: `LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public`, `OWNER TO quotation_owner`, `REVOKE ALL ... FROM PUBLIC`, `GRANT EXECUTE ... TO quotation_app` only. Because FORCE RLS applies to the owner too, each function sets `app.tenant_id` itself and **restores the caller's previous value** before returning. Every function trusts only `app.subject` for "who is calling" and checks it against stored data.

```sql
create_business(p_user_id uuid, p_name text, p_country text, p_time_zone text,
                p_currency text, p_minor_units smallint, p_rounding_mode text, p_tax_rate numeric,
                p_max_price_age_seconds integer, p_max_stock_age_seconds integer,
                p_quote_validity_days integer) RETURNS uuid
```
1. `SELECT subject FROM users WHERE id = p_user_id AND subject = app.subject AND email_verified_at IS NOT NULL AND disabled_at IS NULL`; none → `RAISE ... ERRCODE '42501'` ("not allowed").
2. Validate name (1-200 chars, trimmed), country pattern, time zone exists in `pg_timezone_names`, validity ≥ 1; pricing all-null or all-set (the table checks do the rest).
3. `v_tenant := gen_random_uuid()` (never a parameter, so it cannot target an existing business); set `app.tenant_id`.
4. Insert `tenants` (`status = 'setting_up'`, `created_by = subject`), two `memberships` (`administrator`, `approver`), the `tenant_pricing_policies` row (version 1, values or NULLs, validity), an empty `tenant_profiles` row, an `admin_events` `business_created` row.
5. Restore `app.tenant_id`; return `v_tenant`.

```sql
accept_invitation(p_token_sha256 text, p_user_id uuid) RETURNS uuid
```
Locks the invitation `FOR UPDATE` by token; requires pending (not accepted, not revoked, `expires_at > now()`); requires `users.id = p_user_id AND users.subject = app.subject AND users.email_normalized = invitation.email_normalized`; marks the user verified if not yet (the link proved control of the email); inserts one membership per role `ON CONFLICT DO NOTHING`; sets `accepted_at/by`; writes `invitation_accepted`; returns the tenant. Any failure: one generic error (`P0001`, "invitation not usable").

```sql
change_member_roles(p_tenant uuid, p_user_id uuid, p_roles text[]) RETURNS void
remove_member(p_tenant uuid, p_user_id uuid) RETURNS void
```
Both: set tenant; require `app.subject` to hold `administrator` in `p_tenant` (members:manage), except `remove_member` of oneself (Q22); lock the tenant row `FOR UPDATE` (serialises concurrent changes); compute the administrators that would remain; if none → raise `P0002` "last administrator"; delete/insert membership rows; write the audit row with old and new roles. `remove_member` also sets `revoked_at` on that person's active `channel_bindings` in the tenant (they cannot receive approval requests any more). Tenant must not be `suspended`.

#### 3.12 Readiness
`persistence/database.py:5` → `EXPECTED_REVISION = "0010_accounts"`.

### W4. Persistence
| File | Change |
|---|---|
| `persistence/models.py` | New `User`, `UserCredential`, `EmailVerification`, `PasswordReset`, `Invitation`, `TenantProfile`, `AdminEvent`; new columns on `Tenant`, `TenantPricingPolicy` (Optional), `ApiKey`, `Job.tenant_id` Optional, FK on `Membership.subject`. `alembic check` must stay clean |
| `persistence/accounts.py` (new) | `SqlUserStore`, `SqlInvitationStore`, `SqlMemberStore`, `SqlBusinessStore`, `SqlAccountEmailStore`. Helpers `_with(connection, **gucs)` set GUCs transaction-locally. Definer functions are called with `select(func.create_business(...))`. Errors `42501`/`P0001`/`P0002` are mapped to `NotAllowed`, `InvitationNotUsable`, `LastAdministrator` |
| `persistence/api_keys.py` | `SqlApiKeyAdminStore` (runtime role, tenant transaction, audit row in the same transaction). Keep `insert_api_key` for tests only, or move it to `tests/support` (Q17) |
| `persistence/sessions.py` | `revoke_all_for(subject, keep_token_sha256)` using `app.subject` |
| `persistence/tenancy.py` | `tenants_for` returns status; `SqlPricingPolicyReader.get` raises `PricingPolicyNotConfigured` when the row is incomplete |
| `persistence/jobs.py` | `queue_job(connection, tenant_id: UUID | None, ...)`; `claim` maps NULL tenant |
| `persistence/documents.py` | Approved/preview sources load `tenant_profiles` (and the logo bytes reference) |
| `persistence/seed.py` | Also inserts `users` for every demo subject (`email_verified_at = now()`, synthetic, Q24), marks demo tenants `active`, creates empty `tenant_profiles` rows. Still dev/test only and idempotent |

### W5. Adapters
| File | Content |
|---|---|
| `adapters/accounts/password.py` | `Argon2PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, max_concurrent=4)` (RFC 9106 low-memory profile, to confirm); a `threading.BoundedSemaphore` caps simultaneous hashes so a login flood cannot use more than ~256 MB; `needs_rehash` via `argon2.PasswordHasher.check_needs_rehash`; `dummy_hash` computed once at start; loads the common-password list (Q25) |
| `adapters/accounts/mail/console.py` | `ConsoleMailer(directory)`: writes each email to `storage_dir/dev-mail/<utc time>-<idempotency key>.txt` and logs only "account email written" with the file name (no address, no token). Development/test only |
| `adapters/accounts/mail/__init__.py` | `load_mailer(name, options) -> Mailer` from entry points group `sales_platform.mailers` (`console = ...:ConsoleMailer`). Phase 5 adds the SendGrid package with its own entry point, no code change here (generic integrations rule) |
| `adapters/accounts/rate_limit.py` | `InMemoryRateLimiter` (sliding window, keyed by SHA-256 of the key so no email sits in memory in clear, bounded size) |
| `adapters/documents/logo.py` | `PillowLogoNormalizer`: accept PNG/JPEG only by magic bytes, ≤ 1 MB, `Image.MAX_IMAGE_PIXELS` lowered (e.g. 4 MP), verify, downscale to ≤ 600×240 px, re-encode PNG without metadata |
| `adapters/documents/pdf.py` | Renderer `quotation-pdf-v2`: header shows logo (if any), legal name (fallback: business name), address lines, tax ID label + value. Still data-only (no URLs, no HTML). Escapes all text |

Dependencies: `argon2-cffi` (newest release at least 14 days old, e.g. 25.1.0; check at implementation). Pillow is already installed through ReportLab (`pyproject.toml` constraint `pillow>=12.3,<13`); it is now imported directly, so add it as a direct dependency with the same range. Architecture allow-list: `argon2` and `PIL` in `adapters` only.

### W6. Application services (`application/accounts.py`, contract names + additive)
| Service | Methods | Rules |
|---|---|---|
| `SignupService` | `sign_up(email, password, display_name, client: ClientInfo) -> None`; `verify_email(token) -> bool`; `resend_verification(subject, client)` | Rate limit (IP, email) → normalise → password rules → **always** hash (also for existing emails, equal time) → `UserStore.sign_up`. Same answer for new and existing email (Q7). No session is started |
| `LoginService` | `login(email, password, client) -> VerifiedIdentity`; `forgot_password(email, client)`; `reset_password(token, new_password) -> None`; `change_password(subject, current, new, current_token)` | Rate limits; unknown email → verify against `dummy_hash`, then fail; locked → still verify, then fail with the same `IdentityRejected("Sign-in failed")`; success resets counters and re-hashes if needed; returns `VerifiedIdentity(subject, provider="password")`; reset revokes **all** sessions of the person and queues `PASSWORD_CHANGED`; change revokes all **other** sessions |
| `BusinessService` | `create(person, NewBusiness) -> UUID`, `settings(context)`, `update_details`, `update_profile`, `upload_logo`, `remove_logo`, `update_pricing`, `checklist(context)`, `go_live(context)` | `tenant:manage` for changes; `allows_changes(status)`; profile text checked with `printable_text`; pricing values validated with `PricingPolicy` rules (`domain/pricing.py:39-49`) before saving; optimistic `expected_version` → 409 on conflict; audit rows |
| `InvitationService` | `invite`, `list`, `resend`, `revoke`, `preview(token)`, `accept(person, token) -> UUID` | `members:manage`; roles through `validate_people_roles`; per-business rate limit; inviting an existing member → 409; accept requires a signed-in person (email may be unverified, the function verifies it) |
| `MemberService` | `list`, `change_roles`, `remove` | `members:manage` (self-removal allowed); domain check first, database function decides |
| `ApiKeyAdminService` | `create(context, name, role) -> NewApiKey`, `list`, `revoke` | `integrations:manage` (02 and contract §6); role must be `intake_service` (Q17); token returned once; if phase 1 ties each key to an `api` connection, creation goes through phase 1's `ConnectionService` in the same transaction |
| `AccountEmailHandler` | `__call__(job: ClaimedJob)` | Payload `{purpose, ref_id}` only. For token purposes: mint `secrets.token_urlsafe(32)`, compare-and-set `pending → sending` storing the SHA-256 and `expires_at`, then `Mailer.send`; `MailRejected` → `failed`; `MailUncertain` → `unknown` (never retried; the person can press "resend"); success → `sent`. Rows already consumed/revoked → job completes without sending. Links: `{public_portal_url}#/verify-email?token=…`, `#/reset-password?token=…`, `#/invitations/accept?token=…` (token in the URL fragment, so it never reaches server logs or `Referer`) |

`ClientInfo(ip: str)` comes from the request; with `SALES_TRUSTED_PROXY_COUNT > 0` the IP is read from `X-Forwarded-For` (rightmost trusted hop).

### W7. API
`api/auth.py` keeps `PortalAuth`, `/auth/config`, `/auth/session`, `/auth/logout`, `/auth/dev-login`. New routers `api/accounts.py` and `api/business.py` (contract names). All bodies `extra="forbid"`; all responses that carry a token or session use `Cache-Control: no-store`.

| Method and path | Auth | Request | Response |
|---|---|---|---|
| `GET /api/v1/auth/config` | public | — | `{password_login: true, dev_login: bool, dev_subjects: [..]}` |
| `POST /api/v1/auth/signup` | public, JSON only | `{email, password, display_name?}` | 202 `{status: "check_email"}` (always); 422 password rules; 429 + `Retry-After` |
| `POST /api/v1/auth/verify-email` | public | `{token}` | 200 `{status: "verified"}`; 400 `{detail: "This link is not valid any more"}` (unknown, used or expired: same) |
| `POST /api/v1/auth/verify-email/resend` | session + CSRF | — | 202 |
| `POST /api/v1/auth/login` | public, JSON only | `{email, password}` | 200 `SessionOut` + cookie; 401 "Email or password is incorrect" (every failure); 429 |
| `POST /api/v1/auth/password/forgot` | public | `{email}` | 202 always |
| `POST /api/v1/auth/password/reset` | public | `{token, new_password}` | 204; 400 invalid link; 422 rules |
| `POST /api/v1/auth/password/change` | session + CSRF | `{current_password, new_password}` | 200 `SessionOut` + **new** cookie; 401 wrong current; 422 |
| `GET /api/v1/auth/session` | session | — | `SessionOut` + `user: {user_id, email, display_name, email_verified}`, `tenants[].status` |
| `PATCH /api/v1/account/profile` | session + CSRF | `{display_name}` | 200 user |
| `POST /api/v1/invitations/preview` | public, rate limited | `{token}` | 200 `{business_name, roles, email_hint: "a***@example.com", expires_at}`; 404 generic |
| `POST /api/v1/invitations/accept` | session + CSRF | `{token}` | 200 `{tenant_id}`; 404 generic "This invitation cannot be used" |
| `POST /api/v1/businesses` | session + CSRF, verified email | `{name, country, time_zone, quote_validity_days, pricing: {currency, tax_rate} \| null}` | 201 `{tenant_id}`; 403 unverified; 429 |
| `GET /api/v1/tenants/{t}/business` | member | — | `{name, country, time_zone, status, went_live_at, profile: {...version}, has_logo}` |
| `PUT /api/v1/tenants/{t}/business` | `tenant:manage` | `{name, country, time_zone}` | 200 |
| `PUT /api/v1/tenants/{t}/business/profile` | `tenant:manage` | `{legal_name, address, tax_id_label, tax_id, expected_version}` | 200 `{version}`; 409; 422 unprintable characters |
| `PUT /api/v1/tenants/{t}/business/logo` / `DELETE` | `tenant:manage` | `{content_base64, content_type}` | 204; 422 invalid image |
| `GET /api/v1/tenants/{t}/business/logo` | member | — | `image/png`, `nosniff`, `no-store` |
| `GET /api/v1/tenants/{t}/pricing-policy` | member | — | all fields as **strings** for decimals, `complete: bool`, `version` |
| `PUT /api/v1/tenants/{t}/pricing-policy` | `tenant:manage` | `{currency, minor_units, rounding_mode, tax_rate, max_price_age_seconds, max_stock_age_seconds, quote_validity_days, expected_version}` | 200 `{version}`; 409 |
| `GET /api/v1/tenants/{t}/setup` | member | — | `{status, items: [{key, done, required}], can_go_live}` |
| `POST /api/v1/tenants/{t}/go-live` | `tenant:manage` | — | 200 `{status: "active"}`; 409 `{missing: [...]}` |
| `GET /api/v1/tenants/{t}/members` | `members:manage` | — | `[{user_id, display_name, email, roles, is_you}]` |
| `PUT /api/v1/tenants/{t}/members/{user_id}/roles` | `members:manage` | `{roles}` | 200; 409 last administrator |
| `DELETE /api/v1/tenants/{t}/members/{user_id}` | `members:manage` or self | — | 204; 409 last administrator |
| `GET /api/v1/tenants/{t}/invitations` | `members:manage` | — | `[{invitation_id, email, roles, status, email_status, expires_at, invited_by}]` |
| `POST /api/v1/tenants/{t}/invitations` | `members:manage` | `{email, roles}` | 202 `{invitation_id}` (no token ever); 409 already a member |
| `POST /api/v1/tenants/{t}/invitations/{id}/resend` | `members:manage` | — | 202 |
| `DELETE /api/v1/tenants/{t}/invitations/{id}` | `members:manage` | — | 204 |
| `GET /api/v1/tenants/{t}/api-keys` | `integrations:manage` | — | `[{key_id, name, role, created_at, created_by, revoked_at}]` |
| `POST /api/v1/tenants/{t}/api-keys` | `integrations:manage` | `{name}` | 201 `{key_id, token}` (shown once, `no-store`) |
| `DELETE /api/v1/tenants/{t}/api-keys/{key_id}` | `integrations:manage` | — | 204 |

`api/app.py`: wire stores, hasher, mailer (API side only needs the job queue), rate limiter, `build_account_router`, `build_business_router`; dev login stays exactly as today behind `SALES_DEV_LOGIN`.

### W8. Worker
`worker.py`: register `JobKind.SEND_ACCOUNT_EMAIL: AccountEmailHandler(SqlAccountEmailStore(engine), load_mailer(settings.mailer, settings.mailer_options), settings.public_portal_url)`. Every existing handler uses `job.require_tenant()`. Gate checks of §6 in delivery and (phase 1) outbound sending.

### W9. Configuration (`config.py`)
| Setting | Default | Rule |
|---|---|---|
| `SALES_DEV_LOGIN` (replaces `QUOTATION_AUTH_PROVIDER`) | on in development/test, off in production | `true` in production refused |
| `SALES_MAILER` | `console` | an entry point name; `console` refused in production |
| `SALES_MAILER_OPTIONS` | `{}` | non-secret options; secrets via `SecretStore` (D8) |
| `SALES_MAIL_FROM` | `no-reply@localhost.example` (dev) | required in production (Q19) |
| `SALES_PUBLIC_PORTAL_URL` | `http://localhost:8000/portal/` (dev) | `https` required in production |
| `SALES_TRUSTED_PROXY_COUNT` | 0 | ≥ 0 |
| `SALES_PASSWORD_HASH_CONCURRENCY` | 4 | 1-16 |

Business rules (password length, lockout, token lifetimes, rate limits) are **domain constants** after confirmation, not settings, so they cannot drift per installation.

### W10. Portal
Feature folders follow `.docs/development/portal-architecture.md`. Query keys always include the tenant where there is one.

**`features/auth`** (kept: session state only)
- `model/types.ts`: `AuthConfig {password_login, dev_login, dev_subjects}`; `Session.user`; `TenantInfo.status`; `Permission` adds `members:manage`, `integrations:manage`.
- `api/authRepository.ts`: keep `session`, `logout`, `config`; `devLogin` moves to `account`.
- `hooks/useSession.ts`: export `authKeys` and a `useSessionCache()` helper (`set(session)`, `refresh()`) so `account` and `business` update the session without reaching inside.
- Remove `ui/LoginPage.tsx`, `hooks/useLoginForm.ts`, `routes.tsx` (moved to `account`); keep `RequireSession`.

**`features/account`** (new; contract)
| Layer | Files |
|---|---|
| model | `types.ts`: `SignupInput`, `LoginInput`, `ResetInput`, `ChangePasswordInput`, `InvitationPreview` |
| api | `accountRepository.ts`: `signup`, `verifyEmail`, `resendVerification`, `login` (calls `setCsrfToken`), `devLogin`, `forgotPassword`, `resetPassword`, `changePassword`, `updateProfile`, `previewInvitation`, `acceptInvitation` |
| services | `passwordRules.ts` (same limits as the server, UI hint only), `emailHint.ts`, `tokenFromLocation.ts` (reads `token` from the hash query), `messages.ts` (generic texts: "If an account exists…") |
| hooks | `useSignupForm`, `useLoginForm` (password + optional dev section), `useVerifyEmail`, `useForgotPasswordForm`, `useResetPasswordForm`, `useChangePasswordForm`, `useProfileForm`, `useInvitation` (preview + accept; on success refresh session and select the tenant) |
| ui/pages | `SignupPage`, `CheckEmailPage`, `VerifyEmailPage`, `LoginPage`, `ForgotPasswordPage`, `ResetPasswordPage`, `ProfilePage`, `AcceptInvitationPage` |
| ui/components | `PasswordField` (show/hide, `autocomplete="new-password"`/`current-password`), `PasswordLoginForm`, `DevLoginForm` (only when `dev_login`), `VerifyEmailBanner` |
| routes | `accountPublicRoutes` (signup, verify-email, login, forgot-password, reset-password, invitations/accept) and `accountRoutes` (profile) |

Token pages remove the token from the address bar right after reading it (`history.replaceState`), and the page sets `<meta name="referrer" content="no-referrer">`.

**`features/business`** (new; contract)
| Layer | Files |
|---|---|
| model | `types.ts`: `BusinessSettings`, `BusinessProfile`, `PricingPolicy` (decimal **strings**), `Member`, `Invitation`, `ApiKeySummary`, `NewApiKey`, `SetupChecklist`, `SetupItem` |
| api | `businessRepository.ts` (create, settings, profile, logo, pricing, setup, goLive), `membersRepository.ts` (members, invitations), `apiKeysRepository.ts` |
| services | `pricingForm.ts` (percent text ↔ fraction string using string arithmetic, never floats; ISO 4217 minor-unit suggestions), `checklist.ts` (labels, order, link per item), `memberRules.ts` (`isLastAdministrator` UI hint), `logoFile.ts` (type/size check, base64), `countries.ts`/`timeZones.ts` (lists; browser time zone pre-fill) |
| hooks | `queryKeys.ts` (`["tenants", t, "business"]`, `"pricing"`, `"setup"`, `"members"`, `"invitations"`, `"api-keys"`); `useCreateBusiness` (on success: refresh session, `selectTenant(new)`, go to `/setup`), `useBusinessSettings`, `useUpdateBusiness`, `useProfileForm`, `useLogoUpload`, `usePricingForm`, `useSetupChecklist`, `useGoLive` (invalidates session + setup), `useMembers`, `useChangeRoles`, `useRemoveMember`, `useInvitations`, `useInvite`, `useApiKeys`, `useCreateApiKey` (the token lives only in the mutation result shown in a dialog; it is never put in the query cache, Zustand or storage) |
| ui/pages | `StartPage` (no business: "Create your business" + "Have an invitation? Open the link from your email"), `CreateBusinessPage`, `SetupPage` (checklist + Go live), `BusinessSettingsPage` (details + PDF details + logo), `PricingSettingsPage`, `MembersPage` (members + invitations), `ApiKeysPage` |
| ui/components | `SettingsNav`, `ChecklistItem`, `GoLiveButton`, `BusinessForm`, `ProfileForm`, `LogoUpload`, `PricingForm` (with "set later"), `MemberRow`, `RoleCheckboxes`, `InviteForm`, `InvitationList`, `ApiKeyList`, `ApiKeyCreatedDialog`, `StatusBanner` ("Setting up: nothing is sent to customers yet") |
| routes | `businessStartRoutes` (`/start`, `/businesses/new`; signed in, no tenant needed) and `businessRoutes` (`/setup`, `/settings/business`, `/settings/pricing`, `/settings/members`, `/settings/api-keys`) |

**`features/tenant`**: `TenantInfo.status`; `TenantSwitcher` navigates to `/`.

**`app/`**
- `router.tsx`: public `...accountPublicRoutes`; a `SignedInLayout` (RequireSession + TopBar) with `...accountRoutes`, `...businessStartRoutes`; the tenant layout with `HomeRedirect` (index: `setting_up` and `tenant:manage` → `/setup`, else `/quotations`), existing feature routes and `...businessRoutes`.
- `layout/AppLayout.tsx`: `TenantGate` redirects to `/start` instead of the dead-end text (`AppLayout.tsx:22`).
- `layout/TopBar.tsx`: "Setup" while `setting_up`; "Settings" when the person has any of `tenant:manage`, `members:manage`, `integrations:manage`; a "Profile" link; `StatusBanner`.

### W11. Remove the script and adjust tests
Delete `scripts/create_api_key.py` (if Q17 confirms). Update `setup.md` and `enquiry-intake.md` to use the portal. Integration tests keep creating keys through the owner helper (moved to `tests/support/` if it no longer belongs in `src/`).

### W12. Tests: see §10. W13. Docs: see §12. W14. Checks and live run: see §10.5.

## 8. Mailer and `send_account_email`
- **Port** `Mailer.send(RenderedAccountEmail)`; the message is plain text: `to`, `from`, `subject`, `body`, `idempotency_key` (`<purpose>:<ref_id>:<token row version>`).
- **Adapters:** `console` now (writes files under `storage_dir/dev-mail/`), real sender in phase 5 (SendGrid package with its own entry point; its API key through `SecretStore`). Choosing another sender = install its package and set `SALES_MAILER`; no code changes.
- **Job:** `send_account_email`, payload `{"purpose": "...", "ref_id": "<uuid>"}`; `tenant_id` NULL for verification, reset and notices; the business ID for invitations.
- **Honest outcomes:** `pending → sending` is a compare-and-set before sending; a crash after that leaves `sending`, which the next attempt turns into `unknown`, never a second email (same rule as deliveries, `application/delivery.py:93-101`). "Resend" creates a fresh token and email.
- **Production:** settings refuse `console` in production, so phase 2 alone cannot be deployed; this is a release gate until phase 5.

## 9. Migration plan (`0010_accounts`, down_revision `0009_outbound_messages`)
### Upgrade order
1. Create `users`; **backfill** one user per distinct `memberships.subject` (owner lifts FORCE RLS on `memberships` inside the transaction, as `0007_acceptance.py:61-65` does): `subject` kept, `email`/`email_normalized` = the subject when it looks like an email, else NULL; `email_verified_at` NULL; `display_name` NULL.
2. Add the FK `memberships.subject → users.subject`.
3. Create `user_credentials`, `email_verifications`, `password_resets`.
4. Alter `tenants`: add columns with `status` default `'active'` first, so existing rows become `active` with `went_live_at = created_at`; then change the default to `'setting_up'`; add the status trigger.
5. Create `tenant_profiles`; insert an empty row per existing tenant.
6. Alter `tenant_pricing_policies` (nullable columns, `ck_..._complete`, `updated_by`).
7. Create `invitations`, `admin_events` (+ immutable trigger).
8. Alter `api_keys`, `jobs` (nullable tenant, check, policy), `user_sessions` policies.
9. Create the four definer functions, owner, revoke/grant EXECUTE.
10. All grants listed in §7 W3.

### Downgrade (reverse, tested by `scripts/check_database.py --cycle`)
1. Drop the four functions.
2. Delete `send_account_email` jobs (lift FORCE RLS on `jobs` in the transaction), drop `account_email_jobs` policy and `ck_jobs_tenant`, set `jobs.tenant_id` NOT NULL.
3. Drop the new `user_sessions` policies; drop `api_keys` new columns (revoke grants first).
4. Drop `admin_events`, `invitations`, `password_resets`, `email_verifications`, `user_credentials`.
5. Pricing: **delete incomplete policy rows** (FORCE lifted in the transaction), restore NOT NULL, drop the complete check and `updated_by`, revoke UPDATE.
6. Drop `tenant_profiles` (logo files stay on disk under `tenants/<t>/profile/`), drop the tenants trigger and columns, revoke UPDATE.
7. Drop the memberships FK, drop `users`.

**Data loss on downgrade (documented, not silent):** passwords, verification/reset/invitation records, audit events, PDF details and statuses are lost. Businesses created through sign-up stay as tenants with memberships whose subjects are `user:<uuid>`; nobody can sign in to them on the old code (the old dev login only offers demo people). Their incomplete pricing policies are deleted, so they cannot price, which matches their state before.

### Seed
`persistence/seed.py` creates `users` for the demo subjects (verified, no password), `active` status and empty profiles, idempotently; `api/app.py:115` keeps listing them for the dev login. Demo PDFs keep printing the business name because the profile is empty (existing PDF tests unchanged).

## 10. Test plan
### 10.1 Unit (`tests/unit/...`, fakes in `tests/unit/conftest.py` or a new `tests/unit/accounts_fakes.py`)
| Area | Tests |
|---|---|
| `domain/accounts` | email normalisation table (case, spaces, NFKC, two `@`, 255 chars, IDN → punycode); password problems (short, long, common, contains email); lockout arithmetic; invitation status by time; `validate_people_roles` refuses `intake_service` and empty; `check_administrator_remains` (remove last, demote last, two admins); `missing_for_go_live` |
| `domain/tenancy` | `members:manage` only for administrator; status gate functions table |
| `SignupService` | existing vs new email: same result, both hash once (timing parity by call count), existing queues `ACCOUNT_EXISTS`; rate limit → 429 path; password rules |
| `LoginService` | unknown email verifies the dummy hash; wrong password increments; 10th failure locks; locked + correct password still fails with the same error; success resets and re-hashes; reset revokes all sessions, change revokes others; disabled user fails generically |
| `InvitationService` / `MemberService` | permission checks, existing member 409, re-invite revokes older, accept with different email fails generically, last administrator 409 |
| `BusinessService` | unverified cannot create; pricing all-or-nothing; version conflict 409; unprintable profile text 422; go-live missing items; suspended blocks changes |
| `AccountEmailHandler` | token minted before send and only its hash stored; payload has no token; consumed row → no email; `MailUncertain` → `unknown` and no retry; crash in `sending` → `unknown`; invitation link uses the fragment |
| Adapters | Argon2 parameters in the encoded hash, `needs_rehash` on old params, semaphore caps concurrency; console mailer writes file, logs no address/token; logo normalizer refuses SVG/GIF, oversize, decompression bomb, wrong magic bytes; PDF v2 shows legal name, address, tax ID, logo; profile text escaped |
| API (`tests/unit/api/test_accounts.py`, `test_business.py`) | status codes above; cookie flags on password login; CSRF required on every signed-in change; **`text/plain` body to login/sign-up refused (422)**; no token in any response except API key creation, which is `no-store`; enumeration: identical body/status for existing vs new sign-up, known vs unknown forgot, unknown vs wrong vs locked login; non-member 404; dev login still works and is absent when disabled |
| Config | dev login and console mailer refused in production; https portal URL required in production |
| Architecture | `argon2`, `PIL` allowed only in adapters |

### 10.2 PostgreSQL integration (`tests/integration/...`, runtime roles, never the owner for behaviour)
Fixture changes: add the new tables to the truncate list (`tests/integration/conftest.py:27-33`); a fixture that removes self-created businesses (`TRUNCATE tenants, users CASCADE` with owner, then `seed_demo_tenants` again).

| File | Cases |
|---|---|
| `persistence/test_accounts.py` | users invisible without GUCs; email lookup shows exactly one row; tenant directory shows only members of the current tenant (admin of A cannot see a user who is only in B); `user_credentials` invisible to the worker (permission denied) and to other `app.user_id`; tokens single-use under two concurrent consumers; expired token refused |
| `persistence/test_create_business.py` | creates tenant + admin + approver + policy row + profile + audit; tenant visible to the creator's session lookup only; **abuse:** direct `INSERT INTO tenants` as `quotation_app` → permission denied; `quotation_worker` cannot `EXECUTE` (`has_function_privilege` false and call fails); call with another user's id (`app.subject` mismatch) → 42501; unverified or disabled user → 42501; `app.subject` unset → 42501; invalid country/time zone/partial pricing → error and nothing inserted; function is `prosecdef`, owned by `quotation_owner`, `proconfig` contains the fixed `search_path`; caller's `app.tenant_id` restored after the call; two calls create two separate businesses that cannot see each other |
| `persistence/test_members.py` | `accept_invitation`: wrong email, expired, revoked, already accepted → generic error; two concurrent accepts → one acceptance; marks email verified. `change_member_roles` / `remove_member`: non-administrator caller refused; **two administrators removing each other at the same time → exactly one succeeds, one administrator remains**; removal revokes channel bindings; audit rows written; `admin_events` UPDATE/DELETE rejected even for the owner; runtime still cannot `INSERT INTO memberships` (updated `test_tenancy.py:54-69`) |
| `persistence/test_tenancy.py` (update) | pricing UPDATE allowed for own tenant only (other tenant: 0 rows); incomplete policy → `PricingPolicyNotConfigured`; status trigger: app may go `setting_up → active`, may not set `suspended` or go back; owner may suspend |
| `persistence/test_jobs.py` (update) | app may insert a NULL-tenant job only for `send_account_email`; other kinds with NULL refused; worker claims it |
| `persistence/test_sessions.py` (new) | revoke-all by subject revokes only that person's sessions |
| `persistence/test_api_keys.py` (update) | runtime creates a key in its tenant only (other tenant refused by RLS); created key authenticates; revoked fails |
| `persistence/test_database.py` | readiness true at `0010_accounts`; still false for the owner |
| `api/test_accounts_flow.py` (new, end to end with worker `--once`) | sign up → read console mail file → verify → log in → create business → set details, profile, pricing → create API key → submit a test enquiry → checklist shows test done → go live; invite a second person → they sign up, verify, accept → change roles → remove. **Isolation:** two self-created businesses: every `/tenants/{other}` route returns 404, lists contain no foreign rows, invitation token of A cannot add anyone to B |
| `workflows/test_quotation.py` (update) | approved PDF shows the profile; profile edited after approval does not change the stored PDF |
| Migration | `check_database.py --cycle` (upgrade, downgrade to base, upgrade, retest); a test that upgrades a database containing demo data to 0010 and checks the backfill (users per subject, tenants active, empty profiles) and downgrade back to 0009 with a self-created business present |

### 10.3 Portal (`portal/tests/features/...`, mirrors `src`)
- `account/services/*`: password rules, token parsing, email hint.
- `account/ui/pages/SignupPage.test.tsx`: same "check your email" for any answer; password hints.
- `account/ui/pages/LoginPage.test.tsx`: password form; dev section only when `dev_login`; generic error text; success stores CSRF and goes home.
- `account/ui/pages/ResetPasswordPage.test.tsx`: token removed from the URL; success message; invalid link message.
- `account/ui/pages/AcceptInvitationPage.test.tsx`: preview; signed-out person sent to login and back; accept selects the new business.
- `business/services/pricingForm.test.ts`: `"18"` % ↔ `"0.18"` exact, rejects `"18.0000001"` beyond 6 decimals, "set later".
- `business/ui/pages/SetupPage.test.tsx`: items from the server, Go live disabled until `can_go_live`, 409 shows missing items.
- `business/ui/pages/MembersPage.test.tsx`: remove hidden for the last administrator; server 409 message shown.
- `business/ui/components/ApiKeyCreatedDialog.test.tsx`: token shown once, not present in the query cache after closing.
- `app` / `tenant`: zero businesses → `/start`; `setting_up` home → `/setup`; switcher goes home.

### 10.4 Architecture tests
- Backend allow-list (`tests/unit/test_architecture.py:21-30`): add `argon2`, `PIL` to adapters.
- Portal: new rule "browser storage (`localStorage`, `sessionStorage`) only in `features/tenant/store`", so tokens can never be stored by mistake. Existing rules cover the new features (index-only, layers, fetch only in `shared/api`).

### 10.5 Checks and live run
- `uv run ruff check`, `ruff format --check`, `mypy`, `pytest tests/unit`, `scripts/check_database.py --cycle`, evaluation; in `portal/`: `npm run typecheck`, `npm test`, `npm run build`.
- **Live run:** compose up, `alembic upgrade head`, seed, API + worker + portal. In a browser: sign up, open the file in `var/storage/dev-mail/`, verify, log in, create a business, fill settings, create an API key, send a test enquiry (`scripts/demo_quotation.py` with that key), see it in the portal, invite a colleague (second browser profile), accept, approve a quotation, download the PDF with the profile, Go live. Then create a second business with another person and check nothing of the first is visible. Record evidence in the handoff.

## 11. Security checklist
| Topic | Control |
|---|---|
| Password storage | Argon2id (t=3, m=64 MiB, p=4, to confirm), per-hash random salt (library), encoded string with parameters, re-hash on login when parameters change; passwords never logged, never in job payloads, never returned |
| Hash DoS | Max 128 characters before hashing; concurrency semaphore; rate limits before hashing |
| Timing | Unknown email verifies a dummy hash; existing-email sign-up still hashes; locked accounts still verify; email sending is always asynchronous (job), so response time does not reveal anything |
| Enumeration | Same status and body for: new/existing sign-up, known/unknown forgot password, unknown/wrong/locked login, invalid/used/expired tokens, foreign/unknown tenant (404, existing rule) |
| Tokens | 256-bit random (`secrets.token_urlsafe(32)`), only SHA-256 stored (as API keys, `application/api_keys.py:218-219`), single use by atomic compare-and-set, short expiry (Q6), minted by the worker, carried in the URL **fragment**, removed from the address bar after use, `no-referrer` |
| Sessions | Existing BFF cookie (HttpOnly, SameSite=Strict, Path=/api, Secure in production); new session on every login (no fixation); all sessions revoked after reset; other sessions revoked after change; removal from a business takes effect on the next request |
| CSRF | Existing `X-CSRF-Token` for every signed-in change; unauthenticated POSTs accept `application/json` only (a cross-site form cannot send it without a CORS preflight, and no CORS is allowed); tested |
| Rate limits and lockout | §3 Q3/Q4; 429 with `Retry-After`; lockout temporary; notice email |
| Least privilege | Runtime role: no INSERT on tenants or memberships; access changes only through four definer functions with fixed `search_path`, EXECUTE for `quotation_app` only, caller identity from `app.subject` checked against stored data; worker cannot read credentials |
| RLS for global users | Narrow GUC-scoped policies (self, presented email, presented token, current tenant's members) |
| Audit | `admin_events` append-only (grants + `reject_change()` trigger), written in the same transaction as each change, with actor and old/new values, never secrets |
| Email content | Plain text, fixed templates, user-supplied names length-limited and stripped of line breaks (no header injection); links only to `SALES_PUBLIC_PORTAL_URL` |
| Uploads | Logo: magic-byte check, size and pixel limits, re-encoded PNG, stored under a server-generated key; served `nosniff`, `no-store` |
| PDF | Profile text validated with the renderer's own character rule on save; renderer stays data-only |
| Configuration | Dev login and console mailer refused in production; https portal URL in production |
| Logging | User IDs only; no emails, tokens, hashes or passwords in logs or job errors (existing "exception type only" rule) |

## 12. Documentation to update
| Document | Change |
|---|---|
| New ADR (next free number; 0019 if phase 1 takes 0018) "Accounts, sign-up and business onboarding" | Password provider behind the identity port, users/subjects, definer functions, statuses and Go live, token and email design, rate limits; supersedes the "OIDC required before any other deployment" line in ADR 0013 (D1) |
| ADR 0013 | Status note: email + password added (D1); OIDC no longer planned |
| ADR 0002 | Verification line: definer functions and two-business onboarding isolation tests |
| ADR 0011 | Key management now in the portal |
| `.docs/features/accounts-and-onboarding.md` (new) | Flows, endpoints, statuses, checklist, emails, limits |
| `.docs/development/configuration.md` | New settings; `QUOTATION_AUTH_PROVIDER` replaced |
| `.docs/development/setup.md` | Sign up locally, where dev emails appear, API keys from the portal (script removed) |
| `.docs/development/code-map.md`, `testing.md`, `portal-architecture.md` (features table) | New modules, tests, `account` and `business` features |
| `.docs/operations/security.md` | "Implemented controls (phase 2)"; replace "Use OIDC authentication" with the D1 decision |
| `.docs/features/enquiry-intake.md`, `quotation-delivery.md` | Keys from portal; PDF details; setting-up gate |
| `.docs/plan/platform/07-decisions-needed.md` | Answers to Q1-Q27 |
| `.docs/handoff.md`, `.docs/agent-context.md` | Work done, checks, evidence, next step |

## 13. Risks
| Risk | Mitigation |
|---|---|
| Phase 1 names (connections, outbox, `TemplateKey`, `SecretStore`, API key ↔ `api` connection) change | Phase 2 starts only after phase 1 is merged; the gate points and API key creation follow phase 1's final code |
| Argon2 memory on a small VM; login floods | Semaphore, length cap, rate limits before hashing |
| In-memory throttles multiply with several API processes | Documented; lockout is in PostgreSQL; shared store when hosting is chosen (Q5) |
| Subtle enumeration through timing or different code paths | Explicit parity tests; one generic error type per flow |
| Mistakes in SECURITY DEFINER functions (search path, missing checks) | Fixed `search_path`, no tenant ID parameter for creation, caller checked against stored data, dedicated abuse tests, owner-only CREATE on schema (`init.sql:23-26`) |
| Mixed subject formats (`user:<uuid>` and demo emails) | FK to `users.subject`; subjects never parsed for meaning |
| Logo parsing (Pillow vulnerabilities) | Small limits, decompression-bomb guard, re-encoding, pinned Pillow range, updates |
| Downgrade loses account data | Documented in the migration docstring and §9; tested |
| No production email sender until phase 5 | Settings refuse console in production; release gate recorded |
| Scope creep (email change, account deletion, 2FA, operator UI) | Listed as out of scope / open questions |
| Test database cleanup of self-created businesses | Dedicated fixture; reseed after truncation |
| Pre-fills mistaken for decisions | Every pre-fill is visible, editable, labelled "suggested", and only counts once saved (Q14) |

## 14. Definition of done
- Q1-Q27 answered and recorded; plan updated to match.
- Migration `0010_accounts` upgrades, downgrades and re-upgrades cleanly with data; `alembic check` clean; readiness expects `0010_accounts`.
- A new person can, **without any script**: sign up, verify, log in, create a business, set details, PDF details and pricing, invite and manage members, create and revoke an API key, send a test enquiry, see the checklist complete and Go live.
- Two self-created businesses are isolated in every new table, route and function (integration tests).
- All SECURITY DEFINER abuse tests pass; runtime role still cannot insert tenants or memberships directly.
- Enumeration, timing-parity, CSRF, rate-limit and lockout tests pass.
- Account emails work in development through the console mailer and the `send_account_email` job; production refuses the console mailer.
- Dev login and the demo seed still work; all existing unit, integration, evaluation and portal tests are green; CI green on the branch.
- Portal: `account` and `business` features pass typecheck, tests (including the architecture test) and build.
- Live run done and recorded; documentation in §12 updated; ADR added; handoff updated.
- No commit, push or pull request without the user's explicit permission (AGENTS.md).

**Rough size:** large, about the size of M4. Backend: about 6 new modules, about 12 changed modules, one migration of roughly 450 lines with four functions. Portal: two new features with about 45 files, changes in `auth`, `tenant` and `app`. Tests: about 120 new or changed cases across unit, PostgreSQL integration and portal.

## 15. Proposed changes to shared contracts
**Merged (2026-10-26):** these proposals are now part of [00-shared-contracts.md](00-shared-contracts.md) revision 2; where they differed, the contracts file wins (e.g. one name, `catalogue_reader`, for the catalogue factory method). Points needing the user's decision are listed there in §8.

These are deviations from or additions to [00-shared-contracts.md](00-shared-contracts.md). Per its rule, they must be written **into that file** once the user agrees; this plan does not change it.

| # | Contract section | Proposed change | Reason |
|---|---|---|---|
| C1 | §4 / §5 jobs | `jobs.tenant_id` NULL-able only for `send_account_email`; `ClaimedJob.tenant_id: UUID \| None` with `require_tenant()`; `queue_job` accepts `None` | Sign-up, verification and reset emails belong to no business |
| C2 | §4 `0010_accounts` | Also: `admin_events` (append-only audit); FK `memberships.subject → users.subject`; `tenants.went_live_at`, `updated_at` and a status-transition trigger; nullable commercial columns + completeness check on `tenant_pricing_policies`; `api_keys.created_by/revoked_by` and runtime INSERT/UPDATE grants; `user_sessions` per-subject policies; `jobs` policy/check (C1) | Audit of membership changes, "set later", portal API keys, revoke-all sessions |
| C3 | §4 `0010_accounts` functions | Besides `create_business(...)`: `accept_invitation`, `change_member_roles`, `remove_member` (all SECURITY DEFINER, owner, EXECUTE for `quotation_app` only). `create_business` checks `p_user_id` against `app.subject` | Keeps "runtime cannot insert memberships" (Q26); last-administrator rule enforced in the database under a lock |
| C4 | §2 ports/application/adapters | Additive: ports `MemberStore`, `BusinessStore`, `ApiKeyAdminStore`, `AccountEmailStore`, `RateLimiter`, `LogoNormalizer`; services `MemberService`, `ApiKeyAdminService`, `AccountEmailHandler`; adapters `adapters/accounts/rate_limit.py`, `adapters/documents/logo.py`; `SessionStore.revoke_all_for` | Needed for member management, API keys, emails and limits |
| C5 | §2 third-party packages | Add Pillow (`PIL`) as a direct adapters dependency (already installed through ReportLab) | Logo validation and re-encoding |
| C6 | §7 portal | `account` covers sign-up/login/password/profile/invitation acceptance; the existing `auth` feature stays for session state and the route guard (contract does not say whether `account` replaces `auth`) | Avoid moving session plumbing used by every feature |
| C7 | §1 vocabulary | `User.subject` is `user:<uuid>` for sign-ups; legacy subjects (demo emails) are kept | Existing `subject` columns keep working |
| C8 | Settings (not in contract) | `QUOTATION_AUTH_PROVIDER` replaced by `SALES_DEV_LOGIN`; new `SALES_MAILER` (entry point name, group `sales_platform.mailers`), `SALES_MAIL_FROM`, `SALES_PUBLIC_PORTAL_URL`, `SALES_TRUSTED_PROXY_COUNT` | Password login is always on; mailer chosen generically |
| C9 | §1 / phase 1 `TemplateKey` | Add six `account_*` template keys | Account email wording uses the same template mechanism |
