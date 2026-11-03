"""The Provider contract: what every tool package (api, simulated, Telegram...) offers.

A provider package registers a factory under the Python entry point group
`sales_platform.providers`; the registry builds every installed provider at start-up. Adding a
tool is one package plus one entry point. No core code changes (ADR 0018,
.docs/development/adding-providers.md).

Rules for implementations:
- Never take the tenant from a request. The tenant is always the connection's.
- verify_inbound (or verify_platform_inbound) runs on the raw bytes before parse_inbound; a
  forged or stale request raises InboundRejected.
- send raises SendRejected when the provider refused for good and SendUncertain when the
  message may have been accepted (it is then never resent). Any other exception means nothing
  was handed over, so the attempt may be retried.
- Operations a provider does not support raise OperationUnsupported, never a fake success.

Phase 5 adds the one-click lifecycle (provision, resume, deprovision), platform webhooks for
tools with one callback URL for the whole platform (verify_platform_inbound, routing_keys),
GET handshakes, files fetched after a webhook, account-linking hints and provider-hosted setup
pages. ProviderDefaults gives every optional operation an explicit "not supported", so a
package only writes what its tool can do.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sales_platform.integrations.domain.connections import Capabilities, Connection, Purpose
from sales_platform.integrations.domain.events import InboundEvent
from sales_platform.platform.ports.secrets import SecretStore


@dataclass(frozen=True)
class InboundRequest:
    headers: Mapping[str, str]  # names in lower case
    body: bytes
    query: Mapping[str, str]
    received_at: datetime
    method: str = "POST"
    url: str = ""  # the public URL the provider called (signatures over URLs, e.g. Twilio)


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class RenderedMessage:
    """One outbox message ready for a provider. message_id doubles as the idempotency key."""

    message_id: UUID
    idempotency_key: str
    recipient: str
    text: str
    metadata: Mapping[str, str]  # e.g. task_id, quotation, version, notice_id
    actions: tuple[str, ...] = ()
    action_reference: str | None = None  # created at send time, never stored in the outbox
    expires_at: datetime | None = None
    attachment: Attachment | None = None
    # Phase 5: a page in our portal for this message (the signed approval link, which needs
    # sign-in) and a short-lived public link to the attachment for tools that fetch by URL.
    link: str | None = None
    attachment_url: str | None = None
    # Who it is for: a customer, or a manager about an approval (one connection may serve
    # both, e.g. customers through the business's account and managers through our bot).
    purpose: Purpose = Purpose.CUSTOMER_MESSAGES


class ReceiptKind(StrEnum):
    SENT = "sent"  # push: the provider accepted it
    AVAILABLE = "available"  # pull: the recipient can now fetch it


@dataclass(frozen=True)
class SendReceipt:
    kind: ReceiptKind
    provider_message_id: str | None = None
    # Platform-webhook tools: keys that route a reply to this message back to its connection
    # (e.g. the chat and message ID in our one Telegram bot). Stored in connection_routes.
    routing_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestResult:
    """Outcome of a provider's connection test (not a pytest test class)."""

    __test__ = False
    ok: bool
    detail: str | None = None


class InboundRejected(Exception):
    """The callback is not genuinely from the provider (bad signature, stale timestamp)."""


class InvalidInbound(Exception):
    """The callback is genuine but its content cannot be understood."""


class SendRejected(Exception):
    """The provider refused the message for good (bad address, blocked, too large...)."""


class SendUncertain(Exception):
    """The message may have reached the provider (e.g. a timeout after sending)."""


class OperationUnsupported(Exception):
    """This provider does not offer the requested operation."""


class PendingActionKind(StrEnum):
    OPEN_URL = "open_url"  # open a provider's page (an OAuth login); it returns to resume
    PROVIDER_PAGE = "provider_page"  # our API serves HTML from the package (a pop-up SDK)
    SHOW_STEPS = "show_steps"  # numbered steps the business does in the tool itself
    FILL_FORM = "fill_form"  # a form from a JSON schema; the answers go to resume


