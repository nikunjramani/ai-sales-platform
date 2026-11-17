"""Connections: one business's configured tool (ADR 0018).

A provider is a kind of tool (the API, the simulated phone, later Telegram or an ERP). A
connection is one business's use of a provider: a database row with its purposes, non-secret
settings and references to secrets kept in a SecretStore (never the secrets themselves).

The core never asks "which provider is this?". It asks what the connection is for (purposes)
and what the provider can do (capabilities), so a new provider needs no core change.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

MAX_DISPLAY_NAME = 100


class Purpose(StrEnum):
    CUSTOMER_MESSAGES = "customer_messages"
    MANAGER_APPROVALS = "manager_approvals"
    CATALOGUE = "catalogue"
    # Phase 7: the business's orders are written to this tool (an ERP that takes sales orders).


class ConnectionStatus(StrEnum):
    SETTING_UP = "setting_up"
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"


class DeliveryMode(StrEnum):
    """How a customer gets what we send: pushed to them, or fetched (pulled) by their system."""

    PUSH = "push"
    PULL = "pull"


@dataclass(frozen=True)
class CatalogueCapabilities:
    """What a product source can do beyond the five reads (phase 6, contracts §1)."""

    editable: bool = False  # staff edit the products in our portal


@dataclass(frozen=True)
class Capabilities:
    """What a provider can do. The core decides by these flags, never by provider name."""

    delivery_mode: DeliveryMode = DeliveryMode.PUSH
    webhooks: bool = False  # receives callbacks on the generic hook route
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
    # Staff edit the products in our portal (phase 3); other sources are read-only there.
    catalogue_editable: bool = False
    # We host the customer side ourselves (phase 4): a public chat page and API under
    # /chat/<handle>, visitor sessions and a daily reading cap. Website chat is the first.
    hosted_chat: bool = False
    # Phase 5: Enable runs Provider.provision (one click, then perhaps one pending step).
    self_service: bool = False
    # One callback URL for the whole platform (one bot, one app): /api/v1/hooks/p/<key>,
    # routed to a business by connection_routes instead of a per-connection URL.
    platform_webhooks: bool = False
    # The tool downloads attachments from a public URL instead of receiving the bytes.
    attachments_by_url: bool = False
    # Product source abilities (None: not a product source).
    catalogue: CatalogueCapabilities | None = None


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
    public_handle: str | None = None  # hosted chat address /chat/<handle> (phase 4, Q13)
    public_handle_changed: bool = False  # an administrator may change it once
    # Phase 5: the step the business still has to do (PendingAction without one-time values).
    pending_action: Mapping[str, object] | None = None

    @property
    def active(self) -> bool:
        return self.status is ConnectionStatus.ACTIVE

    @property
    def legacy_key(self) -> str:
        """The free-text connection string used before connections existed.

        Old columns (`connection_id` on inbox events and bindings) and the simulated outbox
        files keep showing this value, so readers of those stay unchanged.
        """
        return self.account_ref or str(self.connection_id)

    def serves(self, purpose: Purpose) -> bool:
        return purpose in self.purposes


def validate_display_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("A connection needs a name")
    if len(name) > MAX_DISPLAY_NAME or not name.isprintable():
        raise ValueError(f"A connection name is 1-{MAX_DISPLAY_NAME} printable characters")
    return name.strip()


def validate_purposes(requested: Iterable[str], offered: frozenset[Purpose]) -> frozenset[Purpose]:
    """The purposes chosen for a connection: at least one, all offered by its provider."""
    try:
        chosen = frozenset(Purpose(value) for value in requested)
    except ValueError:
        raise ValueError("Unknown connection purpose") from None
    if not chosen:
        raise ValueError("A connection needs at least one purpose")
    if not chosen <= offered:
        raise ValueError("This provider cannot be used for that purpose")
    return chosen
