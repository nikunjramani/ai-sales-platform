# Quotation drafting, approval and the portal (M4)
**Status:** M4 is complete: verified locally on PostgreSQL, by a live run and in [CI run 39900219005](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39900219005).

**Business purpose:** every quotation is checked by a person with approval rights before it can go to a customer. Managers can decide in the portal or from an enrolled messaging account, under exactly the same rules.

## Terms in simple language
- **Version:** a frozen snapshot of a quotation (lines, ERP prices and their price lists, stock status, tax, totals). It never changes. A change makes a new version.
- **Content hash:** a fingerprint (SHA-256) of a version. A decision states the hash the manager saw, so it can never apply to different content.
- **Approval task:** "version N needs a decision". There is one per version and at most one decision.
- **Interrupt / resume:** the LangGraph run pauses, stored in PostgreSQL, until a decision or revision arrives. A resume job then continues it, even after the worker restarted.
- **Session / CSRF token:** the sign-in cookie, which page scripts cannot read, plus a header value that only this page knows. Both are required for changes.
- **Channel binding:** "this messaging account belongs to this manager", created by sending a one-time code from that account.
- **Action reference:** the hidden token behind a message's buttons. It works once, for one task, from that one account, for 24 hours.

## Source files
- **Domain:** [domain/quotations.py](../../src/sales_platform/domain/quotations.py) holds statuses, `DecisionCommand`, `quotation_content`, `content_sha256`; the review wording is `render_template(TemplateKey.APPROVAL_REQUEST, ...)` in [domain/messages.py](../../src/sales_platform/domain/messages.py).
- **Application:**
  - [application/approvals.py](../../src/sales_platform/application/approvals.py) (`ApprovalService`)
  - [application/quotations.py](../../src/sales_platform/application/quotations.py) (list, get, revise)
  - [application/channel_actions.py](../../src/sales_platform/application/channel_actions.py) (enrollment, channel decisions)
  - [application/notifications.py](../../src/sales_platform/application/notifications.py) (review delivery job)
  - [application/sessions.py](../../src/sales_platform/application/sessions.py)
- **Workflow:** [workflows/quotation.py](../../src/sales_platform/workflows/quotation.py) (graph and `QuotationJobHandler`).
- **API:**
  - [api/auth.py](../../src/sales_platform/api/auth.py) (sign-in and sessions)
  - [api/portal.py](../../src/sales_platform/api/portal.py) (tenant routes)
  - [api/channels.py](../../src/sales_platform/api/channels.py) (signed webhook)
- **Persistence:**
  - [persistence/quotations.py](../../src/sales_platform/persistence/quotations.py)
  - [persistence/channels.py](../../src/sales_platform/persistence/channels.py)
  - [persistence/sessions.py](../../src/sales_platform/persistence/sessions.py)
  - [0005_approvals.py](../../migrations/versions/0005_approvals.py)
- **Adapters:**
  - [adapters/identity/dev.py](../../src/sales_platform/adapters/identity/dev.py)
  - [adapters/providers/simulated/](../../src/sales_platform/adapters/providers/simulated/provider.py) (phase 1: a provider; approval requests go through the outbox, the action token is created at send time; see [connections](connections.md))
- **Portal:** [portal/](../../portal/src/app/App.tsx) (React; see [portal architecture](../development/portal-architecture.md)).
- **Dev helper:** [scripts/simulated_channel.py](../../scripts/simulated_channel.py).

## Flow
1. M3 records `ready_for_quotation` and, in the same transaction, queues `draft_quotation`.
2. **draft:**
   - The lines are priced with live ERP facts and the tenant policy (M1).
   - If they price, version 1 is stored and the quotation becomes `pending_approval`.
   - If not, it becomes `pricing_failed` with the issues listed, and nothing is guessed.
3. **request_approval:** create the task once, then queue `notify_approvers`.
4. **notify_approvers:**
   - For each active binding of a current approver, store a reference hash first, then send the review packet.
   - A failed send is retried with a fresh token.
5. **await_decision:** the run pauses.
6. **Decision (portal or channel):** `ApprovalService.decide` checks, under a lock:
   - current approve permission;
   - the task is pending;
   - the hash and version are current;
   - the decider is not the creator.

   It then records everything atomically and queues `resume_quotation`.
7. **apply_decision:**
   - approve -> run `approved`, which queues delivery of the approved PDF ([quotation delivery](quotation-delivery.md), M5).
   - reject -> run `rejected`.
   - request revision -> pause for a revision.
8. **Revision:** a sales operator submits new lines and a reason. The server re-prices, stores version N+1 (if the base version is still current) and queues a resume. The graph then requests approval for the new version.

