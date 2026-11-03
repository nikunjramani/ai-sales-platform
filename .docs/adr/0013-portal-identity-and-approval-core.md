# 0013: Portal identity, immutable versions and the shared approval core
Date: 2026-10-22
Status: accepted; implemented in M4 (commit 21f0ba5, [CI run 39900219005](https://github.com/nikunjramani/ai-sales-quotation-order-processing/actions/runs/39900219005) passed)
Implements: [0007](0007-channel-independent-approval.md) for the portal and a simulated channel
Extended by [0019](0019-accounts-and-onboarding.md) (phase 2): email + password login (D1) produces the same `VerifiedIdentity` and session; OIDC is no longer planned, and the dev login stays for development only.
Extended by [0018](0018-connector-framework.md) (phase 1): the `ApprovalChannel` enum and notifier map are replaced by connections and the outbox; the old simulated route is a deprecated alias.

## Context
M4 needs people (managers, sales operators) to sign in, review exact quotation content and decide, from the portal or from a messaging channel, with the same rules. The user chose (2026-10-22):
- a provider-independent login with a free development sign-in now and OIDC later;
- a separate React app for the portal.

## Decision
**Identity and sessions**
- Identity providers only prove who someone is and return a `VerifiedIdentity` ([ports/identity.py](../../src/sales_platform/platform/ports/identity.py)). The dev provider lets you pick a stored synthetic member without a password. Settings refuse it in production. Adding OIDC means one adapter (authorization code + PKCE, ID-token validation) that produces a `VerifiedIdentity`; nothing else changes.
- The API issues its own server-side session (backend-for-frontend pattern). The random token lives only in an `HttpOnly; SameSite=Strict; Path=/api` cookie (Secure in production), and only its SHA-256 hash is stored. The React app never holds tokens.
- State-changing requests must also send `X-CSRF-Token`, derived from the session token (SHA-256), which other sites cannot read.
- The tenant in a URL is only a request: stored membership is re-resolved on every request, and non-members get 404.

**Quotations**
- When extraction is ready, a `draft_quotation` job prices the lines with the M1 calculator and stores version 1 as an immutable JSON snapshot with a SHA-256 content hash.
- Unpriceable lines produce `pricing_failed` with the issues listed, never a guessed price.
- Revisions are new versions that a sales operator submits by changing products or quantities. They are re-priced from the ERP; prices cannot be typed.
- Versions and decisions are append-only: no role has UPDATE/DELETE, and a trigger rejects changes even from the owner.

**Approval core**
- A single `ApprovalService.decide(subject, DecisionCommand)` is used by every entry point.
- Under a row lock on the task, it checks:
  - current membership and approve permission;
  - the task is pending;
  - the content hash matches and the version is current;
  - the decider did not create the version (no self-approval).
- In one transaction it records one decision per task, closes the task, updates the quotation, consumes the channel action reference and queues a `resume_quotation` job.
- An exact replay (same command ID) returns the original decision. Anything else gets a conflict that shows the recorded decision.
- A reason is required to reject or request a revision.

**Workflow**
- A second LangGraph graph (`quotation-approval-v1`) runs draft -> request approval -> interrupt -> apply decision -> approved | rejected | interrupt for revision -> request approval. Its checkpoints are in PostgreSQL.
- Resume jobs carry what they answer; anything that does not match the current pause is ignored as stale.
- A PostgreSQL advisory lock per run ensures only one worker advances a run.

**Manager channels**
- Enrollment: a signed-in approver gets a one-time 10-minute code and sends it from the account, as proof of control. That creates a revocable binding of tenant, person, connection and external user.
- Review packets go only to active bindings of current approvers. Each carries an opaque, single-use, 24-hour reference bound to one task and one binding.
- Before any processing, inbound events must have a valid provider signature (HMAC-SHA256 for the simulated provider). The reference must belong to the same account and connection, so forwarded messages fail.
- Only a simulated provider exists (outbox files plus signed webhook). Real providers are M7 adapters behind the same `ApprovalNotifier` port and `ChannelActionService` (email approval was moved out of M5 by the user; [ADR 0015](0015-quotation-documents-and-delivery.md)).

## Alternatives
- Tokens in browser storage: readable by injected scripts. Rejected.
- Keycloak now: about 1 GB RAM and setup on this VM. Deferred.
- Hosted IdP now: accounts and IT approval needed. Deferred.
- Mutable quote rows with an approved flag: no binding of a decision to content. Rejected.
- Channel-specific approval logic: rejected (ADR 0007).
- Server-rendered portal: simpler, but the user chose React.

## Consequences
- Node 24 tooling for the portal (portal/, pinned versions, package-lock.json) and a CI job.
- The worker role may now read memberships, tenants and pricing policies, still only with tenant context.
- No approval deadlines/escalation, delegation, multi-level approval or quote expiry yet; these are recorded in open decisions.
- The dev login proves nothing about the person: never enable it outside development/test.

## Verification
- Unit: services, graph, routes over in-memory fakes.
- PostgreSQL integration:
  - portal approval and revision loop;
  - signed-channel approval with forwarded-message rejection;
  - two simultaneous decisions producing exactly one;
  - resume after worker restart;
  - append-only enforcement;
  - session/membership RLS;
  - tenant isolation.
- Live manual run with uvicorn, the worker and scripts/simulated_channel.py.
