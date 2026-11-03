# Implementation plan, phase 1: foundations (refactor, no new features)
**Status:** implemented on branch `feat/phase-1-foundations` (2026-10-27), recorded in [ADR 0018](../../../adr/0018-connector-framework.md); deviations from this plan are listed in the [handoff](../../../handoff.md). The original planning text follows. planning document (2026-10-23), for the user's review. Nothing here is built. It follows the binding names in [00-shared-contracts.md](00-shared-contracts.md); where this plan needs something the contracts do not have, it is listed in [Proposed changes to shared contracts](#proposed-changes-to-shared-contracts) and must be accepted there first.

Paths are relative to `src/sales_platform/` unless they start with `tests/`, `portal/`, `migrations/`, `scripts/`, `demo/` or `.docs/`. Line numbers are from commit `3b38741`.

## 1. Goal
Replace today's hardcoded "channel" plumbing with the generic connector framework, **without changing what users, API clients, the demo page or the scripts see**:
- tools are **providers** found through Python entry points;
- each business's tool is a **connection** row (with secrets kept in a `SecretStore`, never in the database);
- provider callbacks arrive on **one generic route**, `POST /api/v1/hooks/{connection_id}/{webhook_token}`;
- everything we send goes through **one outbox**, `outbound_messages`, with fixed-wording **templates**;
- product data comes from the business's **catalogue connection**, not from a hardcoded `SimulatedErp`.

Today's API intake + pull delivery become provider `api`; the simulated manager phone (and the demo catalogue) become provider `simulated`.

## 2. Scope
| In scope | Out of scope (later phase) |
|---|---|
| `Provider` protocol, `ProviderRegistry` (entry points), `Capabilities`, `Purpose` | Enable/Disable/Test buttons, `provision` runs, provider cards (phase 5) |
| `connections` + `connection_events` tables, backfill (migration 0008) | Self-service creation of connections in the portal (phase 5) |
| `SecretStore` port, local encrypted adapter, selection by setting, `none` adapter | A cloud secret manager adapter (needs D24) |
| Generic inbound route, `InboundService`, `process_inbound` job, dedupe with `event_type` | Real providers (Telegram, Twilio...), GET webhook handshakes (phase 5) |
| `outbound_messages` outbox, `send_message` job, `TemplateKey` + `render_template` (migration 0009) | Sending clarification questions to customers (phase 4, D22) |
| Re-implement `deliver_quotation`, `notify_approvers`, `notify_response` on the outbox | Resend buttons for `failed`/`unknown` messages (open decision) |
| Per-business catalogue source resolution; clear outcome when a business has none | Our catalogue tables / `LocalCatalogue` (phase 3) |
| Permission `integrations:manage` | Members, invitations, sign-up (phase 2) |
| Portal feature `connections` replacing `channels` (list + manager linking) | Conversations, contacts, consent (phase 4) |
| Keep the simulated webhook, demo page and scripts working | Removing the deprecated simulated route (phase 4 or 5, see §5.2) |

## 3. Decisions used and open questions
### Decisions this plan relies on
| # | Decision | Used for |
|---|---|---|
| D8 | Tool secrets live in a cloud secret manager; development uses a local stand-in behind the same port | `SecretStore` port, `local` adapter, selection by setting |
| D9 | No shared accounts; each business has its own connections | Tenant comes from the connection; one connection per API key |
| D24 (open) | Hosting / which cloud | No cloud adapter in phase 1; production uses `none` until D24 |
| AGENTS.md | Generic, pluggable integrations; remove replaced provider code | Entry points; no `if provider == ...` outside provider packages; old adapters deleted, not kept alongside |
| ADR 0015 | Honest outcomes; `unknown` never resent | Outbox statuses and `send_message` handler |
| ADR 0013 / 0007 | One approval core for every channel | `ChannelActionService` and `ApprovalService` stay the only decision path |
| ADR 0011 | Tenant from a verified credential, never from the body | Generic route; API key → connection |

### User answers (2026-10-27)
The user accepted the recommendation for every question below: P1-Q1 a (also contracts §8 U3), Q2 a, **Q3 b** (needs review + portal note), Q4 a, Q5 a, Q6 a, Q7 a, Q8 a, **Q9 a** (keep; decide with API key management in phase 2), Q10 a, Q11 a, Q12 a, Q13 yes. Work happens on branch `feat/phase-1-foundations`, with one commit per work item.

### Open questions (ask the user before starting; none are decided by this plan)
| # | Question | Options | Recommendation | Blocks |
|---|---|---|---|---|
| P1-Q1 | What does "all existing tests unchanged" (08-phases) mean for unit tests of classes this phase **replaces** (`DeliveryChannel`, `ApprovalNotifier`, the notifier/channel maps)? | a) behaviour tests (API, PostgreSQL, evaluation, demo) keep their assertions; unit tests of replaced classes are rewritten case by case, no case dropped; b) keep the old classes as wrappers so their unit tests do not change | a) — b) breaks the "remove replaced code" rule | WI-6..WI-9 |
| P1-Q2 | Old route `POST /api/v1/channels/simulated/events` | a) keep as a deprecated, development-only alias with today's synchronous answers (`enrolled`/`recorded`/`replayed`, 401/403/409) until phase 4/5; b) move the demo page and scripts to the generic route now (asynchronous: the answer is "received", the outcome is seen later) | a) — see §5.2 | WI-5 |
| P1-Q3 | A business with **no catalogue source**: what should people see? | a) the job stops at once with a clear logged reason (no retries); b) as a) plus the enquiry goes to `needs_review` with "No product source is connected" shown in the portal; c) refuse the enquiry at intake | b) | WI-10 |
| P1-Q4 | Should **every** non-retryable ERP error (`unauthenticated`, `forbidden`, `invalid_request`, `unsupported`, `not_found`) stop job retries at once, using the existing but unused `ErpError.retryable`? | a) yes, all of them; b) only "no catalogue source" | a) | WI-10 |
| P1-Q5 | API customer accepts a quotation but the business has no catalogue source | a) keep today's answer: 503 "Current prices could not be checked; retry"; b) a non-retry answer (409/422 "no product source") | a) for phase 1 (no external change) | WI-10 |
| P1-Q6 | Approval request messages are **at least once** today (a crash re-sends with a new token). Under the outbox rule a crash while sending becomes `unknown` and is not re-sent. Accept? | a) accept: the task still shows in the portal; a resend action comes later; b) allow automatic resend for approval requests only (token rotation makes old copies useless) | a), with b) as an open item for phase 5 | WI-8 |
| P1-Q7 | How many **active catalogue sources** may a business have? | a) exactly one (a database rule); b) several, with a priority | a) for now | WI-2, WI-10 |
| P1-Q8 | Who may see the **Connections list**? | a) `integrations:manage` (administrators) see everything; approvers see only connections they can link to; b) every member sees the list read-only | a) | WI-12, WI-13 |
| P1-Q9 | Existing behaviour found in review: any API key of a business can read **any** of that business's API enquiries and quotations (`offered()` and `get_enquiry` are not scoped to the submitting key) | a) keep in phase 1 (no external change) and decide in phase 2 with API key management; b) restrict to the submitting key now | a), but it is a security question for the user | — |
| P1-Q10 | A **disabled** connection (e.g. a backfilled revoked API key) | a) no sends, inbound refused (404), API intake for that key refused (403), its bindings get no approval requests; b) something else | a) | WI-2, WI-4, WI-8 |
| P1-Q11 | Production has no secret store until D24. Accept that production can only use connections without secrets (`api`) until then? | a) yes; b) choose the cloud now | a) | WI-3 |
| P1-Q12 | Generic hook answer code | a) 200 with counts (most providers want 2xx quickly); b) 202 | a) | WI-4 |
| P1-Q13 | Portal: rename the "Channels" page to "Connections" at `/connections`, with `/channels` redirecting | yes / keep old name | yes | WI-13 |

## 4. Current-state findings (what must change)
### 4.1 Hardcoded provider names and enums
| Where | What | Why it matters |
|---|---|---|
| `domain/intake.py:23-24` | `class Channel(StrEnum): API = "api"` | Any other provider needs code edits; `InboundEnvelope.channel` (`:112`) is typed by it |
| `ports/intake.py:9,55,66,117` | `EnquiryView.channel`, `find_receipt(... channel: Channel ...)`, `EnquirySummary.channel` use the enum | **Hidden coupling:** `persistence/intake.py:195` and `:331` do `Channel(enquiry.channel)`; an enquiry from any other provider makes the status API and portal list fail with `ValueError` (HTTP 500) |
| `domain/quotations.py:69-73` | `ApprovalChannel.SIMULATED` | `api/portal.py:190-198` validates enrollment against it (422 for anything else); `api/channels.py:87` and `adapters/channels/simulated.py:42` use it |
| `domain/documents.py:24,95-107` | `delivery_recipient` has `if channel == Channel.API.value: return connection_id` | A provider-named branch in the domain. Pull vs push must come from provider capabilities |
| `adapters/delivery/api.py:13-17` | `ApiPullDelivery.channel = Channel.API.value` | Becomes provider `api` |
| `api/enquiries.py:54,222,365-366,392,417` | `Channel.API.value` passed to `offered`, `offer`, `respond`, `download` | The `api` provider's own route may name its provider key, through a constant from its package |
| `application/intake.py:60,65-66` | `channel: Channel = Channel.API`; `connection_id = context.subject` | Connection must be a real row resolved from the API key |
| `worker.py:73-78,81-86` | notifier map `{"simulated": ...}` only when the simulated secret is set; delivery map `{api: ApiPullDelivery()}` | Adding a tool means editing the worker |
| `config.py:40-41,45-51` | provider-named setting `simulated_channel_secret`; production check names it | Against "no provider-named settings"; kept only for the deprecated route (§5.2) |
| `portal/src/features/channels/ui/components/EnrollmentCard.tsx:18` | sends the literal `"simulated"` | Portal must list linkable connections instead |

