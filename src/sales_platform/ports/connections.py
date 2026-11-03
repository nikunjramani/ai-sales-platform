"""Connection storage contracts (ADR 0018).

ConnectionStore reads one tenant's connections. HookLookup is the one cross-tenant read: the
generic webhook route knows only a connection ID from its URL, so it may reveal exactly that
row (and its webhook token hash) and nothing else, like the API key lookup. Phase 5 adds the
self-service lifecycle store and platform routes (RouteLookup reveals one route by its key).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.domain.connections import Connection, ConnectionStatus, Purpose


class ConnectionStore(Protocol):
    def get(self, tenant_id: UUID, connection_id: UUID) -> Connection | None: ...

    def find_by_account(
        self, tenant_id: UUID, provider_key: str, account_ref: str
    ) -> Connection | None: ...

    def list_for(self, tenant_id: UUID, purpose: Purpose | None = None) -> list[Connection]: ...

    def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
        """Active connections with Purpose.CATALOGUE (at most one, a database rule)."""
        ...


class HookLookup(Protocol):
    def find_for_hook(self, connection_id: UUID) -> tuple[Connection, str | None] | None:
        """The connection and its webhook token's SHA-256 (None: no webhook), or None."""
        ...


class HostedChatAdmin(Protocol):
    """Portal-side changes to a hosted chat connection (phase 4)."""

    def create(
        self,
        tenant_id: UUID,
        provider_key: str,
        display_name: str,
        purposes: Sequence[str],
        config: Mapping[str, object],
        actor: str,
    ) -> UUID: ...

    def ensure_handle(self, tenant_id: UUID, connection_id: UUID, actor: str) -> None: ...

    def change_handle(
        self, tenant_id: UUID, connection_id: UUID, handle: str, actor: str
    ) -> bool: ...

    def update_config(
        self, tenant_id: UUID, connection_id: UUID, config: Mapping[str, object], actor: str
    ) -> None: ...

    def set_status(
        self, tenant_id: UUID, connection_id: UUID, status: ConnectionStatus, actor: str
    ) -> None: ...


class RouteTaken(Exception):
    """A routing key already names another connection (keys are unique on the platform)."""


@dataclass(frozen=True)
class ConnectionChange:
    """Everything one provisioning step changes, applied in one transaction (phase 5).

    config and secret_refs are the complete new values; routes are added (key -> expiry) or
    removed; poll_at queues a poll_connection job. detail goes to the audit, never secrets.
    """

    status: ConnectionStatus
    config: Mapping[str, object]
    secret_refs: Mapping[str, str]
    last_error: str | None
    pending_action: Mapping[str, object] | None
    event_kind: str  # provisioned | resumed | status_changed | deprovisioned | tested
    add_routes: Mapping[str, datetime | None] = field(default_factory=dict)
    remove_routes: tuple[str, ...] = ()
    remove_all_routes: bool = False
    poll_at: datetime | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
    # Phase 6: a business has one product source and one place its orders go. Becoming active
    # for one of these purposes switches off the active connection that has it (event reason
    # "replaced"); Disable switches the replaced ones back on if nothing else took their place.
    exclusive_purposes: tuple[str, ...] = ()
    restore_replaced: bool = False
    jobs: tuple[tuple[str, Mapping[str, str]], ...] = ()  # (job kind, payload) queued with it


class ConnectionLifecycleStore(Protocol):
    """Self-service writes (Enable, setup steps, Disable). Both the API and the worker use it:
    a setup event from the provider finishes a connection in the worker."""

    def create(
        self,
        tenant_id: UUID,
        provider_key: str,
        display_name: str,
        purposes: Sequence[str],
        actor: str,
    ) -> UUID:
        """A new connection in setting_up (with its 'created' audit event)."""
        ...

    def apply(
        self, tenant_id: UUID, connection_id: UUID, change: ConnectionChange, actor: str
    ) -> None:
        """Raises RouteTaken (and changes nothing) if an added key belongs to another one."""
        ...

    def record_event(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        kind: str,
        actor: str,
        detail: Mapping[str, object],
    ) -> None: ...


class RouteLookup(Protocol):
    """The one cross-tenant read of the platform webhook: an exact key names one connection."""

    def resolve(self, provider_key: str, routing_key: str) -> tuple[UUID, UUID] | None:
        """(tenant_id, connection_id) for an unexpired key, or None."""
        ...


class RouteWriter(Protocol):
    def add_routes(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        provider_key: str,
        keys: Mapping[str, datetime | None],
    ) -> None:
        """Keys that route replies to a sent message; a key already taken is skipped."""
        ...
