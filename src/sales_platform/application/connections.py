"""Who may see a business's connections (ADR 0018, user decision P1-Q8).

Administrators (integrations:manage) see every connection, with its last error. Approvers see
only the active manager-approval connections they can link their own account to. Others see
nothing. Secrets, secret references and webhook token hashes are never part of a view.
Phase 4 adds the first self-service tool: an administrator enables a hosted chat (website
chat) with one click and gets its public address; other tools follow in phase 5.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sales_platform.application.channel_actions import ConnectionNotFound
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.chat import MAX_GREETING, validate_handle, validate_origins
from sales_platform.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.domain.tenancy import Permission, PermissionDenied, TenantContext
from sales_platform.ports.connections import ConnectionStore, HostedChatAdmin


@dataclass(frozen=True)
class ConnectionView:
    connection: Connection
    provider_name: str
    show_errors: bool  # only administrators see technical errors


class HandleTaken(Exception):
    """The chat address is used by another chat."""


class ConnectionService:
    def __init__(
        self,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        chats: HostedChatAdmin | None = None,
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._chats = chats

    def list(self, context: TenantContext, purpose: Purpose | None = None) -> list[ConnectionView]:
        admin = Permission.MANAGE_INTEGRATIONS in context.permissions
        if not admin and Permission.APPROVE_QUOTATIONS not in context.permissions:
            raise PermissionDenied("Permission integrations:manage is required")
        if admin:
            rows = self._connections.list_for(context.tenant_id, purpose)
        else:
            rows = [
                row
                for row in self._connections.list_for(context.tenant_id, Purpose.MANAGER_APPROVALS)
                if row.active and (purpose is None or purpose is Purpose.MANAGER_APPROVALS)
            ]
        return [ConnectionView(row, self.provider_name(row), admin) for row in rows]

    def provider_name(self, connection: Connection) -> str:
        provider = self._registry.find(connection.provider_key)
        return provider.display_name if provider is not None else connection.provider_key

    # --- hosted chat (phase 4) ------------------------------------------------------------
    def enablable(self, context: TenantContext) -> Sequence[tuple[str, str]]:
        """(provider key, name) of the installed tools an administrator can enable here."""
        context.require(Permission.MANAGE_INTEGRATIONS)
        return [
            (provider.key, provider.display_name)
            for provider in self._registry.all()
            if provider.capabilities.hosted_chat and self._chats is not None
        ]

    def enable_hosted_chat(self, context: TenantContext, provider_key: str) -> Connection:
        """One click: an active connection with its public address. Enabling again returns
        the business's existing active one."""
        context.require(Permission.MANAGE_INTEGRATIONS)
        provider = self._registry.find(provider_key)
        if provider is None or not provider.capabilities.hosted_chat or self._chats is None:
            raise ValueError("This tool cannot be enabled here yet")
        for row in self._connections.list_for(context.tenant_id):
            if row.provider_key == provider_key and row.active:
                if row.public_handle is None:
                    self._chats.ensure_handle(context.tenant_id, row.connection_id, context.subject)
                    return self._chat(context, row.connection_id)
                return row
        connection_id = self._chats.create(
            context.tenant_id,
            provider_key,
            provider.display_name,
            sorted(purpose.value for purpose in provider.purposes),
            {"allowed_origins": [], "greeting": ""},
            context.subject,
        )
        return self._chat(context, connection_id)

    def configure_hosted_chat(
        self,
        context: TenantContext,
        connection_id: UUID,
        allowed_origins: Sequence[str] | None,
        greeting: str | None,
    ) -> Connection:
        """Websites allowed to embed the chat (at most 10) and the greeting (300 characters)."""
        row = self._chat(context, connection_id)
        config = dict(row.config)
        if allowed_origins is not None:
            config["allowed_origins"] = validate_origins(allowed_origins)
        if greeting is not None:
            text = " ".join(greeting.split())
            if len(text) > MAX_GREETING or not text.isprintable():
                raise ValueError(f"The greeting is at most {MAX_GREETING} characters")
            config["greeting"] = text
        assert self._chats is not None
        self._chats.update_config(context.tenant_id, connection_id, config, context.subject)
        return self._chat(context, connection_id)

    def change_chat_address(
        self, context: TenantContext, connection_id: UUID, handle: str
    ) -> Connection:
        """Once only (Q13): the old address stops working. Raises HandleTaken."""
        row = self._chat(context, connection_id)
        if row.public_handle_changed:
            raise ValueError("The chat address can be changed only once")
        assert self._chats is not None
        if not self._chats.change_handle(
            context.tenant_id, connection_id, validate_handle(handle), context.subject
        ):
            raise HandleTaken("This chat address is taken")
        return self._chat(context, connection_id)

    def set_hosted_chat_active(
        self, context: TenantContext, connection_id: UUID, active: bool
    ) -> Connection:
        """Switch the chat off (its address answers "not found") or on again."""
        self._chat(context, connection_id)
        assert self._chats is not None
        status = ConnectionStatus.ACTIVE if active else ConnectionStatus.DISABLED
        self._chats.set_status(context.tenant_id, connection_id, status, context.subject)
        return self._chat(context, connection_id)

    def _chat(self, context: TenantContext, connection_id: UUID) -> Connection:
        context.require(Permission.MANAGE_INTEGRATIONS)
        row = self._connections.get(context.tenant_id, connection_id)
        provider = None if row is None else self._registry.find(row.provider_key)
        if row is None or provider is None or not provider.capabilities.hosted_chat:
            raise ConnectionNotFound("Not found")
        return row