## API (portal, cookie session)
| Route | Who | Notes |
|---|---|---|
| `GET /api/v1/auth/config` | anyone | Sign-in method; demo people for the dev login |
| `POST /api/v1/auth/dev-login` | dev only | `{subject}`; sets cookie, returns CSRF token and tenants |
| `GET /api/v1/auth/session`, `POST /api/v1/auth/logout` | signed in | Logout needs CSRF |
| `GET /api/v1/tenants/{t}/quotations[?status=]` | `quotations:view` | List |
| `GET /api/v1/tenants/{t}/quotations/{id}` | `quotations:view` | Versions, tasks, decisions, `can_decide`, `can_revise` |
| `POST .../approval-tasks/{task}/decision` | `quotations:approve` | `{command_id, action, content_sha256, reason?}`; replay 200, conflict/stale 409 |
| `POST .../quotations/{id}/revisions` | `quotations:draft` | `{command_id, base_version, lines, reason}`; 201, replay 200, 422 with pricing issues, 409 if changed |
| `GET .../enquiries[?status=]` | `enquiries:view` | Exception list (needs review/clarification) |
| `GET .../connections[?purpose=]` | admin: all; approver: linkable | The business's tools (phase 1) |
| `POST .../connections/{id}/enrollments` | `quotations:approve` | Code that links only on that connection; 404 / 422 |
| `POST .../channel-enrollments` (deprecated), `GET`/`DELETE .../channel-bindings` | approver / own (admin: all) | Link and revoke accounts; bindings show `connection_ref` and `connection_name` |
| `POST /api/v1/hooks/{connection_id}/{webhook_token}` | token + provider signature | Generic callbacks (phase 1); 200 `{received, duplicates}`, routed by the `process_inbound` job |

Non-members get 404 for any tenant URL. Missing or invalid CSRF gets 403, and no session gets 401.

## Tenant and auth boundary
- Every query filters by tenant and runs under forced RLS.
- The API role may update only specific columns (task status, quotation status/version, binding revocation and similar).
- Sessions have no tenant; the API can see only the row whose hash it presents.
- Membership and tenant-name lookups for the signed-in person use the narrow `app.subject` policies.

## Failures and recovery
| Failure | Behaviour |
|---|---|
| ERP outage while drafting | Job retried; nothing stored |
| Crash after drafting | Resumes at request_approval without re-pricing; one task |
| Resume before pause | Retried |
| Stale or duplicate resume | Ignored |
| Two decisions at once | One records; the other gets 409 showing the first |
| Forwarded message, revoked binding, expired reference, lost role | Refused |
| Notification send failure | Retried with a new token |

## Configuration
These are names only; see [configuration](../development/configuration.md):
- `QUOTATION_AUTH_PROVIDER`
- `SALES_SESSION_HOURS`
- `SALES_SESSION_COOKIE_SECURE`
- `SALES_SIMULATED_CHANNEL_SECRET`
- `SALES_PORTAL_DIST_DIR`

## Tests and example
- **Unit tests:**
  - [unit/workflows/test_quotation.py](../../tests/unit/workflows/test_quotation.py)
  - [unit/application/test_approvals.py](../../tests/unit/application/test_approvals.py)
  - [test_channel_actions.py](../../tests/unit/application/test_channel_actions.py)
  - [test_sessions.py](../../tests/unit/application/test_sessions.py)
  - [unit/api/test_portal.py](../../tests/unit/api/test_portal.py)
  - [unit/domain/test_quotations.py](../../tests/unit/domain/test_quotations.py)
- **PostgreSQL:** [integration/workflows/test_quotation.py](../../tests/integration/workflows/test_quotation.py).
- **Portal:** `npm test` in portal/ (tests in [portal/tests/](../../portal/tests/features/quotations/ui/components/DecisionPanel.test.tsx)).
- **Walkthrough:** in [setup](../development/setup.md#approvals-and-portal-m4).

## Known limitations
- Dev login only; OIDC is pending.
- No approval deadlines, escalation, delegation or multi-level policy.
- Quotations have no validity date yet (M6 offer lifecycle).
- Approved versions are rendered and delivered by M5 ([quotation delivery](quotation-delivery.md)).
- Only the simulated channel exists.
- Enquiry exceptions are listed but cannot yet be edited or answered in the portal.
- Revisions cannot add free-text terms or discounts (deferred, ADR 0008).

**Related ADRs:** [0007](../adr/0007-channel-independent-approval.md), [0013](../adr/0013-portal-identity-and-approval-core.md), [0004](../adr/0004-durable-execution.md), [0010](../adr/0010-deterministic-pricing-and-tenant-context.md).