### 4.2 Security and correctness findings
| Where | Finding | Phase 1 fix |
|---|---|---|
| `api/channels.py:42-52,85-89` | The simulated webhook takes `tenant_id` and `connection_id` **from the body**; one global secret signs every tenant's events | Generic route takes the tenant from the connection found by URL and token. The old route stays only as a dev alias (§5.2) |
| `adapters/erp/simulated.py:76-78` + `application/jobs.py:59-71` | A tenant without a demo catalogue gets `ErpError(UNSUPPORTED)`; the job runner retries every non-`PermanentJobError` until dead (5 attempts, minutes of backoff) | Resolver raises `CatalogueSourceMissing`; job stops at once (P1-Q3/Q4) |
| `ports/erp.py:31-42` | `ErpError.retryable` exists but nothing reads it | Read it in `JobRunner` (P1-Q4) |
| `persistence/documents.py:427-462`, `persistence/acceptance.py:150-157` | `offered()` / `offer_for_enquiry()` filter by tenant, enquiry and **channel**, not by recipient | Kept as is (P1-Q9); recorded in security.md |
| `persistence/channels.py:106-122` | `approver_bindings` ignores whether the binding's connection is usable | Add "connection status = active" to the query (P1-Q10) |
| `application/notifications.py:58-66` | The live action token exists only in memory and in the sent message | The outbox must **never store the token**; it is created at send time by a send hook (§5.3) |
| `ports/documents.py:110-125` | `set_status` reaching `available`/`sent` also queues `expire_quotation` | **Hidden coupling:** the outbox status update must keep queuing expiry when the message belongs to a delivery |
| `application/channel_actions.py:124-135` | `command_id` and `evidence_ref` are built from `channel:connection_id:event_id` | With `connection_ref` (UUID) a replay of an event sent **before** the upgrade gets 409 instead of "replayed". Accepted edge case; documented |
| `persistence/documents.py:167-223` | `queue_delivery` reads `InboxEvent.connection_id` and `sender_external_id` to compute the recipient | Recipient becomes the envelope's `reply_to`, computed at intake from capabilities (backfilled) |

### 4.3 Other couplings found
- `api/app.py:91-93` and `worker.py:68-72,97` build one `SimulatedErp(demo_catalogues(INDUSTRIAL_TENANT, OFFICE_TENANT))` shared by pricing (`application/pricing.py:23-31`), the enquiry workflow (`workflows/enquiry.py:174,218`) and acceptance (`application/acceptance.py:219`). Keeping `QuotationPricingService(erp: ErpReader, ...)` unchanged and passing a **tenant-routing `ErpReader`** avoids touching those services and their tests.
- `scripts/simulated_channel.py:22-26,47,100-105` and `demo/server.py:114-127,143-149` sign events with the global secret, post to the old route and expect `200` with `result`. The demo also posts `{"channel": "simulated"}` to `/channel-enrollments` (`demo/server.py:144`).
- `tests/integration/workflows/test_quotation.py:120-170` wires handlers by hand (`{"simulated": SimulatedChannelNotifier(...)}`, `{"api": ApiPullDelivery()}`); `:309-341` uses the old route. A shared composition function (WI-11) stops future refactors from touching test wiring.
- `tests/integration/conftest.py:27-33` truncates intake tables; it must add `outbound_messages` and must not truncate `connections` (seeded) or `connection_events`.
- `scripts/create_api_key.py:34-37`, `demo/server.py:88-100` and `tests/integration/conftest.py:100-109` all create keys through `persistence/api_keys.py:44-57 insert_api_key`. Creating the `api` connection **inside** `insert_api_key` keeps all three working unchanged.
- `adapters/channels/simulated.py:54-77`: the outbox JSON shape (`tenant_id`, `connection_id`, `to`, `task_id`, `quotation`, `version`, `text`, `actions`, `action_reference`, `expires_at`; notices: `notice_id`) and file names (`<task_id>.json`, `notice-<id>.json` under `<tenant>/<external user>/`) are read by the demo and the script. They must stay byte-compatible.
- `migrations/versions/0003_intake.py:157-160`: the `api_key_lookup` policy pattern (reveal only the row whose ID the caller set) is reused for the hook lookup.
- Worker grants (`0005_approvals.py:96-107`): the worker cannot record approval decisions today. Routing manager actions through the `process_inbound` **job** needs new worker grants (WI-2).

## 5. Design summary
### 5.1 Main types (new modules follow 00 §2)
```python
# domain/connections.py (stdlib only)
class Purpose(StrEnum):
    CUSTOMER_MESSAGES = "customer_messages"
    MANAGER_APPROVALS = "manager_approvals"
    CATALOGUE = "catalogue"


class ConnectionStatus(StrEnum):
    SETTING_UP = "setting_up"
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"


class DeliveryMode(StrEnum):
    PUSH = "push"
    PULL = "pull"


@dataclass(frozen=True)
class Capabilities:
    delivery_mode: DeliveryMode = DeliveryMode.PUSH
    webhooks: bool = False  # receives callbacks on the generic route
    text: bool = True
    attachments: bool = False
    max_attachment_bytes: int | None = None
    buttons: bool = False
    threads: bool = False
    outbound_initiation: bool = True  # may message someone first
    reply_window: timedelta | None = None
    templates_required: bool = False
    delivery_receipts: bool = False
    manager_enrollment: str = "code_message"  # "code_message" | "oauth" | "email_link" | "none"
    max_inbound_bytes: int = 64 * 1024


@dataclass(frozen=True)
class Connection:
    connection_id: UUID
    tenant_id: UUID
    provider_key: str
    display_name: str
    purposes: frozenset[Purpose]
    config: Mapping[str, object]
    secret_refs: Mapping[str, str]
    account_ref: str | None
    status: ConnectionStatus
    last_error: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime

    @property
    def legacy_key(self) -> str:
        return self.account_ref or str(self.connection_id)


def validate_display_name(name: str) -> str: ...  # 1-100 printable chars
def validate_purposes(requested, offered) -> frozenset[Purpose]: ...  # non-empty subset
```
```python
# domain/intake.py (added; InboundEnvelope gains reply_to, schema_version 2)
class InboundEventType(StrEnum): CUSTOMER_MESSAGE = "customer_message"; MANAGER_ACTION = "manager_action"; DELIVERY_STATUS = "delivery_status"; CONSENT_CHANGE = "consent_change"
@dataclass(frozen=True) class CustomerMessage: provider_event_id: str; sender_external_id: str; text: str; occurred_at: datetime | None; thread_id: str | None = None; reply_to_external_id: str | None = None
@dataclass(frozen=True) class ManagerAction: provider_event_id: str; external_user_id: str; kind: str ("enroll"|"decision"); code: str | None; reference: str | None; action: str | None; reason: str | None
@dataclass(frozen=True) class DeliveryStatusUpdate: provider_event_id: str; provider_message_id: str; status: str ("delivered"|"failed"); detail: str | None
@dataclass(frozen=True) class ConsentChange: provider_event_id: str; sender_external_id: str; opted_in: bool
type InboundEvent = CustomerMessage | ManagerAction | DeliveryStatusUpdate | ConsentChange
def event_type(event: InboundEvent) -> InboundEventType: ...
```
`DeliveryStatusUpdate` avoids clashing with the existing `domain.documents.DeliveryStatus` enum (contract name `DeliveryStatus`; see proposed changes).

