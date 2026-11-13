# 0018: Connector framework: providers, connections, secrets, one webhook route, one outbox
Date: 2026-10-27
Extended by [0021](0021-customers-conversations-and-website-chat.md): The first hosted customer tool (website chat, capability hosted_chat) and consent events (phase 4, ADR 0021).
Status: accepted; implemented and merged to main (PR #6, phase 1 of the [platform plan](../plan/platform/README.md)); extended by [0019](0019-accounts-and-onboarding.md)
Extends [0007](0007-channel-independent-approval.md), [0011](0011-durable-intake-and-jobs.md), [0013](0013-portal-identity-and-approval-core.md), [0015](0015-quotation-documents-and-delivery.md) and [0016](0016-customer-acceptance.md); supersedes their per-channel maps (`DeliveryChannel`, `ApprovalNotifier`, the `Channel`/`ApprovalChannel` enums). Plan: phase 1 (git history), shared contracts (git history).

## Context
Every business will use its own tools: WhatsApp, Telegram, email, website chat, an ERP or shop. Before phase 1, tools were named in code:
- a `Channel` enum and an `ApprovalChannel` enum;
- a delivery map and a notifier map in the worker;
- one hardcoded simulated ERP for every business.

Adding a tool meant editing the core. The user requires generic, pluggable integrations: a new tool is a package or a setting, never a code branch (AGENTS.md, 2026-10-23).

## Decision
| Topic | Decision |
|---|---|
| Provider | A tool package implementing `Provider` (ports/providers.py), found through the Python entry point group `sales_platform.providers`. Providers state their `purposes` and `capabilities`; the core decides by those, never by provider name (architecture test). Development-only providers are skipped in production |
| Connection | One business's use of a provider: a `connections` row with purposes, non-secret config, **secret references** (never values), provider-side account and status. Each API key is one `api` connection; the demo businesses have a `simulated` connection (manager phone and demo catalogue) |
| Secrets (D8) | A `SecretStore` port chosen by entry point (`sales_platform.secret_stores`): `local` (AES-256-GCM files, development) and `none` (production until hosting and its cloud store are chosen, D24) |
| Inbound | One route: `POST /api/v1/hooks/{connection_id}/{webhook_token}`. Constant-time token check; the provider verifies the raw bytes **before** parsing; the business always comes from the connection. Events are stored once (dedupe per connection, provider event ID and type) with a `process_inbound` job, routed by event type through the same approval core as the portal |
| Outbound | One outbox, `outbound_messages`, with fixed-wording templates, an idempotency key per message and the ADR 0015 statuses (+ `delivered`). `deliver_quotation`, `notify_approvers` and `notify_response` queue messages; `send_message` sends them. Send hooks create the single-use approval token only at send time (never stored) and attach the hash-checked PDF |
| Product data | Each business's active connection with purpose `catalogue` is its product source (at most one, a database rule). `TenantCatalogue` routes every ERP call to the caller's source |
| Deprecated alias | `POST /api/v1/channels/simulated/events` stays for the demo page and scripts, rebuilt on the framework with the same answers. It is development-only, still takes the business from the body (accepted weakness), and is removed in phase 4/5 |

### User decisions (2026-10-27, phase 1 open questions)
All recommendations were accepted (phase 1 plan §3 (git history)):
- **P1-Q1:** behaviour tests unchanged; unit tests of the replaced classes rewritten case by case.
- **P1-Q2:** the old route kept as an alias.
- **P1-Q3:** no product source puts the enquiry in review, with "No product source is connected".
- **P1-Q4:** ERP errors that can't succeed on retry stop the job at once.
- **P1-Q5:** accepting a quotation with no source still answers 503.
- **P1-Q6:** an approval request interrupted mid-send becomes `unknown` and is not resent.
- **P1-Q7:** one active product source per business.
- **P1-Q8:** administrators see all connections; approvers see only the ones they can link.
- **P1-Q9:** API key read scope unchanged until phase 2.
- **P1-Q10:** a disabled connection does nothing.
- **P1-Q11:** no secret store in production until D24.
- **P1-Q12:** webhooks answer 200.
- **P1-Q13:** the "Channels" page becomes "Connections".

## Implementation notes
- **Tables:** migration `0008_connections` adds `connections`, `connection_events` (append-only) and `connection_ref` on inbox events, bindings and enrollments, with a backfill. `0009_outbound_messages` adds the outbox and backfills one message per delivery. Both downgrade.
- **Event identity:** manager actions keep the provider key and the connection's legacy key in `ChannelEvent`. Decision command IDs are therefore the same before and after the upgrade, so a replayed event is still "replayed".
- **Intake:** API intake finds the caller's `api` connection. The envelope (schema 2) carries `connection_ref` and `reply_to`; the reply address is fixed at intake from the provider's delivery mode.
- **Worker grants:** the worker gained column-level grants to record approval decisions from webhooks.
- **Composition root:** `composition.py` wires the registry, secret store, product sources and every job handler. The API, the worker and the tests share it.

## Adding a provider
See [adding providers](../development/adding-providers.md): one package with a `build(environment)` factory, one entry point, a contract-test sample. No change to the API, worker, application services or portal.

## Consequences
- **Phase 5 (one-click Enable) only adds operations.** `provision`, `resume`, handshakes and platform webhooks get grants for creating connections; the core paths stay as they are.
- **One extra job step:** deliveries and notifications pass through `send_message`, which adds about one worker poll interval.
- **Production has no secret store yet (D24):** only providers without secrets (the API) work there until hosting is chosen.