@dataclass(frozen=True)
class PendingAction:
    """The one step a business still has to do before a connection works.

    Stored on the connection and shown in the portal, except `once`: values shown only in the
    answer that created them (a link code). Steps refer to them as {name}. Pressing Enable
    again creates new ones.
    """

    action_id: str  # 1-64 characters: letters, digits, _ and -
    kind: PendingActionKind
    title: str
    steps: tuple[str, ...] = ()
    url: str | None = None
    form_schema: Mapping[str, object] | None = None
    once: Mapping[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None

    @property
    def returns_data(self) -> bool:
        """The portal (or a provider page) posts the result to resume."""
        return self.kind is not PendingActionKind.SHOW_STEPS


class ProvisionStatus(StrEnum):
    READY = "ready"  # the connection works now
    NEEDS_ACTION = "needs_action"  # the business must do `pending`
    WORKING = "working"  # the tool is still working (a review); poll_after says when to ask
    FAILED = "failed"  # cannot continue; `error` says why (shown to administrators)


@dataclass(frozen=True)
class ProvisionResult:
    """What provision, resume or a poll achieved. The core applies it in one transaction:
    status, public config, secrets (kept in the SecretStore, references on the connection),
    routes and the pending step. Providers never write the database themselves."""

    status: ProvisionStatus
    pending: PendingAction | None = None
    detail: str | None = None  # public, e.g. "Customers can write to @acme"
    config: Mapping[str, object] = field(default_factory=dict)  # merged into the config
    secrets: Mapping[str, str] = field(default_factory=dict)
    add_routes: Mapping[str, datetime | None] = field(default_factory=dict)  # key -> expiry
    remove_routes: tuple[str, ...] = ()
    poll_after: timedelta | None = None
    error: str | None = None


@dataclass(frozen=True)
class HandshakeResponse:
    """The answer to a provider's verification GET (e.g. echoing a challenge)."""

    body: str
    status: int = 200
    content_type: str = "text/plain"


@dataclass(frozen=True)
class EnrollmentHint:
    """How a manager links their own account, in the provider's words (shown in the portal)."""

    text: str
    url: str | None = None  # e.g. a deep link that sends the code from their own account


@dataclass(frozen=True)
class CostNote:
    """Shown on the Enable card. With costs, an administrator must tick a box (P5-11)."""

    free: bool
    text: str


FREE = CostNote(free=True, text="Free")


@dataclass(frozen=True)
class ProviderEnvironment:
    """Generic dependencies given to every provider factory; never provider-named settings."""

    environment: str  # development | test | production
    storage_dir: Path
    options: Mapping[str, object] = field(default_factory=dict)
    # Our own catalogue tables, bound to the running process's database role (phase 3): a
    # catalogue.ports.catalogue_admin.CatalogueReadStore (typed loosely here, ADR 0026).
    catalogue_store: object | None = None
    # Phase 5: our public HTTPS address (SALES_PUBLIC_BASE_URL), for webhook URLs.
    public_base_url: str = ""


class Provider(Protocol):
    key: str
    display_name: str
    purposes: frozenset[Purpose]
    capabilities: Capabilities
    dev_only: bool  # the registry skips it in production
    cost: CostNote
    # A Pydantic model (adapters only) of the company-level settings that the operator
    # command keeps in the platform secret namespace; None when the provider needs none.
    platform_config_model: type | None

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult: ...

    # --- lifecycle (capabilities.self_service) ------------------------------------------
    def provision(self, conn: Connection, secrets: SecretStore) -> ProvisionResult:
        """Create what the business needs (Enable). Must be safe to call again."""
        ...

    def resume(
        self,
        conn: Connection,
        secrets: SecretStore,
        action_id: str,
        submission: Mapping[str, object],
    ) -> ProvisionResult:
        """Continue after a pending action returned data, a setup event or a poll."""
        ...

    def deprovision(self, conn: Connection, secrets: SecretStore) -> tuple[str, ...]:
        """Undo what provision created; returns steps the business must still do itself."""
        ...

    def provider_page(self, conn: Connection, secrets: SecretStore, action_id: str) -> str:
        """HTML for a provider_page pending action (served by our API)."""
        ...

    def enrollment_hint(
        self, conn: Connection, secrets: SecretStore, code: str
    ) -> EnrollmentHint: ...

    def platform_setup(self, secrets: SecretStore, public_base_url: str) -> str:
        """Company-level setup run once by an operator (e.g. register our platform webhook
        URL with the tool); returns what was done. Uses only the platform secrets."""
        ...

    # --- inbound -------------------------------------------------------------------------
    def handshake(
        self, conn: Connection | None, secrets: SecretStore, request: InboundRequest
    ) -> HandshakeResponse | None:
        """Answer a verification GET (conn is None on the platform route); None: not one."""
        ...

    def verify_inbound(
        self, conn: Connection, secrets: SecretStore, request: InboundRequest
    ) -> None: ...

    def verify_platform_inbound(self, secrets: SecretStore, request: InboundRequest) -> None:
        """The platform route (capabilities.platform_webhooks): check with company secrets."""
        ...

    def routing_keys(self, request: InboundRequest) -> Sequence[str]:
        """After verify_platform_inbound: the keys present in this request, never guessed."""
        ...

    def unrouted(self, secrets: SecretStore, request: InboundRequest) -> None:
        """A verified platform request no connection claims: the tool may answer the sender
        generically (nothing is stored)."""
        ...

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        """Normalised events; raises InvalidInbound. Each carries a stable provider_event_id."""
        ...

    def fetch_file(
        self, conn: Connection, secrets: SecretStore, file_ref: str, max_bytes: int
    ) -> bytes:
        """Download a file named in a CustomerMessage; larger than max_bytes: ValueError."""
        ...

    # --- outbound ------------------------------------------------------------------------
    def send(
        self, conn: Connection, secrets: SecretStore, message: RenderedMessage
    ) -> SendReceipt: ...


class ProviderDefaults:
    """Explicit "not supported" for every optional operation; providers subclass it."""

    dev_only = False
    cost = FREE
    platform_config_model: type | None = None

    def provision(self, conn: Connection, secrets: SecretStore) -> ProvisionResult:
        raise OperationUnsupported("This tool cannot be enabled with one click")

    def resume(
        self,
        conn: Connection,
        secrets: SecretStore,
        action_id: str,
        submission: Mapping[str, object],
    ) -> ProvisionResult:
        raise OperationUnsupported("This tool has no setup steps")

    def deprovision(self, conn: Connection, secrets: SecretStore) -> tuple[str, ...]:
        return ()

    def provider_page(self, conn: Connection, secrets: SecretStore, action_id: str) -> str:
        raise OperationUnsupported("This tool has no setup page")

    def enrollment_hint(self, conn: Connection, secrets: SecretStore, code: str) -> EnrollmentHint:
        return EnrollmentHint(f"Send the code {code} from your own account within 10 minutes.")

    def platform_setup(self, secrets: SecretStore, public_base_url: str) -> str:
        raise OperationUnsupported("This tool needs no company-level setup")

    def handshake(
        self, conn: Connection | None, secrets: SecretStore, request: InboundRequest
    ) -> HandshakeResponse | None:
        return None

    def verify_inbound(
        self, conn: Connection, secrets: SecretStore, request: InboundRequest
    ) -> None:
        raise OperationUnsupported("This tool has no per-connection webhook")

    def verify_platform_inbound(self, secrets: SecretStore, request: InboundRequest) -> None:
        raise OperationUnsupported("This tool has no platform webhook")

    def routing_keys(self, request: InboundRequest) -> Sequence[str]:
        raise OperationUnsupported("This tool has no platform webhook")

    def unrouted(self, secrets: SecretStore, request: InboundRequest) -> None:
        return None

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        raise OperationUnsupported("This tool sends no webhooks")

    def fetch_file(
        self, conn: Connection, secrets: SecretStore, file_ref: str, max_bytes: int
    ) -> bytes:
        raise OperationUnsupported("This tool sends no files")


type ProviderFactory = Callable[[ProviderEnvironment], Provider]