```python
# ports/providers.py (stdlib only)
@dataclass(frozen=True) class InboundRequest: headers: Mapping[str, str]  # lower-case names
                                               body: bytes; query: Mapping[str, str]; received_at: datetime
@dataclass(frozen=True) class RenderedMessage: message_id: UUID; idempotency_key: str; recipient: str
    text: str; metadata: Mapping[str, str]        # e.g. task_id, quotation, version, notice_id
    actions: tuple[str, ...] = (); action_reference: str | None = None; expires_at: datetime | None = None
    attachment: Attachment | None = None          # filename, content_type, data, sha256
class ReceiptKind(StrEnum): SENT = "sent"; AVAILABLE = "available"
@dataclass(frozen=True) class SendReceipt: kind: ReceiptKind; provider_message_id: str | None = None
class InboundRejected(Exception): ...   # bad signature, stale timestamp
class SendRejected(Exception): ...      # permanent refusal -> failed
class SendUncertain(Exception): ...     # may have been sent -> unknown, never resent
class OperationUnsupported(Exception): ...
@dataclass(frozen=True) class ProviderEnvironment: environment: str; storage_dir: Path; options: Mapping[str, object]
class Provider(Protocol):          # 00 §3, plus the proposed additions marked (+)
    key: str; display_name: str; purposes: frozenset[Purpose]; capabilities: Capabilities
    config_model: type; dev_only: bool                                    # (+) dev_only
    def provision(self, conn, secrets) -> ProvisionResult: ...           # phase 1: api/simulated raise OperationUnsupported
    def deprovision(self, conn, secrets) -> None: ...
    def test(self, conn, secrets) -> TestResult: ...
    def verify_inbound(self, conn, secrets, request: InboundRequest) -> None: ...
    def parse_inbound(self, conn, request: InboundRequest) -> list[InboundEvent]: ...
    def send(self, conn, secrets, message: RenderedMessage) -> SendReceipt: ...
    def catalogue_reader(self, conn, secrets) -> ErpReader: ...           # (+) only for Purpose.CATALOGUE
type ProviderFactory = Callable[[ProviderEnvironment], Provider]          # (+) what an entry point points to
```
```python
# ports/secrets.py
class SecretNotFound(Exception): ...; class SecretStoreUnavailable(Exception): ...
class SecretStore(Protocol):
    def put(self, tenant_id: UUID, name: str, value: str) -> str: ...   # returns an opaque reference "local:<uuid>"
    def get(self, tenant_id: UUID, reference: str) -> str: ...          # SecretNotFound if missing or other tenant
    def delete(self, tenant_id: UUID, reference: str) -> None: ...      # idempotent
# ports/connections.py
class ConnectionStore(Protocol):
    def get(self, tenant_id: UUID, connection_id: UUID) -> Connection | None: ...
    def find_by_account(self, tenant_id: UUID, provider_key: str, account_ref: str) -> Connection | None: ...
    def list(self, tenant_id: UUID, purpose: Purpose | None = None) -> list[Connection]: ...
    def catalogue_source(self, tenant_id: UUID) -> list[Connection]: ...        # active, purpose catalogue
class HookLookup(Protocol):
    def find_for_hook(self, connection_id: UUID) -> tuple[Connection, str | None] | None: ...  # with webhook_token_sha256
# ports/messages.py
class MessageStatus  (domain/messages.py): pending, sending, available, sent, delivered, failed, unknown
class MessageOutbox(Protocol):
    def get(self, tenant_id: UUID, message_id: UUID) -> OutboundMessage | None: ...
    def set_status(self, tenant_id, message_id, status, *, expected: frozenset[MessageStatus], provider_message_id=None, error=None) -> bool: ...  # compare-and-set; mirrors a linked delivery and queues expiry
    def apply_receipt(self, tenant_id, connection_id, provider_message_id, status, detail) -> bool: ...
class SendHook(Protocol):                                                    # (+) see §5.3
    def before_send(self, message: OutboundMessage) -> SendPreparation: ...  # extra fields or cancel(reason)
    def after_send(self, message: OutboundMessage, receipt: SendReceipt) -> None: ...
```
```python
# domain/messages.py
class TemplateKey(StrEnum): APPROVAL_REQUEST = "approval_request"; RESPONSE_NOTICE = "response_notice"; QUOTATION_DOCUMENT = "quotation_document"
@dataclass(frozen=True) class OutboundMessage: message_id; tenant_id; connection_id; provider_key; purpose: Purpose
    template_key: TemplateKey; params: Mapping[str, str]; recipient: str; idempotency_key: str
    body_text: str; metadata: Mapping[str, str]; status: MessageStatus; document_id: UUID | None
    quotation_id; version; approval_task_id; binding_id; enquiry_id; correlation_id; provider_message_id; last_error; created_at; updated_at
def render_template(key: TemplateKey, params: Mapping[str, object]) -> tuple[str, Mapping[str, str]]: ...  # (text, metadata); fixed wording
```
`render_review_text` (`domain/quotations.py:253-279`) and `notice_text` (`application/acceptance.py:266-279`) move into `render_template` **word for word** and are deleted (golden tests first, WI-0).

### 5.2 The old simulated route: kept as a deprecated alias (P1-Q2)
Recommendation: keep `POST /api/v1/channels/simulated/events` in phase 1, re-implemented on the new framework, and remove it when the demo moves to a real provider (phase 4/5). Reasons:
- The demo page (`demo/server.py:114-127`), the script and the PostgreSQL test (`test_quotation.py:309-341`) depend on its **synchronous** answers (`enrolled`, `recorded`, `replayed`, 403 forwarded, 409). The generic route is asynchronous by contract (`process_inbound` job), so it can only answer "received".
- It is development-only (refused in production, `config.py:45-51`) and already signed with a secret.

How it works after phase 1:
1. Verify the global `SALES_SIMULATED_CHANNEL_SECRET` HMAC on the raw body (as today, `api/channels.py:76`).
2. Parse the old body shape. Use `tenant_id` + `connection_id` only to **find** the tenant's `simulated` connection by `account_ref` (`find_by_account`). No connection, or not active → 403 "Action not accepted".
3. Give the parsed `ManagerAction` to `InboundService.process_now(connection, events)`: it records the event in `inbox_events` (dedupe) and routes it **in the request**, returning the `ChannelActionService` result. A duplicate is routed again (the services are idempotent), so a repeat still answers `replayed`.
4. Same answers and status codes as today.

Known remaining weakness (accepted, dev-only): the alias still lets the body choose among tenants that share the dev secret. The generic route does not have this weakness. Both are listed in `.docs/operations/security.md`.

### 5.3 Approval tokens never stored: send hooks
The outbox stores the rendered text and metadata, **not** the single-use action token. At send time `SendMessageHandler` calls the `SendHook` registered for the template:
- `ApprovalRequestHook.before_send`: re-reads the review packet (`NotificationStore.review_packet`); if the task is no longer pending → cancel (message `failed`, reason "The approval task was already decided"); else creates the token, calls `prepare_reference(...)` (existing rotation rule, `persistence/channels.py:124-169`) and returns `action_reference` + `expires_at`; `None` from `prepare_reference` means already sent → message `sent`.
- `ApprovalRequestHook.after_send`: `mark_sent(reference_id)`.
- `QuotationDocumentHook.before_send`: loads and hash-checks the document bytes (`read_verified`).
Hooks are application code keyed by `TemplateKey`, not by provider, so they stay generic.

## 6. Work items (in order; each is one reviewable pull request unless noted)
Size: S ≤ 1 day, M 2-3 days, L 4-5 days.

### WI-0. Pin today's behaviour (tests only) — S
Before any refactor, add tests that must still pass at the end:
- `tests/unit/domain/test_quotations.py`: golden string for `render_review_text` on a fixed packet (incl. the stock warning and "not given" reference).
- `tests/unit/application/test_acceptance.py`: golden strings for `notice_text` (accepted, declined with reason).
- `tests/unit/adapters/channels/test_simulated.py` (new): exact JSON keys and file names written by `SimulatedChannelNotifier.send_review`/`send_notice`.
- `tests/unit/api/test_portal.py`: response bodies of the old route for enroll, record, replay, forwarded (403), bad signature (401), disabled (404).
- `tests/unit/api/test_enquiries.py`: the JSON `channel` value `"api"` in status and list responses.
These are moved, not deleted, when the code they pin moves.

### WI-1. Domain and ports for providers, connections, secrets, messages — M
Add (stdlib only): `domain/connections.py`, `domain/messages.py` (`MessageStatus`, `TemplateKey`, `OutboundMessage`, `render_template`), `ports/providers.py`, `ports/connections.py`, `ports/secrets.py`, `ports/messages.py`; inbound event types in `domain/intake.py`.
Change:
| File | Change |
|---|---|
| `domain/intake.py:23-24` | Delete `Channel`. `InboundEnvelope.channel: str` (a `ProviderKey`), `connection_id: UUID` (the connection row), new `reply_to: str`, `ENVELOPE_SCHEMA_VERSION = 2`; `to_json` writes `connection_ref` and keeps `connection_id` = `legacy_key` for old readers |
| `ports/intake.py:9,55,66,117` | `channel: str`; `find_receipt(tenant_id, connection_id: UUID, provider_event_id, event_type)` |
| `domain/documents.py:24,95-107` | `delivery_recipient(mode: DeliveryMode, connection_key: str, sender_external_id: str \| None) -> str` (pull → connection key, push → verified sender, else `NoDeliveryAddress`). Used **at intake** to fill `reply_to` |
| `domain/quotations.py:69-76` | Delete `ApprovalChannel`; keep `PORTAL_TRANSPORT` |
| `domain/quotations.py:253-279` | Move `render_review_text` into `render_template(APPROVAL_REQUEST, {"packet": ...})` |
| `domain/jobs.py:19-27` | Add `SEND_MESSAGE = "send_message"`, `PROCESS_INBOUND = "process_inbound"`, `PROVISION_CONNECTION = "provision_connection"` (no handler until phase 5; the runner marks it dead with "No handler") |
| `domain/tenancy.py:25-34,39-47` | `Permission.MANAGE_INTEGRATIONS = "integrations:manage"`, granted to `Role.ADMINISTRATOR` only |
Tests: `tests/unit/domain/test_connections.py`, `test_messages.py` (golden wording from WI-0), `test_intake.py` (event types, envelope v2), `test_documents.py` (new `delivery_recipient`), `test_tenancy.py` (new permission).

