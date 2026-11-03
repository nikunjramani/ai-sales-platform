# Implementation plan, part 0: shared contracts
**Status:** planning document. Revision 2 (2026-10-26) merges the technical proposals from all phase plans; the [change log](#9-change-log) lists them. This file fixes the names, interfaces, tables and numbering every phase plan must use. The phase plans are [phase 1](phase-1-foundations.md), [2](phase-2-accounts.md), [3](phase-3-catalogue.md), [4](phase-4-conversations.md), [5](phase-5-one-click-providers.md) and [6-7](phase-6-7-pilot-source-and-orders.md).
- **Where it wins:** if a phase plan and this file disagree, **this file wins**.
- **Changing it:** a phase that needs a change makes it here and adds a change-log row.
- **Business rules:** this file contains technical contracts only. Points that need the user's decision are in §8 and in [../07-decisions-needed.md](../07-decisions-needed.md) (D1…D24).

## 1. Vocabulary in code
| Concept | Python name | Table | Replaces / notes |
|---|---|---|---|
| A kind of tool (adapter package) | `Provider` (protocol), `ProviderKey` (str) | — (registry in memory) | Today's `Channel` / `ApprovalChannel` enums |
| A business's configured tool | `Connection` | `connections` | Today's free-text `connection_id` strings |
| What a connection is used for | `Purpose` (`StrEnum`: `customer_messages`, `manager_approvals`, `catalogue`; later `orders`, `payments`, `shipments`) | `connections.purposes` (text[]) | — |
| Provider capability flags | `Capabilities` (frozen dataclass). It has messaging flags and optional sub-dataclasses: `CatalogueCapabilities` (`editable`, `live_prices`, `live_stock`, `stock_semantics`, `warehouses`, `webhooks`) and `OrderCapabilities` (`create_order`, `reference_lookup`, `idempotency_key`, `reserve_stock`) | — | The core decides by capability, never by provider name |
| Normalised inbound event | `InboundEvent`, a union of: `CustomerMessage`, `CustomerAction` (button press: sender, reference, action, reason), `ManagerAction` (enroll or decide), `ManagerReply` (free text to be classified, D10), `DeliveryStatusUpdate`, `ConsentChange`; later `OrderStatusChanged`, `PaymentEvent` | `inbox_events` (extended) | `InboundEnvelope` becomes the payload of `CustomerMessage`. `DeliveryStatusUpdate` avoids the clash with `domain/documents.py:DeliveryStatus` |
| Outgoing message | `OutboundMessage`, outbox row | `outbound_messages` | `DeliveryChannel.deliver` and `ApprovalNotifier.send_*` become uses of it |
| Message wording | `TemplateKey` (`StrEnum`) + `render_template()` | — (code, fixed wording) | `render_review_text`, `notice_text`; includes the `account_*` keys (phase 2) |
| Customer | `Contact`, `ContactIdentity` | `contacts`, `contact_identities` | `enquiries.customer_reference` (kept, mapped) |
| Customer browser session (website chat) | `CustomerSession` | `customer_sessions` | — |
| Thread | `Conversation`, `ConversationMessage` | `conversations`, `conversation_messages` | — |
| Interpreter input | `EnquiryTranscript` (ordered messages + open items) | — | `EnquiryInterpreter.extract(text, documents)` changes to take a transcript (phase 4, ADR 0012 update) |
| Product data source (read) | `ProductSource` (port name kept; docs say "catalogue source") | — | Hardcoded `SimulatedErp` |
| Order write port (optional) | `OrderWriter`: `reserve_stock`, `release_reservation`, `create_sales_order`, `get_order_by_external_reference`, `get_order_status` | — | Names from `.docs/architecture/integrations.md` |
| Our product tables | `LocalCatalogue` (adapter implementing `ProductSource`, provider key `local_catalogue`) | `catalogue_categories`, `catalogue_products`, `catalogue_prices`, `catalogue_stock`, `catalogue_changes` | — |
| Person who signs in | `User` (`subject` = `user:<uuid>` for sign-ups; legacy demo subjects kept) | `users` | Bare `subject` strings |
| Secret store | `SecretStore` (port), with a tenant namespace and a `platform(provider_key)` namespace for company-level secrets | `connections.secret_refs` (JSON of references only) | Global `QUOTATION_*` provider settings |

"Channel" is not used for new code. Existing columns named `channel` keep their name, but they now hold a `ProviderKey` (e.g. `api`, `simulated`, `telegram`).

## 2. Module layout (backend, `src/sales_platform/`)
This follows the existing layers enforced by `tests/unit/test_architecture.py`: domain, ports and application use the standard library only.

**`domain/`**
- `connections.py`: Purpose, Capabilities, ConnectionStatus.
- `messages.py`: OutboundMessage, MessageStatus, TemplateKey, `render_template`.
- `contacts.py`, `conversations.py`, `accounts.py`, `catalogue_admin.py`.
- `catalogue_search.py`: the shared tokeniser.
- `intake.py` gains the inbound event types.

**`ports/`**
- `providers.py`: Provider, ProviderFactory, ProviderEnvironment, ProvisionResult, PendingAction, InboundRequest.
- `connections.py`, `secrets.py`.
- `messages.py`: MessageOutbox, MessageSender, SendHook.
- `contacts.py`, `conversations.py`, `customer_sessions.py`, `customer_actions.py`.
- `accounts.py`: UserStore, PasswordHasher, InvitationStore, Mailer, MemberStore, BusinessStore, ApiKeyAdminStore, AccountEmailStore, RateLimiter.
- `catalogue_admin.py`: CatalogueStore, CatalogueReadStore.
- `erp.py` gains `OrderWriter`.

**`application/`**
- `providers.py`: ProviderRegistry.
- `connections.py`: enable, disable, test, resume.
- `inbound.py`: verify, dedupe, route.
- `messaging.py`: OutboundService and the `send_message` handler.
- `conversations.py`: ConversationService, IntentRouter.
- `customer_actions.py`, `customer_sessions.py`.
- `accounts.py`: SignupService, LoginService, InvitationService, BusinessService, MemberService, ApiKeyAdminService, AccountEmailHandler.
- `catalogue_admin.py`, `catalogue_sources.py`.
- `manager_replies.py`: AI classification (D10).

**`adapters/`**
- **`providers/`:** one sub-package per provider: `api/`, `simulated/`, `local_catalogue/`, `web_chat/`, later `sendgrid_email/`, `twilio_whatsapp/`, `twilio_sms/`, `telegram/`, `meta_instagram/`, `meta_messenger/`, ERP/shop packages.
- **`secrets/`:** loaded by entry points (group `sales_platform.secret_stores`): `local` (dev, encrypted with a key from `.env`) and `none`. The cloud store is written only after D24.
- **`accounts/`:** `password.py` (Argon2id), `rate_limit.py`, and mailers by entry points (group `sales_platform.mailers`): `console` (dev), later `sendgrid`.
- **`catalogue/`:** `local.py`, over `CatalogueReadStore`; no SQLAlchemy in adapters.
- **`documents/`:** gains `logo.py`.

**`persistence/`**
- `connections.py`, `messages.py`, `contacts.py`, `conversations.py`, `customer_sessions.py`, `customer_actions.py`, `accounts.py`, `catalogue.py`.

**`api/`**
- `hooks.py`: the generic inbound routes.
- `connections.py`, `accounts.py`, `business.py`, `catalogue.py`, `conversations.py`.
- `public_chat.py`: the website chat public API.
- `media.py`: short-lived public PDF links.

**New third-party packages** go in adapters only. Each is added to the architecture allow-list when introduced, and every version must be at least 14 days old:
- `argon2-cffi` (phase 2);
- Pillow as a direct dependency (phase 2, logos; already installed through ReportLab);
- `cryptography` (phase 1, already transitive);
- provider SDKs only inside their provider package (phase 5).

## 3. The Provider contract
```python
ProviderFactory = Callable[[ProviderEnvironment], Provider]  # entry point target; the environment
# carries generic dependencies (storage folder, engines/stores, clock), never provider-named settings


class Provider(Protocol):
    key: str  # unique: "telegram", "web_chat", "api", "local_catalogue" …
    display_name: str
    purposes: frozenset[Purpose]
    capabilities: Capabilities
    config_model: type  # Pydantic; the portal renders the business form from its JSON schema
    platform_config_model: (
        type | None
    )  # company-level settings (our Twilio master account, Meta app …)
    dev_only: bool  # the registry skips dev-only providers in production (e.g. simulated)

    # lifecycle
    def provision(self, conn, secrets) -> ProvisionResult: ...
    def resume(
        self, conn, secrets, action_id: str, submission: Mapping[str, object]
    ) -> ProvisionResult: ...
    def deprovision(self, conn, secrets) -> None: ...
    def test(self, conn, secrets) -> TestResult: ...
    # inbound
    def handshake(
        self, conn, secrets, request: InboundRequest
    ) -> HandshakeResponse | None: ...  # e.g. GET challenge
    def verify_inbound(
        self, conn, secrets, request: InboundRequest
    ) -> None: ...  # raises InboundRejected
    def verify_platform_inbound(
        self, secrets, request: InboundRequest
    ) -> None: ...  # platform route (one webhook per app)
    def routing_keys(self, request: InboundRequest) -> list[str]: ...  # after platform verification
    def parse_inbound(self, conn, request: InboundRequest) -> list[InboundEvent]: ...
    # outbound
    def send(
        self, conn, secrets, message: RenderedMessage
    ) -> SendReceipt: ...  # raises SendRejected / SendUncertain
    # data purposes (optional)
    def catalogue_reader(self, conn, secrets) -> ProductSource: ...
    def order_writer(self, conn, secrets) -> OrderWriter: ...
```
- **Unsupported operations** raise `OperationUnsupported`; this is explicit and tested.
- **`ProvisionResult`** is either `ready` (with public details such as a phone number or chat link) or `needs_action` with a `PendingAction`.
  - A `PendingAction` has `action_id`, `expires_at` and a kind: `open_url`, `provider_page` (HTML from the provider package, e.g. Meta's pop-up, served by our API), `show_steps` or `fill_form` (JSON schema).
  - `resume` receives the result.
- **`InboundRequest`:** raw body, headers, query and URL. For first-party providers (`web_chat`, `api`) it also carries an optional `session` filled in by our own routes.
- **Discovery:** Python entry points, group `sales_platform.providers`. `ProviderRegistry.load(environment)` runs at start-up in the API and the worker. The in-repo providers register the same way, in `pyproject.toml`.
- **Contract tests:** `tests/unit/providers/test_provider_contract.py` runs every registered provider through the same checks:
  - a forged signature is refused;
  - duplicate events are handled;
  - send outcomes map correctly;
  - unsupported operations are explicit;
  - parsing keeps tenants isolated.
  - Data providers also run the ERP contract tests.

## 4. Routes (generic, provider-independent)
| Route | Purpose |
|---|---|
| `POST\|GET /api/v1/hooks/{connection_id}/{webhook_token}` | Per-connection webhooks. The business comes from the connection |
| `POST\|GET /api/v1/hooks/p/{provider_key}` | Platform webhooks for providers that allow one URL per app or bot (Telegram, Meta, email inbound). Steps: `verify_platform_inbound`, then `routing_keys`, then the `connection_routes` lookup (security-definer function), then the connection |
| `POST /api/v1/tenants/{t}/connections/{id}/actions/{action_id}` | Return data from a pending action (`resume`) |
| `GET /api/v1/connections/{id}/actions/{action_id}/page` | Provider-hosted setup page |
| `GET /api/v1/media/{token}` | Short-lived, single-file public PDF links. Twilio and Meta fetch attachments by URL |
| `/api/v1/chat/...` and `/chat/{handle}` | Website chat public API and hosted chat page (phase 4). `/chat/widget.js` serves the widget |
| The old `/api/v1/channels/simulated/events` | Kept as a deprecated alias in phase 1, routed through the generic inbound service |

## 5. Tables and migration numbers
- **One Alembic migration per phase step,** continuing from `0007_acceptance`.
- **Every business table** has `tenant_id` with forced row-level security (`_TENANT_FILTER`).
- **Grants** are explicit per role, with column-level UPDATE grants.
- **Append-only tables** use the `reject_change()` trigger.
- **Cross-tenant lookups** (an API key, a public chat handle, a webhook route) use narrow lookup policies or `SECURITY DEFINER` functions, following the existing `api_key_lookup` pattern.

| Migration | Phase | Tables / changes |
|---|---|---|
| `0008_connections` | 1 | **New tables:** `connections` (`account_ref` unique per tenant and provider, nullable `webhook_token_sha256`, `public_handle` unique and nullable, `secret_refs`, `config`, `purposes`, `status`, `last_error`); `connection_events` (append-only audit).<br>**`inbox_events` gains:** `event_type`, `connection_ref` (composite FK), `processed_at`, `outcome`; the dedupe key becomes (tenant, connection_ref, provider_event_id, event_type).<br>**`channel_bindings` and `channel_enrollments` gain** `connection_ref`.<br>**Backfill:** one `api` connection per API-key subject; one `simulated` connection per distinct (tenant, connection_id) in bindings; the demo tenants get an enabled `catalogue` connection to the simulated ERP |
| `0009_outbound_messages` | 1 | `outbound_messages`:<br>• statuses pending, sending, available, sent, delivered, failed, unknown;<br>• idempotency key, template key, recipient, `connection_ref`;<br>• `provider_message_id` (indexed per connection), `action_ref_id`;<br>• links to quotation, task and enquiry.<br>`quotation_deliveries` stays, linked through `outbound_message_id` |
| `0010_accounts` | 2 | **Account tables:** `users`, `user_credentials`, `email_verifications`, `password_resets`, `invitations`, `admin_events` (append-only).<br>**`tenants` gains:** `status`, `country`, `time_zone`, `created_by`, `went_live_at`, `updated_at`, plus a status-transition trigger.<br>**Other changes:**<br>• new `tenant_profiles` table;<br>• nullable commercial columns plus a completeness check on `tenant_pricing_policies` ("set later");<br>• `api_keys.created_by`, `revoked_by` and runtime grants;<br>• per-subject `user_sessions` policies;<br>• FK `memberships.subject → users.subject`;<br>• `jobs.tenant_id` may be NULL only for `send_account_email`.<br>**Functions** (`SECURITY DEFINER`, owner): `create_business(...)` (checks `app.subject`, creates the default catalogue connection), `accept_invitation`, `change_member_roles`, `remove_member` |
| `0011_catalogue` | 3 | `catalogue_categories`, `catalogue_products` (with `source`, `external_id`, `synced_at` for later sync), `catalogue_prices` (history; `price_list`, `revision`, effective dates), `catalogue_stock` (as-of, source, unknown allowed), `catalogue_changes` (append-only audit).<br>Also: `pg_trgm` and full-text indexes; `create_business` updated; a `local_catalogue` connection backfilled for tenants without a catalogue connection |
| `0012_conversations` | 4 | **New tables:** `contacts`, `contact_identities` (verified flag, consent), `conversations`, `conversation_messages` (append-only), `customer_sessions`, `customer_action_refs`.<br>**`enquiries` gains:** `conversation_id`, `contact_id`, and a narrow `UPDATE (status, updated_at)` for `quotation_app` with the `guard_enquiry_reopen` trigger.<br>**Other changes:** `workflow_runs.round`; `acknowledge_enquiries` setting if D14 makes it per business |
| `0013_provider_state` | 5 | **As built (rev 6):** `connection_routes` (global unique (provider_key, routing_key), optional expiry; lookup **policy** `connection_route_lookup`, not a function); `approval_reply_prompts` (reason and confirm prompts; replaces the planned `approval_reason_prompts`); `manager_reply_classifications` (append-only); `media_links`; `outbound_messages.action_ref_id` and `resent_from_id`; `connections.pending_action`; event types `manager_reply`, `customer_action`, `connection_update`; connection event kind `resumed`; policy `approval_link_lookup`; grants as in the configuration reference |
| `0014_catalogue_sync` | 6 | **As built (rev 7):** `catalogue_sync_runs`; source `sync` on products, prices and stock; worker INSERT on the catalogue tables and column-level UPDATE of synced product columns; one waiting "Sync now" per business (`uq_jobs_manual_sync`; the API still cannot read jobs) |
| `0015_orders` | 7 | **As built (rev 7):** `orders`, `order_lines` (immutable), `order_events` (append-only), `order_operations`; `tenants.order_creation`; `uq_connections_one_orders`; event type `order_status_request`; admin action `order_settings_changed` |

## 6. Job kinds (added to `domain/jobs.py` `JobKind`)
| Kind | Phase | Purpose |
|---|---|---|
| `send_message` | 1 | Send one `outbound_messages` row through its provider |
| `provision_connection` | 1 (used from 5) | Run `provision` or `resume` in the background |
| `process_inbound` | 1 | Route one stored inbound event from a webhook provider. The `api` and `web_chat` routes call the same `ConversationService.route` synchronously, because the API must return `enquiry_id` and `continued` |
| `send_account_email` | 2 | Verification, invitation and password-reset emails (no tenant) |
| `poll_connection` | 5 | Providers that poll instead of using webhooks |
| `classify_manager_reply` | 5 | AI classification of a manager's free-text reply (D10), outside the webhook request |
| `sync_catalogue` | 6 | Copy a synced source's catalogue (repeats every `sync_minutes`; manual runs do not repeat) |
| `create_order`, `erp_order_operation`, `reconcile_order_operation`, `refresh_order_status`, `notify_order` | 7 | Create after acceptance (if automatic), write to the ERP, look up an unknown write, follow ERP statuses, tell the customer |

The existing kinds stay. `deliver_quotation`, `notify_approvers` and `notify_response` are re-implemented on top of `send_message` in phase 1, without changing their external behaviour.

## 7. Permissions and portal features
| Permission | Roles | Phase |
|---|---|---|
| `integrations:manage` | administrator | 1 (used in 5) |
| `members:manage` | administrator | 2 |
| `catalogue:view` | administrator, sales_operator, approver, viewer | 3 |
| `catalogue:manage` | administrator, sales_operator (**to confirm, §8**) | 3 |
| `conversations:view` | administrator, sales_operator, approver, viewer (viewer sees masked contact details, **to confirm, §8**) | 4 |
| `messages:resend` | administrator, sales_operator (user answer 2026-10-29) | 5 |
| `orders:view` | administrator, sales_operator, approver, viewer | 7 |
| `orders:manage` | administrator, sales_operator, approver (user answer Q2, 2026-10-29) | 7 |

The existing `tenant:manage` covers business settings.

| Portal feature | Phase | Screens |
|---|---|---|
| `connections` (replaces `channels`) | 1 / 5 | Connections list, my linked accounts; later Enable cards and pending-action panel |
| `account` | 2 | Sign up, verify email, log in, forgot/reset password, profile, accept invitation (the existing `auth` feature keeps session state and the route guard) |
| `business` | 2 | Create business, settings (business, pricing, validity, PDF details), members and invitations, API keys, setup checklist, Go live |
| `catalogue` | 3 | Product list with search, add/edit product, categories, prices, stock |
| `conversations` | 4 | Inbox, conversation detail, contacts |
| `orders` | 7 | Orders list (status and "needs attention" filters, creation setting), order detail (lines, ERP write, next steps, timeline), the order panel on quotations |
| Widget `portal/src/widget/` | 4 | Built by `portal/vite.widget.config.ts` to `dist/widget/widget.js`, served at `/chat/widget.js`; must not import portal features |

**Settings changes** (generic, never provider-named):
- `SALES_DEV_LOGIN` replaces `QUOTATION_AUTH_PROVIDER`.
- `SALES_MAILER` (an entry point name), `SALES_MAIL_FROM`, `SALES_PUBLIC_PORTAL_URL`, `SALES_TRUSTED_PROXY_COUNT`.
- `SALES_SECRET_STORE` (an entry point name).
- `SALES_DEFAULT_CATALOGUE_PROVIDER` (default `local_catalogue`).

## 8. Points for the user (raised by the phase plans, not decided here)
| # | Question | Proposal | Where |
|---|---|---|---|
| U1 | Who may edit products: administrators only (02 doc) or also sales operators (this file)? | **Decided (user, 2026-10-28):** administrators and sales operators | Phase 3 Q14 |
| U2 | Should viewers see customer names, emails and phone numbers in conversations? | **Decided (user, 2026-10-28):** masked for viewers; full for administrator, sales operator and approver | Phase 4 Q12 |
| U3 | Phase 1 replaces two internal classes, so their unit tests change. Is "all behaviour tests (API, PostgreSQL, evaluation, demo) unchanged" the right promise instead of "all tests unchanged"? | Yes | Phase 1 Q1 |

## 9. Change log
| Rev | Date | Change | From |
|---|---|---|---|
| 1 | 2026-10-23 | First version | — |
| 2 | 2026-10-26 | **Provider contract:** factory and environment, `dev_only`, `resume`, `handshake`, platform webhooks (`verify_platform_inbound`, `routing_keys`, `connection_routes`), `platform_config_model`, data-purpose methods `catalogue_reader` / `order_writer`, catalogue and order capabilities | Phase 1 C1-C3, C13; phase 3 C4-C5; phase 5 C1, C2, C5, C7; phase 6-7 P1-P2 |
| 2 | 2026-10-26 | **Events:** `CustomerAction`, `ManagerReply`, `DeliveryStatusUpdate` (renamed), later `OrderStatusChanged` / `PaymentEvent`; `InboundRequest.session` | Phase 1 C9; phase 4 C3-C4; phase 5 C4; phase 6-7 P7 |
| 2 | 2026-10-26 | **Outbox:** `available` status, `connection_ref`, `provider_message_id`, `action_ref_id`, `SendHook` | Phase 1 C4, C8, C11; phase 5 C3 |
| 2 | 2026-10-26 | **Migrations:** 0008 extra columns and backfill details; 0010 accounts additions and functions; 0011 `catalogue_changes`, sync columns, default catalogue connection; 0012 `customer_sessions`, `public_handle`, enquiry reopen grant; 0013 contents | Phase 1 C5-C7; phase 2 C1-C3; phase 3 C1, C6-C7; phase 4 C1-C2, C6-C7; phase 5 §5; phase 6-7 P3 |
| 2 | 2026-10-26 | **Modules, jobs, routes, settings:** account ports and services, catalogue read port and tokeniser, customer sessions and actions, media route, provider page route, `classify_manager_reply`, `sync_catalogue`, order jobs, generic settings list, secret stores and mailers by entry points | Phase 1 C10; phase 2 C4-C5, C8-C9; phase 3 C3; phase 4 C5, C8, C10-C11; phase 5 C6, C8-C9; phase 6-7 P4 |
| 2 | 2026-10-26 | **Permissions:** `catalogue:view`, order permissions (phase 7); U1/U2 raised for the user | Phase 3 C2; phase 4 C9; phase 6-7 P5 |
| 2 | 2026-10-26 | **ERP write names** aligned with `integrations.md`; `06-future-commerce.md` and `05-product-data.md` table names aligned with this file | Phase 6-7 P8; phase 3 contradictions |
| 3 | 2026-10-27 | **Phase 2 as built:** `0010_accounts` also has `account_notices` (emails without a link) and `rate_limit_counters` (user answer Q5: counters in PostgreSQL); no lockout columns (Q3); no "all-or-nothing" pricing check (currency or tax may be saved alone at creation, Q14; complete = all six set). `create_business` does **not** create a catalogue connection yet (no in-portal catalogue provider until phase 3; the default catalogue connection moves to 0011). Ports: `UserStore.reset_email`, `sign_up(notify_existing=callable)`, `MailerEnvironment`, `TenantStatusReader`; services also `ProfileService`, wrapper `PauseWhileSuspended`. API key reads are scoped to the submitting key (P1-Q9) | Phase 2 implementation |
| 4 | 2026-10-28 | **Phase 3 as built:** `create_business` gains `p_catalogue_provider` and `p_catalogue_name` (default NULL); the API passes `SALES_DEFAULT_CATALOGUE_PROVIDER`. **C7 was not quite true:** the 0008 backfill gives the simulated connection only manager approvals; the seed adds the catalogue purpose. So the seed now disables any other active catalogue source on the two ERP demo businesses, and a third dev-only business uses the local list. `ProviderEnvironment.catalogue_store` (each process's store) instead of a registry-level dependency hook. `InMemoryCatalogueStore` ships in `adapters/catalogue/memory.py` (tests and `evaluate_extraction.py --source local`). `CatalogueReadStore.search` returns the best matches already ranked. The catalogue summary also returns the business time zone (a scheduled price starts at midnight there) | Phase 3 implementation |
| 5 | 2026-10-28 | **Phase 4 as built:** permissions `contacts:details` (not viewers, Q12) and `contacts:merge` (administrators, sales operators, D13) besides `conversations:view`; capability `hosted_chat`; one `customer_action_refs` row per offered version and identity (button ID or secure-link token hash), not one per action; `connections.public_handle_changed_at`, `tenants.acknowledge_enquiries`; extra connection event kind `settings_changed` and admin actions `contacts_merged`, `customer_messages_changed`; the API role may update a connection's `config` (chat settings); `IntakeStore.record(..., router)` replaces a separate `ConversationStore`; `WorkflowStore.record_outcome` returns whether the outcome applies and gains `readings_today`; customer messages from webhook providers are routed in phase 5 | Phase 4 implementation |
| 6 | 2026-10-29 | **Phase 5 as built (framework + Telegram, user answers 2026-10-29):** `ProviderDefaults` (explicit unsupported operations); `Provider.cost`, `platform_setup`, `unrouted`, `fetch_file`, `enrollment_hint`; `deprovision` returns steps; capabilities `self_service`, `platform_webhooks`, `attachments_by_url`; `ProvisionResult` statuses ready / needs_action / working / failed with config, secrets, routes and `poll_after`; `PendingAction.once` (one-time values never stored); `RenderedMessage.purpose`, `link`, `attachment_url`; `SendReceipt.routing_keys`; `ManagerAction` kind `confirm`; `CustomerMessage.files`, `skipped`, `from_business`; `IntakeStore.record(hold_for_review)`; routes as a lookup policy; prompts in `approval_reply_prompts`; job kinds `poll_connection`, `classify_manager_reply` (`provision_connection` unused: Enable provisions in the request); permission `messages:resend`; settings `SALES_MEDIA_LINK_MINUTES` (no provider-named settings; company settings in the platform secret namespace); `httpx` only inside provider packages | Phase 5 implementation |
| 7 | 2026-10-29 | **Phases 6 and 7 as built (ERPNext; ADR 0023, 0024):** capabilities `catalogue: CatalogueCapabilities` and `orders: OrderCapabilities` (with `sync` and `order_status` added to the planned flags; `catalogue_editable` kept); purpose `orders`; `Provider.fetch_catalogue` (full feed) next to `catalogue_reader` and `order_writer`; `OrderWriter` = `create_sales_order`, `get_order_by_external_reference`, `get_order_status` (no reservation port: Q6 needs none); generic config key `sync_minutes`; `ConnectionChange.exclusive_purposes`, `restore_replaced`, `jobs` (one product source and one order destination per business); event `OrderStatusRequest`; templates `order_confirmed`, `order_shipped`, `order_delivered`, `order_cancelled`, `order_status`, `order_none`; migrations renumbered `0014_catalogue_sync` and `0015_orders` (§4) | Phase 6-7 implementation |
