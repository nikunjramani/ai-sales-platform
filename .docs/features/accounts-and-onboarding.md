# Accounts and onboarding (phase 2)
A new business goes from zero to live in the portal, without scripts. Decisions: [ADR 0019](../adr/0019-accounts-and-onboarding.md); plan and user answers: [phase 2](../plan/platform/implementation/phase-2-accounts.md).

## The flow, in simple words
1. **Sign up** with an email and a password. The screen always says "check your email", so nobody can find out which addresses have accounts.
2. **Confirm the email** with the link (valid 24 hours). You can sign in before that, but cannot create or join a business.
3. **Create a business:** name, country, time zone and how long quotations stay valid. Currency and tax rate can wait ("set later"). You become its administrator and approver.
4. **Set it up:** the Setup page lists what is still missing and links to each screen:
   - business details;
   - the details printed on quotations (legal name and address; tax ID and logo optional);
   - complete pricing;
   - an approver;
   - an API key for your website or system;
   - one test enquiry through each customer tool.

   Products come in phase 3, so they are shown but not required yet.
5. **Go live.** Until then nothing is pushed to customers. Your own system can still fetch quotations through the API, so you can test. Going live cannot be undone.
6. **Invite colleagues** with one or more roles. The link (valid 7 days) works only for the invited address and joins them at once; it also confirms their email.

## Who may do what
| Action | Who |
|---|---|
| Business details, PDF details, logo, pricing, Go live | administrators (`tenant:manage`) |
| See members, invite, change roles, remove | administrators (`members:manage`) |
| Create and revoke API keys | administrators (`integrations:manage`) |
| Leave a business | everyone, except the last administrator |
| See the setup checklist and settings | every member |

## Limits and timings
| What | Value |
|---|---|
| Password | 12 to 128 characters; common passwords and your email name refused |
| Login | 30 tries per 10 minutes per IP address, 10 per 10 minutes per email; no lockout |
| Sign-up | 10 per hour per IP address |
| Forgot password | 5 per hour per email, 20 per hour per IP address |
| Resend verification | 1 per minute and 5 per day per person |
| Invitations | 50 per day per business; opening or accepting one: 30 per 10 minutes per IP |
| Create a business | 5 per day per person, no total limit |
| Links | verify 24 hours, reset 30 minutes, invitation 7 days; each works once |

A refused request answers 429 with `Retry-After`.

## Business status
| | setting_up | active | suspended |
|---|---|---|---|
| Settings, members, keys | change | change | read only |
| Enquiries through the API | yes (test) | yes | refused (403) |
| Drafting and approval | yes | yes | jobs wait |
| Quotations pulled by your own API client | yes | yes | no |
| Messages pushed to customers | **no** ("business is not live yet") | yes | no |

Suspension is set only by the operator with owner credentials:
```sql
-- with the owner role, e.g. psql as sales_owner
SELECT set_config('app.tenant_id', '<tenant id>', false);
UPDATE tenants SET status = 'suspended' WHERE id = '<tenant id>';  -- or 'active' to lift it
```

## Emails
Account emails are jobs (`send_account_email`) sent by the worker through the mailer chosen in `SALES_MAILER`. In development the `console` mailer writes each email to `var/storage/dev-mail/<time>-<key>.txt`; open the file and follow the link. Production refuses the console mailer, so phase 2 alone cannot be deployed (a real sender comes in phase 5).

| Email | When |
|---|---|
| Confirm your email address | sign-up, "send again" |
| Reset your password | forgot password (only if the account exists) |
| You are invited to ... | an invitation, "resend" |
| Someone tried to sign up with your email address | sign-up with an existing address (at most the resend allowance) |
| Your password was changed | reset or change |

## API
Routes without a session accept `application/json` only. Answers never contain tokens, except a new API key, which is shown once with `Cache-Control: no-store`.

| Route | Purpose |
|---|---|
| `POST /api/v1/auth/signup`, `/auth/verify-email`, `/auth/verify-email/resend` | sign up and confirm |
| `POST /api/v1/auth/login`, `/auth/password/forgot`, `/auth/password/reset`, `/auth/password/change` | passwords |
| `PATCH /api/v1/account/profile` | display name |
| `POST /api/v1/invitations/preview`, `/invitations/accept` | join a business |
| `POST /api/v1/businesses` | create a business |
| `/api/v1/tenants/{t}/business`, `/business/profile`, `/business/logo`, `/pricing-policy`, `/setup`, `/go-live` | settings and Go live |
| `/api/v1/tenants/{t}/members`, `/invitations`, `/api-keys` | people and keys |

Details of each route: the module docstrings of `api/accounts.py` and `api/business.py`.

## Failure behaviour
- **Unknown, used or expired link:** "This link is not valid any more" (one message for all).
- **Saving PDF details or pricing that someone else changed meanwhile:** 409; the screen reloads the current values.
- **Removing or demoting the last administrator:** 409, also when two administrators try at the same time.
- **Database down:** 503; nothing is recorded.
- **Email outcome unknown** (the mail service timed out after taking it): recorded `unknown` and not resent; press "send again" or "resend" for a fresh link.