### WI-2. Migration `0008_connections` + persistence — L
**Table `connections`** (forced RLS with `_TENANT_FILTER`, policy `tenant_isolation`):
| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | |
| `tenant_id` | uuid NOT NULL FK tenants | `UNIQUE (tenant_id, id)` for composite FKs |
| `provider_key` | varchar(32) NOT NULL | `CHECK (provider_key ~ '^[a-z][a-z0-9_]{1,31}$')` |
| `display_name` | varchar(100) NOT NULL | |
| `purposes` | text[] NOT NULL | `CHECK (cardinality(purposes) >= 1 AND purposes <@ ARRAY['customer_messages','manager_approvals','catalogue','payments','orders'])` |
| `config` | jsonb NOT NULL DEFAULT '{}' | non-secret settings, validated by the provider's `config_model` |
| `secret_refs` | jsonb NOT NULL DEFAULT '{}' | references only, e.g. `{"signing_secret": "local:..."}` |
| `account_ref` | varchar(200) NULL | provider-side identity (API key subject, legacy `connection_id`, later a phone number); `UNIQUE (tenant_id, provider_key, account_ref)` |
| `webhook_token_sha256` | char(64) NULL | NULL = no inbound webhook (e.g. `api`) |
| `status` | varchar(16) NOT NULL | `CHECK (status IN ('setting_up','active','error','disabled'))` |
| `last_error` | varchar(300) NULL | |
| `created_by` | varchar(200) NOT NULL | subject, `migration:0008` or `seed` |
| `created_at`, `updated_at` | timestamptz NOT NULL DEFAULT now() | |
Indexes: `ix_connections_tenant_status (tenant_id, status)`; `uq_connections_one_catalogue UNIQUE (tenant_id) WHERE status = 'active' AND purposes @> ARRAY['catalogue']` (P1-Q7).

**Table `connection_events`** (append-only, `reject_change()` trigger, forced RLS): `id`, `tenant_id`, `connection_id` (FK `(tenant_id, connection_id)`), `kind varchar(32) CHECK IN ('created','migrated','status_changed','tested','provisioned','deprovisioned')`, `actor varchar(200)`, `detail jsonb` (no secrets), `created_at`. Index `(tenant_id, connection_id, created_at)`.

**`inbox_events`:** add `event_type varchar(32) NOT NULL DEFAULT 'customer_message'` (`CHECK IN ('customer_message','manager_action','delivery_status','consent_change')`), `connection_ref uuid` (backfilled, then NOT NULL, FK `(tenant_id, connection_ref) → connections(tenant_id, id)`), `processed_at timestamptz NULL`, `outcome varchar(300) NULL`. Drop `uq_inbox_events_provider_event`; create `uq_inbox_events_provider_event UNIQUE (tenant_id, connection_ref, provider_event_id, event_type)`. `channel` and `connection_id` stay (deprecated, still filled).

**`channel_bindings`:** add `connection_ref uuid` (backfilled, NOT NULL, composite FK). Replace index `uq_channel_bindings_active_account` with `(tenant_id, connection_ref, external_user_id) WHERE revoked_at IS NULL`. `channel`/`connection_id` stay filled (`connection_id` = `legacy_key`) for display compatibility.

**`channel_enrollments`:** add `connection_ref uuid NULL` (composite FK). NULL = old endpoint (any connection of that provider); set = the code only works on that connection.

**Policies and grants:**
- `connection_hook_lookup ON connections FOR SELECT TO quotation_app USING (id = NULLIF(current_setting('app.connection_id', true), '')::uuid)` (same pattern as `api_key_lookup`, `0003_intake.py:157-160`).
- `quotation_app`: `SELECT` on `connections`, `connection_events`; `UPDATE (processed_at, outcome)` on `inbox_events` (sync alias path).
- `quotation_worker`: `SELECT` on `connections`, `connection_events`; `UPDATE (processed_at, outcome)` on `inbox_events`; `INSERT` on `inbox_events`, `enquiries` (customer messages routed by `process_inbound`); and for manager actions in the worker: `INSERT` on `approval_decisions`, `channel_bindings`; `UPDATE (status, decided_at)` on `approval_tasks`; `SELECT, UPDATE (consumed_at)` on `channel_enrollments`; `UPDATE (consumed_at)` on `approval_action_refs`.
- No role may `INSERT`/`UPDATE` `connections` except the owner in phase 1 (`insert_api_key`, seed). Phase 5 adds the API grants with Enable.

**Backfill (inside the migration; FORCE RLS is lifted for the owner within the transaction, as `0004_workflow.py:141-147` does):**
1. One `api` connection per `api_keys` row: `display_name = api_keys.name`, `purposes = {customer_messages}`, `account_ref = 'api-key:' || id`, `status = CASE WHEN revoked_at IS NULL THEN 'active' ELSE 'disabled' END`, `created_by = 'migration:0008'`.
2. One connection per distinct `(tenant_id, channel, connection_id)` in `channel_bindings` not yet covered: `provider_key = channel` (`simulated`), `purposes = {manager_approvals}`, `account_ref = connection_id` (normally `simulated-local`), `status = 'active'`, no webhook token.
3. One `disabled` connection per distinct `(tenant_id, channel, connection_id)` in `inbox_events` still uncovered (safety net; should be none).
4. A `migrated` row in `connection_events` for each.
5. Fill `inbox_events.connection_ref` and `channel_bindings.connection_ref` by joining on `(tenant_id, provider_key = channel, account_ref = connection_id)`; then `SET NOT NULL`.
6. `inbox_events.envelope = envelope || jsonb_build_object('reply_to', CASE WHEN channel = 'api' THEN connection_id ELSE envelope->>'sender_external_id' END)` (the only place the word `api` appears, frozen in a migration).

**Persistence:** `persistence/models.py` (new `ConnectionRow`, `ConnectionEvent`; columns above on `InboxEvent` `:125-148`, `ChannelEnrollment` `:419-434`, `ChannelBindingRow` `:437-462`); new `persistence/connections.py` with `SqlConnectionStore` and `SqlHookLookup` (sets `app.connection_id`, like `persistence/api_keys.py:19-24`).
`persistence/api_keys.py:44-57 insert_api_key` also inserts the `api` connection and a `created` event in the same transaction; `revoke_api_key` sets the connection `disabled` with a `status_changed` event.
`persistence/seed.py:49-84` ensures, for each demo tenant, one `simulated` connection with `account_ref = 'simulated-local'`, `purposes = {manager_approvals, catalogue}`, `config = {"catalogue": "industrial" | "office"}`, and (only when `SALES_SIMULATED_CHANNEL_SECRET` is set) a webhook token and signing secret (WI-5). Upsert on `(tenant_id, provider_key, account_ref)` merging purposes.
`persistence/channels.py:106-122` adds `JOIN connections ... status = 'active'`; `enroll` (`:201-276`) matches on `connection_ref` and honours `channel_enrollments.connection_ref`.
`persistence/intake.py:45-46,65-69,83-101` use `connection_ref` + `event_type`; `:195,:331` stop parsing the enum.

**Downgrade:** delete `inbox_events` rows with `event_type <> 'customer_message'` (they have no enquiries); delete jobs of kind `process_inbound`, `provision_connection`; restore the old unique constraint and index; drop the new columns, policies, grants, `connection_events`, `connections`; remove `reply_to` from envelopes (`envelope - 'reply_to'`).

Tests: `tests/integration/persistence/test_connections.py` (RLS isolation between the two demo tenants; hook lookup reveals only the requested ID and nothing without it; append-only events; one-catalogue index; `insert_api_key` creates the connection), `test_migrations_0008.py` (upgrade with M6 data → backfill correct; downgrade → upgrade round trip).

