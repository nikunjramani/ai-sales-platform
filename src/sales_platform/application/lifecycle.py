"""One-click Enable for self-service tools, their setup steps, Test and Disable (phase 5).

Flow (09 one-click integrations):
1. enable: an administrator presses Enable. A tool with costs needs the "may incur costs" tick
   (P5-11). We create the connection (setting_up) and call Provider.provision, which creates
   what the business needs and answers ready, needs_action (one step for the business: open a
   page, follow steps, fill a form) or working (the tool is still reviewing; we poll).
   Pressing Enable again on a tool that is not active yet provisions again (a new link code).
2. resume: the step returned data (a form, a pop-up's result) -> Provider.resume.
3. A setup event from the tool (the business linked its account in the tool) arrives on a
   webhook and is handled by the worker: on_update -> Provider.resume, no person involved.
4. poll: a poll_connection job asks the provider again (Provider.resume with action "poll").
5. test / disable: Provider.test; Provider.deprovision returns steps the business must still do
   in the tool itself (we cannot remove our bot from their account).

Every step's ProvisionResult is applied in one transaction: secrets go to the SecretStore and
only their references to the connection; one-time values (`once`) are returned to the caller
and never stored. The core never names a provider: it reads capabilities and results.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sales_platform.application.channel_actions import ConnectionNotFound
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.connections import (
    Connection,
    ConnectionStatus,
    Purpose,
    validate_purposes,
)
from sales_platform.domain.intake import ConnectionUpdate
from sales_platform.domain.jobs import JobKind, PermanentJobError
from sales_platform.domain.tenancy import Permission, TenantContext
from sales_platform.ports.connections import (
    ConnectionChange,
    ConnectionLifecycleStore,
    ConnectionStore,
)
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.ports.providers import (
    OperationUnsupported,
    PendingAction,
    PendingActionKind,
    Provider,
    ProvisionResult,
    ProvisionStatus,
    TestResult,
)
from sales_platform.ports.secrets import SecretStore

POLL_ACTION = "poll"
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EXISTING = (ConnectionStatus.ACTIVE, ConnectionStatus.SETTING_UP, ConnectionStatus.ERROR)


def utc_now() -> datetime:
    return datetime.now(UTC)


class CostsNotAccepted(ValueError):
    """This tool may cost money; the administrator must accept that first (P5-11)."""


class ActionNotFound(Exception):
    """No such pending step on this connection (finished, replaced or expired)."""


@dataclass(frozen=True)
class EnableOutcome:
    connection: Connection
    pending: PendingAction | None  # with its one-time values; only in this answer
    detail: str | None


def pending_to_json(action: PendingAction) -> dict[str, object]:
    """Stored form, without the one-time values."""
    return {
        "action_id": action.action_id,
        "kind": action.kind.value,
        "title": action.title,
        "steps": list(action.steps),
        "url": action.url,
        "form_schema": None if action.form_schema is None else dict(action.form_schema),
        "expires_at": None if action.expires_at is None else action.expires_at.isoformat(),
    }


def pending_from_json(data: Mapping[str, object]) -> PendingAction:
    steps = data.get("steps") or []
    schema = data.get("form_schema")
    expires = data.get("expires_at")
    url = data.get("url")
    return PendingAction(
        action_id=str(data["action_id"]),
        kind=PendingActionKind(str(data["kind"])),
        title=str(data["title"]),
        steps=tuple(str(step) for step in steps) if isinstance(steps, list) else (),
        url=None if url is None else str(url),
        form_schema=dict(schema) if isinstance(schema, dict) else None,
        expires_at=None if expires is None else datetime.fromisoformat(str(expires)),
    )


class ConnectionLifecycle:
    def __init__(
        self,
        connections: ConnectionStore,
        store: ConnectionLifecycleStore,
        registry: ProviderRegistry,
        secrets: SecretStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._connections = connections
        self._store = store
        self._registry = registry
        self._secrets = secrets
        self._clock = clock

    def available(self, context: TenantContext) -> list[Provider]:
        """Installed tools an administrator can enable with one click."""
        context.require(Permission.MANAGE_INTEGRATIONS)
        return [provider for provider in self._registry.all() if provider.capabilities.self_service]

    def enable(
        self,
        context: TenantContext,
        provider_key: str,
        accept_costs: bool,
        purposes: Sequence[str] | None = None,
    ) -> EnableOutcome:
        context.require(Permission.MANAGE_INTEGRATIONS)
        provider = self._registry.find(provider_key)
        if provider is None or not provider.capabilities.self_service:
            raise ValueError("This tool cannot be enabled here")
        if not provider.cost.free and not accept_costs:
            raise CostsNotAccepted("Please confirm that this tool may incur costs")
        chosen = validate_purposes(
            purposes if purposes is not None else [item.value for item in provider.purposes],
            provider.purposes,
        )
        existing = next(
            (
                row
                for row in self._connections.list_for(context.tenant_id)
                if row.provider_key == provider_key and row.status in _EXISTING
            ),
            None,
        )
        if existing is not None and existing.active:
            return EnableOutcome(existing, None, _detail(existing))
        if existing is None:
            connection_id = self._store.create(
                context.tenant_id,
                provider_key,
                provider.display_name,
                sorted(purpose.value for purpose in chosen),
                context.subject,
            )
            existing = self._load(context.tenant_id, connection_id)
        result = self._call(lambda: provider.provision(existing, self._secrets))
        return self._apply(existing, result, context.subject, "provisioned")

    def resume(
        self,
        context: TenantContext,
        connection_id: UUID,
        action_id: str,
        submission: Mapping[str, object],
    ) -> EnableOutcome:
        """The data a pending step returned (a form's answers, a pop-up's result)."""
        context.require(Permission.MANAGE_INTEGRATIONS)
        connection = self._own(context, connection_id)
        action = self._pending(connection, action_id)
        if not action.returns_data:
            raise ActionNotFound("Not found")
        provider = self._registry.get(connection.provider_key)
        result = self._call(
            lambda: provider.resume(connection, self._secrets, action_id, submission)
        )
        return self._apply(connection, result, context.subject, "resumed")

    def pending_page(self, context: TenantContext, connection_id: UUID, action_id: str) -> str:
        """HTML of a provider_page step (contracts C7), for a signed-in administrator."""
        context.require(Permission.MANAGE_INTEGRATIONS)
        connection = self._own(context, connection_id)
        action = self._pending(connection, action_id)
        if action.kind is not PendingActionKind.PROVIDER_PAGE:
            raise ActionNotFound("Not found")
        provider = self._registry.get(connection.provider_key)
        return self._call(lambda: provider.provider_page(connection, self._secrets, action_id))

    def on_update(self, connection: Connection, update: ConnectionUpdate) -> str:
        """A setup event the provider verified (worker). Raises to retry."""
        provider = self._registry.find(connection.provider_key)
        if provider is None or connection.status is ConnectionStatus.DISABLED:
            return "ignored"
        if not _ACTION_ID.fullmatch(update.action_id):
            return "ignored"
        try:
            result = provider.resume(connection, self._secrets, update.action_id, update.data)
        except OperationUnsupported:
            return "unsupported"
        except ValueError:
            return "rejected"
        outcome = self._apply(connection, result, f"provider:{provider.key}", "resumed")
        return outcome.connection.status.value

    def poll(self, job: ClaimedJob) -> None:
        """Job handler for poll_connection, payload {"connection_id": "..."}."""
        try:
            connection_id = UUID(job.payload["connection_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid connection_id") from None
        connection = self._connections.get(job.require_tenant(), connection_id)
        if connection is None:
            raise PermanentJobError("Connection not found")
        provider = self._registry.find(connection.provider_key)
        if provider is None or connection.status is ConnectionStatus.DISABLED:
            return
        result = provider.resume(connection, self._secrets, POLL_ACTION, {})
        self._apply(connection, result, f"provider:{provider.key}", "resumed")

    def test(self, context: TenantContext, connection_id: UUID) -> TestResult:
        context.require(Permission.MANAGE_INTEGRATIONS)
        connection = self._own(context, connection_id)
        provider = self._registry.get(connection.provider_key)
        result = provider.test(connection, self._secrets)
        self._store.record_event(
            context.tenant_id,
            connection_id,
            "tested",
            context.subject,
            {"ok": result.ok, "detail": result.detail},
        )
        return result

    def disable(self, context: TenantContext, connection_id: UUID) -> tuple[str, ...]:
        """Switch a self-service tool off; returns what the business must still do itself."""
        context.require(Permission.MANAGE_INTEGRATIONS)
        connection = self._own(context, connection_id)
        provider = self._registry.get(connection.provider_key)
        steps = provider.deprovision(connection, self._secrets)
        self._store.apply(
            connection.tenant_id,
            connection.connection_id,
            ConnectionChange(
                status=ConnectionStatus.DISABLED,
                config=connection.config,
                secret_refs=connection.secret_refs,
                last_error=None,
                pending_action=None,
                event_kind="deprovisioned",
                remove_all_routes=True,
                restore_replaced=True,
            ),
            context.subject,
        )
        return steps

    # --- internals -------------------------------------------------------------------------
    def _apply(
        self, connection: Connection, result: ProvisionResult, actor: str, kind: str
    ) -> EnableOutcome:
        refs = dict(connection.secret_refs)
        replaced: list[str] = []
        stored: list[str] = []
        for name, value in result.secrets.items():
            if name in refs:
                replaced.append(refs[name])
            refs[name] = self._secrets.put(connection.tenant_id, name, value)
            stored.append(refs[name])
        status, error = _status(result)
        config = {**connection.config, **result.config}
        if result.detail is not None:
            config["public_detail"] = result.detail
        poll_at = None if result.poll_after is None else self._clock() + result.poll_after
        pending = result.pending if result.status is ProvisionStatus.NEEDS_ACTION else None
        if pending is not None and not _ACTION_ID.fullmatch(pending.action_id):
            raise ValueError("A pending step needs a short identifier")
        becomes_active = status is ConnectionStatus.ACTIVE and not connection.active
        change = ConnectionChange(
            status=status,
            config=config,
            secret_refs=refs,
            last_error=error,
            pending_action=None if pending is None else pending_to_json(pending),
            event_kind=kind,
            add_routes=result.add_routes,
            remove_routes=result.remove_routes,
            poll_at=poll_at,
            detail={"result": result.status.value},
            exclusive_purposes=_exclusive(connection) if status is ConnectionStatus.ACTIVE else (),
            jobs=self._first_jobs(connection) if becomes_active else (),
        )
        try:
            self._store.apply(connection.tenant_id, connection.connection_id, change, actor)
        except BaseException:  # RouteTaken or a database error: nothing was changed
            for reference in stored:
                self._secrets.delete(connection.tenant_id, reference)
            raise
        for reference in replaced:
            self._secrets.delete(connection.tenant_id, reference)
        updated = self._load(connection.tenant_id, connection.connection_id)
        return EnableOutcome(updated, pending, _detail(updated))

    def _first_jobs(self, connection: Connection) -> tuple[tuple[str, Mapping[str, str]], ...]:
        """A product source that copies its catalogue starts with a first copy at once."""
        provider = self._registry.find(connection.provider_key)
        abilities = None if provider is None else provider.capabilities.catalogue
        if Purpose.CATALOGUE not in connection.purposes or abilities is None or not abilities.sync:
            return ()
        payload = {"connection_id": str(connection.connection_id), "trigger": "connected"}
        return ((JobKind.SYNC_CATALOGUE.value, payload),)

    def _own(self, context: TenantContext, connection_id: UUID) -> Connection:
        connection = self._connections.get(context.tenant_id, connection_id)
        provider = None if connection is None else self._registry.find(connection.provider_key)
        if connection is None or provider is None or not provider.capabilities.self_service:
            raise ConnectionNotFound("Not found")
        return connection

    def _pending(self, connection: Connection, action_id: str) -> PendingAction:
        stored = connection.pending_action
        if stored is None or stored.get("action_id") != action_id:
            raise ActionNotFound("Not found")
        action = pending_from_json(stored)
        if action.expires_at is not None and action.expires_at <= self._clock():
            raise ActionNotFound("Not found")
        return action

    def _load(self, tenant_id: UUID, connection_id: UUID) -> Connection:
        connection = self._connections.get(tenant_id, connection_id)
        if connection is None:
            raise ConnectionNotFound("Not found")
        return connection

    @staticmethod
    def _call[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except OperationUnsupported as error:
            raise ValueError(str(error) or "This tool cannot do that") from None


# A business has one product source and one order destination (ADR 0023): enabling another
# replaces the active one.
EXCLUSIVE_PURPOSES = (Purpose.CATALOGUE, Purpose.ORDERS)


def _exclusive(connection: Connection) -> tuple[str, ...]:
    return tuple(item.value for item in EXCLUSIVE_PURPOSES if item in connection.purposes)


def _status(result: ProvisionResult) -> tuple[ConnectionStatus, str | None]:
    if result.status is ProvisionStatus.READY:
        return ConnectionStatus.ACTIVE, None
    if result.status is ProvisionStatus.FAILED:
        return ConnectionStatus.ERROR, result.error or "Setting up this tool failed"
    return ConnectionStatus.SETTING_UP, None


def _detail(connection: Connection) -> str | None:
    value = connection.config.get("public_detail")
    return None if value is None else str(value)