### WI-3. SecretStore adapters and selection — S
- `adapters/secrets/local.py` `LocalSecretStore(root: Path, key: bytes)`: one file per secret at `<storage_dir>/secrets/<tenant_id>/<uuid>.bin`, AES-256-GCM (`cryptography` AEAD) with associated data `tenant_id + reference`, so a file copied to another tenant or renamed does not decrypt. Reference format `local:<uuid>`. Writes are atomic (temp file + replace, as `adapters/channels/simulated.py:84-91`).
- `adapters/secrets/none.py` `NoSecretStore`: every call raises `SecretStoreUnavailable("No secret store is configured (see D24)")`.
- Selection by entry point group `sales_platform.secret_stores` (`local`, `none`); a cloud adapter later is one package + one entry point (no code change). Resolved in the composition root (WI-11).
- `pyproject.toml`: add `cryptography>=50,<51` to `dependencies` (already locked at 50.0.2 as a transitive dependency); architecture test allow-list for `adapters` gains `cryptography`.
- Settings (`config.py`):
| Setting | Type / default | Rule |
|---|---|---|
| `SALES_SECRET_STORE` | str, default `local` in development/test, `none` in production | Production refuses `local` |
| `SALES_SECRET_STORE_KEY` | SecretStr, optional | Required when the store is `local`: 32 bytes, url-safe base64. `scripts/init_local_env.py --add-missing` generates it |
| `SALES_SECRET_STORE_OPTIONS` | JSON dict, default `{}` | Passed to the adapter as is (for cloud adapters later) |
| `SALES_PUBLIC_BASE_URL` | URL, optional | Only used to show hook URLs in logs/portal; D24 tunnel in development |
- Never log secret values or references with values; `repr` hides them.
Tests: `tests/unit/adapters/secrets/test_local.py` (round trip; wrong tenant → `SecretNotFound`; tampered file; wrong key; delete idempotent), `tests/unit/test_config.py` (production refuses `local`; key required for `local`).

### WI-4. Provider registry, generic hook route, InboundService, `process_inbound` — L
- `application/providers.py`:
```python
class ProviderRegistry:
    @classmethod
    def load(
        cls, environment: ProviderEnvironment, entry_points: Iterable[EntryPointLike] | None = None
    ) -> "ProviderRegistry": ...
    def get(self, key: str) -> Provider: ...  # UnknownProvider
    def all(self) -> list[Provider]: ...
    def for_purpose(self, purpose: Purpose) -> list[Provider]: ...
```
  Reads `importlib.metadata.entry_points(group="sales_platform.providers")`. Refuses duplicate keys, a key different from the entry point name, and `dev_only` providers when `environment == "production"` (logs and skips). A missing entry point gives "run `uv sync` to register providers".
- `application/inbound.py`:
```python
@dataclass(frozen=True) class InboundReceipt: received: int; duplicates: int
class HookNotFound(Exception): ...          # unknown connection, wrong token, not active: one message
class InboundService:
    def __init__(self, hooks: HookLookup, registry: ProviderRegistry, secrets: SecretStore, store: InboundStore, clock=utc_now): ...
    def receive(self, connection_id: UUID, webhook_token: str, request: InboundRequest) -> InboundReceipt: ...
    def process_now(self, connection: Connection, events: Sequence[InboundEvent]) -> list[RouteResult]: ...   # alias only
class ProcessInboundHandler:   # job "process_inbound", payload {"inbox_event_id": "..."}
    def __init__(self, store: InboundStore, routes: Mapping[InboundEventType, InboundRoute]): ...
```
  `receive` order: (1) look up the connection; compare `sha256(token)` with `hmac.compare_digest`, using a dummy hash when not found so timing is equal; not found / wrong token / not `active` / provider not installed / `capabilities.webhooks` false → `HookNotFound`; (2) size check against `capabilities.max_inbound_bytes`; (3) `provider.verify_inbound` on the raw bytes, **before** parsing → `InboundRejected`; (4) `provider.parse_inbound`; (5) one transaction: insert each event into `inbox_events` with `ON CONFLICT (tenant_id, connection_ref, provider_event_id, event_type) DO NOTHING` and, for each new row, a `process_inbound` job. The tenant is **always** `connection.tenant_id`.
- Routes (application code, by event type, never by provider):
| Event | Route | Outcome stored on the inbox event |
|---|---|---|
| `ManagerAction` | `ChannelActionService.handle(ChannelEvent(...))` | `enrolled` / `recorded` / `replayed` / `rejected` / `conflict` (no retry for rejected/conflict) |
| `DeliveryStatusUpdate` | `MessageOutbox.apply_receipt` (→ `delivered` or `failed`) | `applied` / `unknown_message` |
| `CustomerMessage` | enquiry intake from the event: `InboundEnvelope` with `reply_to = delivery_recipient(...)`, enquiry, `prepare_enquiry` job (text only; attachments are phase 4) | `enquiry:<id>` |
| `ConsentChange` | none until phase 4 | `unsupported` |
- `application/channel_actions.py:38-51`: `ChannelEvent.connection_id: str` → `connection_ref: UUID`; `channel` holds the provider key. `:114-121` compares `binding.connection_ref`; `:124-135` builds `command_id`/`evidence_ref` from `connection_ref`. `start_enrollment(context, provider_key, connection_id: UUID | None = None)` checks the provider exists with `MANAGER_APPROVALS` and the tenant has an active connection for it (else `ValueError` → 422, like today's enum).
- `api/hooks.py` `build_hook_router(service)`: `POST /api/v1/hooks/{connection_id}/{webhook_token}`; reads the raw body (refuses > 1 MiB before reading more, then the provider limit).
| Case | Status | Body |
|---|---|---|
| received (new or duplicate) | 200 | `{"received": 1, "duplicates": 0}` |
| unknown connection, bad token, disabled, provider missing | 404 | `{"detail": "Not found"}` (same for all) |
| too large | 413 | `{"detail": "Event too large"}` |
| signature refused | 401 | `{"detail": "Invalid signature"}` |
| verified but unparseable | 400 | `{"detail": "Invalid event"}` |
| database unavailable | 503 | `{"detail": "Try again later"}` (provider retries; dedupe makes it safe) |
- Worker: register `JobKind.PROCESS_INBOUND`.
Tests: `tests/unit/application/test_providers.py` (fake entry points: load, duplicates, dev-only refused in production, unknown key); `test_inbound.py` (verify before parse with a spy; tenant from connection even when the body names another tenant; dedupe per `(connection, provider_event_id, event_type)`; same event ID with another type is not a duplicate; each route and outcome; rejected manager action completes the job without retry); `tests/unit/api/test_hooks.py` (table above, equal 404s).

### WI-5. Providers `api` and `simulated` (move existing adapters) — M
- `adapters/providers/api/__init__.py` exports `PROVIDER_KEY = "api"` and `build(env) -> ApiProvider`:
  - `purposes = {CUSTOMER_MESSAGES}`, `capabilities = Capabilities(delivery_mode=PULL, webhooks=False, outbound_initiation=False)`, `dev_only = False`;
  - `send` returns `SendReceipt(ReceiptKind.AVAILABLE)` (today's `adapters/delivery/api.py:16-17`); `verify_inbound`/`parse_inbound`/`provision`/`catalogue_reader` raise `OperationUnsupported`; `test` returns ok.
  - Delete `adapters/delivery/api.py`.
- `adapters/providers/simulated/` (`provider.py`, `outbox.py`, `signing.py`), `build(env) -> SimulatedProvider`:
  - `purposes = {MANAGER_APPROVALS, CATALOGUE}`, `dev_only = True`, `capabilities = Capabilities(webhooks=True, buttons=True, max_inbound_bytes=16 * 1024)`;
  - `send` writes the **same JSON and file names** as `adapters/channels/simulated.py:47-91` (review: `<task_id>.json` with `action_reference`/`expires_at`; notice: `notice-<notice_id>.json`), using `RenderedMessage.metadata` and `conn.legacy_key` for `connection_id`;
  - `verify_inbound`: HMAC-SHA256 of the raw body with the connection's `signing_secret` from the `SecretStore`, header `X-Simulated-Signature`;
  - `parse_inbound`: body `{"event_id", "external_user_id", "type": "enroll"|"decision", "code"?, "action_reference"?, "action"?, "reason"?}` (no tenant or connection fields; `extra="forbid"`) → `[ManagerAction]`;
  - `catalogue_reader`: a `SimulatedErp` holding the demo catalogue named in `conn.config["catalogue"]` for `conn.tenant_id`; `adapters/erp/simulated.py:170` gains `demo_catalogue(name, tenant_id)`; `SimulatedErp` stays where it is (tests, evaluation and `scripts/demo_quotation.py` import it).
  - Delete `adapters/channels/simulated.py`; update imports in `scripts/simulated_channel.py:22-26`, `demo/server.py:24`, `tests/integration/workflows/test_quotation.py:31` (imports only).
- `pyproject.toml`:
```toml
[project.entry-points."sales_platform.providers"]
api = "sales_platform.adapters.providers.api:build"
simulated = "sales_platform.adapters.providers.simulated:build"

[project.entry-points."sales_platform.secret_stores"]
local = "sales_platform.adapters.secrets.local:build"
none = "sales_platform.adapters.secrets.none:build"
```
- Seed (WI-2): when `SALES_SIMULATED_CHANNEL_SECRET` is set, the demo `simulated` connection gets `signing_secret` = that value (stored through the `SecretStore`) and webhook token `hmac_sha256(secret, "hook:" + connection_id)` (only its hash stored), so development tools can compute the hook URL from `.env`. `scripts/simulated_channel.py` gains `--via hooks` (default stays the old route).
- `api/channels.py` becomes the deprecated alias (§5.2). Old body and responses unchanged; docstring marks it deprecated with the removal phase.
Tests: contract tests (WI-14) for both; `tests/unit/adapters/providers/simulated/test_outbox.py` (WI-0 golden files), `test_signing.py`.

### WI-6. Migration `0009_outbound_messages` + persistence — M
**Table `outbound_messages`** (forced RLS):
| Column | Type | Rule |
|---|---|---|
| `id` | uuid PK | `UNIQUE (tenant_id, id)` |
| `tenant_id` | uuid NOT NULL | |
| `connection_ref` | uuid NOT NULL | FK `(tenant_id, connection_ref) → connections` |
| `provider_key` | varchar(32) NOT NULL | copy for display |
| `purpose` | varchar(32) NOT NULL | `customer_messages` / `manager_approvals` |
| `template_key` | varchar(64) NOT NULL | |
| `params` | jsonb NOT NULL | IDs only; **never** tokens or secrets |
| `body_text` | text NOT NULL | rendered fixed wording (no token) |
| `metadata` | jsonb NOT NULL DEFAULT '{}' | e.g. `task_id`, `quotation`, `version`, `notice_id` |
| `recipient` | varchar(200) NOT NULL | |
| `idempotency_key` | varchar(200) NOT NULL | `UNIQUE (tenant_id, idempotency_key)` |
| `status` | varchar(16) NOT NULL | `CHECK IN ('pending','sending','available','sent','delivered','failed','unknown')` |
| `provider_message_id` | varchar(200) NULL | `UNIQUE (tenant_id, connection_ref, provider_message_id)` where not null |
| `last_error` | varchar(300) NULL | |
| `document_id` | uuid NULL | FK `(tenant_id, document_id) → quotation_documents` |
| `quotation_id`, `version`, `approval_task_id`, `binding_id`, `enquiry_id`, `response_id` | NULL | links for the portal and audit |
| `correlation_id` | uuid NOT NULL | |
| `created_at`, `updated_at`, `sent_at`, `delivered_at` | timestamptz | |
Indexes: `(tenant_id, status)`, `(tenant_id, quotation_id)`, `(tenant_id, approval_task_id)`.
**`quotation_deliveries`:** add `outbound_message_id uuid NULL`, FK `(tenant_id, outbound_message_id)`, unique.
Idempotency keys: `quotation-delivery:<delivery_id>`, `approval-request:<task_id>:<binding_id>`, `response-notice:<response_id>:<binding_id>`.
**Grants:** worker `SELECT, INSERT, UPDATE (status, provider_message_id, last_error, updated_at, sent_at, delivered_at)` on `outbound_messages`, `UPDATE (outbound_message_id)` on `quotation_deliveries`; API `SELECT` on `outbound_messages`.
**Backfill:** one `outbound_messages` row per existing `quotation_deliveries` row: same status, `template_key = 'quotation_document'`, `connection_ref` from the enquiry's inbox event, `recipient`, `provider_message_id = provider_reference`, `document_id`, timestamps; link `outbound_message_id`. Past approval references and notices are not backfilled (no outbox existed; the references stay valid).
**Downgrade:** for each delivery still `pending` whose message is `pending`, insert a `deliver_quotation` job; for each `pending` approval task with no sent reference, insert a `notify_approvers` job; delete `send_message` jobs; drop the column and table.
**Persistence:** `persistence/messages.py` `SqlMessageOutbox`: `enqueue(...)` (insert `ON CONFLICT DO NOTHING` + `send_message` job in one transaction), `get`, `set_status` (compare-and-set; when the message has a linked delivery, the same transaction updates `quotation_deliveries` with the mapped status — `delivered` → `sent` — and on `available`/`sent` queues `expire_quotation` exactly like `persistence/documents.py:226-258`), `apply_receipt`.
Tests: `tests/integration/persistence/test_messages.py` (idempotent enqueue; compare-and-set; delivery mirror + expiry job; RLS), migration round trip with an M5/M6 delivery.

### WI-7. `send_message` job and templates — M
`application/messaging.py`:
```python
class OutboundService:
    def __init__(
        self, outbox: MessageOutbox, connections: ConnectionStore, registry: ProviderRegistry
    ): ...
    def queue(
        self,
        connection: Connection,
        *,
        template: TemplateKey,
        params: Mapping[str, object],
        recipient: str,
        idempotency_key: str,
        links: MessageLinks,
        correlation_id: UUID,
    ) -> UUID: ...


class SendMessageHandler:  # job "send_message", payload {"message_id": "..."}
    def __init__(
        self,
        outbox,
        connections,
        registry,
        secrets,
        hooks: Mapping[TemplateKey, SendHook],
        clock=utc_now,
    ): ...
```
Handler flow (same honesty rules as `application/delivery.py:82-179`):
1. Final status → return. `sending` → `unknown` ("The worker stopped while handing the message over; check with the provider").
2. Connection missing / not active / provider not installed → `failed` with a reason (replaces "No delivery channel is configured for ..." at `application/delivery.py:114-117`; the wording keeps the provider key).
3. `hook.before_send` → extra fields or cancel (`failed` with its reason).
4. Compare-and-set `pending → sending`.
5. `provider.send`: `SENT`/`AVAILABLE` → that status + `provider_message_id`; `SendRejected` → `failed`; `SendUncertain` → `unknown`; other errors → back to `pending` (last attempt: `failed`) and re-raise for the job retry.
6. `hook.after_send`.
Tests: `tests/unit/application/test_messaging.py` with a `RecordingProvider` fake (replaces `RecordingDeliveryChannel`/`RecordingNotifier` in `tests/unit/conftest.py:1089-1128`): every outcome above, plus "never resent when unknown".

### WI-8. Re-implement delivery and notifications on the outbox — M
| Job (kind unchanged) | Before | After |
|---|---|---|
| `deliver_quotation` (`application/delivery.py`) | renders, then `channel.deliver` | renders/stores the document (unchanged `_document`, `:181-220`), then `OutboundService.queue(template=QUOTATION_DOCUMENT, recipient=record.recipient, idempotency_key=f"quotation-delivery:{id}", document_id=...)` and links the delivery; `send_message` does the rest. Constructor loses `channels` (`:73-79`) |
| `notify_approvers` (`application/notifications.py:32-66`) | per binding: token, reference, `notifier.send_review`, `mark_sent` | per active binding of an approver: `queue(template=APPROVAL_REQUEST, ...)`; token/reference in `ApprovalRequestHook` (§5.3). Constructor loses `notifiers` |
| `notify_response` (`application/acceptance.py:281-315`) | `notice_text` + `notifier.send_notice` per binding | `queue(template=RESPONSE_NOTICE, idempotency_key=f"response-notice:{response_id}:{binding_id}")`; no more repeated notices on retry |
Delete `DeliveryChannel`, `DeliveryRequest`, `DeliveryReceipt`, `ReceiptKind`, `DeliveryRejected`, `DeliveryUncertain` from `ports/documents.py:34-80` and `ApprovalNotifier` from `ports/channels.py:42-60` (replaced; AGENTS.md). `ChannelBinding` (`ports/channels.py:16-29`) gains `connection_ref: UUID`.
Backward compatibility: the portal's delivery list (`DeliveryOut`, `api/portal.py:219-231`) is unchanged; the delivery row still moves `pending → sending → available/sent/failed/unknown`; the simulated outbox files are identical; one extra worker step (two jobs) adds about one poll interval.
Tests: rewrite `tests/unit/application/test_delivery.py`, `test_acceptance.py` (notices), and the notification cases in `tests/unit/workflows/test_quotation.py` and `test_channel_actions.py` case by case (P1-Q1).

### WI-9. API intake on the `api` connection — S
- `application/intake.py:55-110`: `submit(context, submission, idempotency_key, connection: Connection)`; refuses (`PermissionDenied`) if the connection is not active or not the caller's; the envelope gets `connection_id = connection.connection_id`, `channel = connection.provider_key`, `reply_to = delivery_recipient(capabilities.delivery_mode, connection.legacy_key, submission.customer_reference)` (= `api-key:<id>`, as today).
- `api/enquiries.py`: a dependency resolves the connection with `find_by_account(tenant, PROVIDER_KEY, context.subject)`; missing or disabled → 403 "Not permitted". Uses `PROVIDER_KEY` instead of `Channel.API.value` at `:365-366,392,417`; `:222` returns the stored string.
- `api/portal.py:584` returns `item.channel` (already a string).
External behaviour: same requests, status codes and JSON.

### WI-10. Per-business catalogue source — M
- `application/catalogue_sources.py`:
```python
class CatalogueSourceMissing(ErpError):  # code UNSUPPORTED, retryable False
    """No active catalogue connection, or its provider is not installed."""


class CatalogueSourceResolver:
    def __init__(
        self,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        secrets: SecretStore,
        ttl: timedelta = timedelta(seconds=30),
        clock=utc_now,
    ): ...
    def reader_for(self, tenant_id: UUID) -> ErpReader: ...


class TenantCatalogue:  # implements ErpReader; each call goes to reader_for(context.tenant_id)
    def __init__(self, resolver: CatalogueSourceResolver): ...
```
  Exactly one active connection with purpose `catalogue` (P1-Q7); readers cached per tenant for `ttl`, keyed by connection ID and `updated_at` so a changed connection is picked up within 30 s.
- `api/app.py:14,91-93` and `worker.py:16,68-69` build `TenantCatalogue(...)` instead of `SimulatedErp`. `QuotationPricingService`, `EnquiryWorkflowDeps` and `QuotationResponseService` keep taking an `ErpReader` (no change).
- `application/jobs.py:59-71`: treat `ErpError` with `retryable is False` as permanent (P1-Q4; if the user picks option b, only `CatalogueSourceMissing`). Recorded error: `"ErpError unsupported: No product source is connected for this business"`.
- If P1-Q3 = b: the enquiry graph's `match`/`assess` step catches `CatalogueSourceMissing` and finishes with `needs_review` and that reason (`workflows/enquiry.py:164-218`); quotation drafting records `pricing_failed` with an issue `no_catalogue_source`.
- API acceptance (`api/enquiries.py:303-308`) keeps 503 (P1-Q5 = a).
Tests: `tests/unit/application/test_catalogue_sources.py` (none → missing; two tenants isolated; cache TTL; disabled connection ignored); ERP contract suite (`tests/unit/ports/test_erp_contract.py`) also runs over `TenantCatalogue` + simulated provider; `tests/unit/application/test_jobs.py` (non-retryable ERP error → dead at attempt 1, retryable → retry); PostgreSQL: a third tenant without a source gets the clear outcome on the first attempt.

### WI-11. Composition root shared by API, worker and tests — S
`sales_platform/composition.py` (top level, like `worker.py`):
```python
@dataclass(frozen=True) class Runtime: registry; secrets; connections; catalogue: ErpReader; outbound: OutboundService
def build_runtime(settings: Settings, engine: Engine, *, catalogue: ErpReader | None = None) -> Runtime: ...
def build_job_handlers(settings: Settings, engine: Engine, runtime: Runtime, checkpointer, interpreter) -> dict[JobKind, JobHandler]: ...
```
`worker.py:53-122` and `api/app.py:84-139` call it. `tests/integration/workflows/test_quotation.py:120-170` uses `build_job_handlers(..., catalogue=SimulatedErp(...))` (wiring only; assertions unchanged), so the failure-injection tests keep their own `SimulatedErp`.

### WI-12. Portal API: connections and linking — S
| Route | Permission | Request | Response |
|---|---|---|---|
| `GET /api/v1/tenants/{t}/connections` | `integrations:manage` → all; approvers → `purpose=manager_approvals`, active only (P1-Q8) | `?purpose=` optional | `[{"connection_id", "provider_key", "provider_name", "display_name", "purposes": [...], "status", "last_error" (admins only), "created_at"}]` |
| `POST /api/v1/tenants/{t}/connections/{id}/enrollments` | `quotations:approve` | `{}` | `201 {"connection_id", "channel", "code", "expires_at"}`; 404 unknown/other tenant; 422 not a manager-approvals connection or not active |
| `POST /api/v1/tenants/{t}/channel-enrollments` (kept, deprecated) | unchanged | `{"channel": "simulated"}` | unchanged; unknown provider → 422 as today |
| `GET /api/v1/tenants/{t}/channel-bindings` | unchanged | | each item adds `connection_ref` and `connection_name`; `connection_id` keeps the legacy string |
`api/connections.py` (new) holds the first two; `api/portal.py:190-208,378-388,593-612` change as listed. Never return `config` secrets, `secret_refs` or token hashes.
Tests: `tests/unit/api/test_connections.py` (permissions, tenant isolation 404, fields hidden from approvers).

### WI-13. Portal feature `connections` replacing `channels` — M
- Move `portal/src/features/channels/*` to `portal/src/features/connections/` with layers `model/types.ts` (`Connection`, `Binding`, `Enrollment`), `api/connectionRepository.ts`, `services/connectionService.ts` (pure: group by purpose, status label, "can link"), `hooks/useConnections.ts` (TanStack Query keys `["tenants", t, "connections"]`, `["tenants", t, "channel-bindings"]`), `ui/pages/ConnectionsPage.tsx`, `ui/components/ConnectionList.tsx`, `LinkAccountCard.tsx` (one "Link my account" per linkable connection; replaces the hardcoded `"simulated"` in `EnrollmentCard.tsx:18`), `BindingList.tsx` (shows the connection name), `routes.tsx` (`connections`, plus `channels` → redirect), `index.ts`.
- `portal/src/app/layout/TopBar.tsx:9` label "Connections"; `portal/src/app/router.tsx:4` imports `connectionRoutes`.
- No Enable cards, no provider forms (phase 5).
- Tests (mirrored tree): `portal/tests/features/connections/services/connectionService.test.ts`, `ui/components/LinkAccountCard.test.tsx`, `ui/components/ConnectionList.test.tsx`; `portal/tests/architecture.test.ts` keeps passing (feature has `index.ts`, layer rules).

### WI-14. Provider contract tests and architecture rules — S
- `tests/unit/providers/test_provider_contract.py`, parametrised over every registered provider (from real entry points) plus a fake third provider (proves "a second provider needs no core change"):
  - `capabilities.webhooks` ⇒ a forged or missing signature raises `InboundRejected`; a valid one parses; the same body twice gives the same `provider_event_id`;
  - parsed events never carry a tenant; parsing the same body for two connections of different tenants yields the same events (tenant comes only from the connection);
  - `send` outcome mapping (`SENT`/`AVAILABLE`/`SendRejected`/`SendUncertain`);
  - unsupported operations raise `OperationUnsupported`, never return fake success;
  - `Purpose.CATALOGUE` in `purposes` ⇔ `catalogue_reader` works and passes the ERP contract suite;
  - `key` matches its entry point name; `dev_only` providers are absent in production.
- `tests/unit/test_architecture.py:21-30`: allow `cryptography` in adapters; add a test that no module outside `adapters/providers/`, `migrations/` and two listed exceptions (`api/enquiries.py` via `PROVIDER_KEY`, the deprecated `api/channels.py`) contains a string literal equal to an installed provider key or imports from `adapters.providers.<name>` (exceptions removed when the alias goes).
- `tests/unit/test_entry_points.py`: the installed metadata lists `api`, `simulated`, `local`, `none`.

### WI-15. Documentation and handoff — M (in the same pull requests as the code, finished here)
See §9.

## 7. Migration plan
| Step | Migration | Upgrade | Downgrade | Data check |
|---|---|---|---|---|
| 1 | `0008_connections` | tables, columns, policies, grants, backfill (§WI-2) | removes manager-action inbox rows and new jobs, restores old keys, drops tables | every `inbox_events`/`channel_bindings` row has `connection_ref`; one connection per API key; demo tenants have a `simulated` connection |
| 2 | `0009_outbound_messages` | table, link column, grants, backfill from deliveries | re-queues `deliver_quotation`/`notify_approvers` for pending work, deletes `send_message` jobs, drops table | one message per delivery with equal status |
Order of deployment: stop the worker → `alembic upgrade head` → run the seed (development) → start API and worker. `scripts/check_database.py --cycle` must pass (full suite, downgrade to empty, upgrade, full suite again), plus `0009 → 0007 → 0009` with data (an API enquiry, a binding, an approved and delivered quotation, an accepted one).

## 8. Test plan
| Level | What | Where |
|---|---|---|
| Unit (offline) | Every WI's tests above; WI-0 golden tests still green at the end | `tests/unit/...` mirrored |
| Provider contract | `api`, `simulated`, fake third provider | `tests/unit/providers/test_provider_contract.py` |
| ERP contract | `SimulatedErp` (existing) + `TenantCatalogue` over the simulated provider | `tests/unit/ports/test_erp_contract.py` |
| Architecture | layer imports, `cryptography`, no provider literals in the core, entry points registered | `tests/unit/test_architecture.py`, `test_entry_points.py` |
| PostgreSQL | All existing integration tests with **unchanged assertions** (API pull delivery, simulated approval via the alias, acceptance, expiry, isolation). New: approval through the **generic hook** (POST → 200 → drain worker → decision recorded, transport `simulated`); forged signature 401; hook URL of tenant A with a body naming tenant B → handled only in A; duplicate hook event processed once; disabled connection → 404; RLS isolation of `connections`/`outbound_messages`; worker grants are the minimum (a denied column update fails); tenant without a catalogue source → clear outcome on attempt 1; migration backfill and round trips | `tests/integration/...`; `tests/integration/conftest.py:27-33` adds `outbound_messages` to the truncate list |
| Portal | services/components above, architecture test, `npm run build` | `portal/tests/...` |
| Evaluation | `uv run python scripts/evaluate_extraction.py` → 14/14 (rules), unchanged | — |
| Live run | `uv run python demo/run_demo.py`: enquiry → review on the phone → approve → PDF → accept → notice; `scripts/simulated_channel.py enroll/decide` (old route) and `--via hooks`; `curl` of the generic hook with a bad token (404) and bad signature (401); portal Connections page shows the demo connection and links an account | recorded in the handoff |

## 9. Documentation to update
| Document | Change |
|---|---|
| **New ADR 0018 "Connector framework"** | Providers by entry points, connections, `SecretStore` (D8, D24 open), generic hook route, outbox + templates + send hooks, catalogue resolution, the deprecated alias and its removal phase; supersedes the channel-map parts of 0013/0015 |
| ADR 0007, 0011, 0013, 0015, 0016 | Status line: "extended by 0018" where their channel/notifier/delivery wording changes |
| `.docs/development/adding-channels.md` | Rewrite as `adding-providers.md` (old file becomes a one-line pointer): one package, entry point, capabilities, contract tests, secrets, no core change |
| `.docs/architecture/integrations.md` | Mark the provider contract, hook route, outbox and event types implemented; event dedupe key |
| `.docs/architecture/data-model.md`, `module-boundaries.md`, `execution-flow.md`, `approvals.md`, `flowcharts.md` | Connections, outbox, `process_inbound`/`send_message` flows |
| `.docs/development/configuration.md` | New settings (§WI-3); `SALES_SIMULATED_CHANNEL_SECRET` marked deprecated; new routes |
| `.docs/development/code-map.md`, `testing.md`, `setup.md` (`uv sync` after pulling, secret key), `portal-architecture.md` | New modules, tests, portal feature |
| `.docs/features/` | New `connections.md`; update `enquiry-intake.md`, `quotation-approval.md`, `quotation-delivery.md`, `customer-acceptance.md` |
| `.docs/operations/security.md` | Webhook tokens (hash only), verify-before-parse, secret store, the alias weakness, P1-Q9 finding |
| `demo/README.md` | Only if the demo changes (it should not) |
| `.docs/handoff.md` | Work done, checks and results, open questions, next action |

## 10. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Entry points are read from installed metadata; a stale install has no providers | Clear startup error ("run `uv sync`"); `test_entry_points.py`; setup docs |
| Backfill under forced RLS | Lift FORCE only inside the migration transaction (existing pattern); data checks after upgrade; round-trip tests with data |
| The worker gains approval-decision grants (manager actions run in a job) | Column-level grants only; `ApprovalService` checks unchanged; PostgreSQL test that other columns stay denied; security review of the diff |
| Action tokens or secrets leak into the outbox, logs or `connection_events` | Send hooks create tokens at send time; tests assert no token in `outbound_messages`; secret values never logged; `repr` hidden |
| Behaviour drift in wording or simulated files | WI-0 golden tests written first |
| Approval requests become at-most-once on crashes (P1-Q6) | Portal still shows pending tasks; `unknown` visible; resend action planned |
| Extra job step adds latency to delivery/notifications | One poll interval (2 s default); measured in the live run |
| Catalogue cache shows an old source after a change | 30 s TTL keyed by `updated_at`; no connection editing in phase 1 anyway |
| Deprecated alias keeps "tenant from body" | Dev-only, signed, production refuses it; removal phase recorded in ADR 0018 |
| Production has no secret store (D24) | `none` adapter fails clearly; `api` needs no secrets |
| Scope creep (Enable, conversations) | Out-of-scope table; no new user-visible feature except the renamed page |

## 11. Definition of done
- Every open question in §3 answered by the user and recorded (ADR 0018 / handoff).
- No `Channel`/`ApprovalChannel` enum, no hardcoded `SimulatedErp` in `api/app.py`/`worker.py`, no provider maps in the worker; architecture tests enforce it.
- `api` and `simulated` load through entry points and pass the provider contract tests; a fake third provider works with no core change.
- Migrations 0008/0009 upgrade, backfill and downgrade with data; `check_database.py --cycle` green.
- All existing behaviour tests green with unchanged assertions; WI-0 golden tests green; offline unit, PostgreSQL and portal suites green; evaluation 14/14.
- Live run: the demo page and `scripts/simulated_channel.py` work end to end; the generic hook route works for the simulated provider.
- Docs in §9 updated; handoff records checks, results and next action. Commit/push only with the user's permission, on a branch with a pull request to `main`.

## 12. Rough size
| WI | Size | WI | Size |
|---|---|---|---|
| 0 Pin behaviour | S | 8 Delivery/notifications on outbox | M |
| 1 Domain + ports | M | 9 API intake on connection | S |
| 2 Migration 0008 + persistence | L | 10 Catalogue resolution | M |
| 3 SecretStore | S | 11 Composition root | S |
| 4 Registry + hooks + inbound | L | 12 Portal API | S |
| 5 Providers api/simulated | M | 13 Portal feature | M |
| 6 Migration 0009 + persistence | M | 14 Contract + architecture tests | S |
| 7 send_message + templates | M | 15 Docs | M |
Total: about 5-6 weeks of focused work for one developer ("large refactor", 08-phases). Suggested pull requests: WI-0; WI-1+3; WI-2; WI-4+5; WI-6+7; WI-8+9; WI-10+11; WI-12+13; WI-14+15.

## Proposed changes to shared contracts
**Merged (2026-10-26):** these proposals are now part of [00-shared-contracts.md](00-shared-contracts.md) revision 2; where they differed, the contracts file wins (e.g. one name, `catalogue_reader`, for the catalogue factory method). Points needing the user's decision are listed there in §8.

These must be applied to [00-shared-contracts.md](00-shared-contracts.md) (with the user's agreement) before WI-1 starts.
| # | Contract text today | Proposed | Why |
|---|---|---|---|
| C1 | `Provider` has no catalogue operation | Add `catalogue_reader(conn, secrets) -> ErpReader` (raises `OperationUnsupported` unless `Purpose.CATALOGUE`) | Per-business catalogue resolution needs a generic way to get an `ErpReader` from a connection |
| C2 | `Provider` has no dev-only flag; production refusal lives in `config.py` naming the simulated provider | Add `dev_only: bool`; the registry skips dev-only providers in production | Removes a provider-named production check |
| C3 | How an entry point builds a provider is unspecified | Entry point → `ProviderFactory = Callable[[ProviderEnvironment], Provider]` | Providers need settings such as the storage folder without provider-named settings |
| C4 | `outbound_messages` statuses: pending/sending/sent/delivered/failed/unknown | Add `available` (pull providers) | The `api` provider's delivery is "available", not "sent" (ADR 0015) |
| C5 | `connections` columns listed only in 03 | Add `account_ref` (unique per tenant + provider) and make the webhook token a nullable `webhook_token_sha256` | Maps API keys and legacy `connection_id` strings 1:1; `api` has no webhook |
| C6 | 0008: "`channel_bindings` gains `connection_ref`" | Also `channel_enrollments.connection_ref` (nullable) and `inbox_events.processed_at`, `inbox_events.outcome` | Codes bound to one connection; async routing needs a recorded outcome |
| C7 | 0008 backfill: "one `simulated` connection per tenant with bindings" | One per distinct `(tenant, connection_id)` used by bindings (normally one) | The old route let callers choose any `connection_id` string |
| C8 | `ports/messages.py` has `MessageOutbox`, `MessageSender` | Add `SendHook` (before/after send, per `TemplateKey`); `MessageSender` is the `send_message` handler | Action tokens must never be stored in the outbox |
| C9 | Event type named `DeliveryStatus` | Name it `DeliveryStatusUpdate`; inbound event types live in `domain/intake.py` | `domain/documents.py:36` already has `DeliveryStatus` |
| C10 | `adapters/secrets/cloud.py` "chosen by setting" | Secret stores by entry point group `sales_platform.secret_stores` (`local`, `none` in phase 1); `cloud.py` written only after D24 | Same generic loading as providers; D24 is open |
| C11 | Column name `connection` for the outbox | `connection_ref`, a composite FK `(tenant_id, connection_ref)` like every other table | Consistency with `inbox_events`/`channel_bindings` |
| C12 | 08-phases: "All existing tests unchanged" | "All behaviour tests (API, PostgreSQL, evaluation, demo) unchanged; unit tests of replaced classes migrated case by case" (P1-Q1) | The contract itself replaces `DeliveryChannel`/`ApprovalNotifier` |
| C13 | Phase 5 note | The `Provider` protocol will likely need a webhook handshake operation (e.g. GET challenge) | Some platforms verify a webhook with a GET request before sending events |
