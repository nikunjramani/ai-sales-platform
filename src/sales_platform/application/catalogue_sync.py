"""Copying a business's ERP or shop catalogue into ours on a schedule (phase 6, ADR 0023).

Purpose: quotations search our own copy (fast, our search, works while the ERP is slow) while
final prices and stock are checked live. Inputs: a sync_catalogue job naming the connection
and why it runs (connected, schedule, manual). Output: a sync run record and the stored copy.

Rules:
- only the business's active product source is copied, and only if its provider declares
  CatalogueCapabilities.sync; otherwise the job does nothing and the schedule ends (the
  source was replaced or disabled);
- every failure is recorded on the run (the last good copy stays) and the schedule goes on:
  the job itself succeeds, so a broken ERP never kills the repeating run;
- "Sync now" adds one manual run that does not repeat; at most one waits at a time.
The interval is the connection's generic `sync_minutes` config value (set by its provider).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.catalogue_sync import SyncRun, SyncTrigger, sync_minutes
from sales_platform.domain.connections import Connection
from sales_platform.domain.jobs import PermanentJobError
from sales_platform.domain.tenancy import Permission, TenantContext
from sales_platform.ports.catalogue_sync import CatalogueSyncStore
from sales_platform.ports.connections import ConnectionStore
from sales_platform.ports.erp import ErpError
from sales_platform.ports.jobs import ClaimedJob
from sales_platform.ports.providers import OperationUnsupported, Provider
from sales_platform.ports.secrets import SecretStore


def utc_now() -> datetime:
    return datetime.now(UTC)


class SyncNotAvailable(Exception):
    """The business's product source is not copied (our own list, or a live-only source)."""


@dataclass(frozen=True)
class SyncStatusView:
    connection_id: UUID
    every_minutes: int
    latest: SyncRun | None


def synced_source(
    connections: ConnectionStore, registry: ProviderRegistry, tenant_id: UUID
) -> tuple[Connection, Provider] | None:
    """The business's active product source if its provider copies its catalogue."""
    for connection in connections.catalogue_sources(tenant_id):
        provider = registry.find(connection.provider_key)
        abilities = None if provider is None else provider.capabilities.catalogue
        if provider is not None and abilities is not None and abilities.sync:
            return connection, provider
    return None


class CatalogueSyncHandler:
    """Job handler for sync_catalogue, payload {"connection_id", "trigger"}."""

    def __init__(
        self,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        secrets: SecretStore,
        store: CatalogueSyncStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._secrets = secrets
        self._store = store
        self._clock = clock

    def __call__(self, job: ClaimedJob) -> None:
        tenant_id = job.require_tenant()
        try:
            connection_id = UUID(job.payload["connection_id"])
            trigger = SyncTrigger(job.payload.get("trigger", SyncTrigger.SCHEDULE.value))
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid connection_id") from None
        source = synced_source(self._connections, self._registry, tenant_id)
        if source is None or source[0].connection_id != connection_id:
            return  # replaced, disabled or not copied any more: the schedule ends here
        connection, provider = source
        next_run_at = (
            None
            if trigger is SyncTrigger.MANUAL
            else self._clock() + timedelta(minutes=sync_minutes(connection.config))
        )
        run_id = self._store.start(tenant_id, connection_id, trigger)
        try:
            feed = provider.fetch_catalogue(connection, self._secrets)
        except (ErpError, OperationUnsupported, ValueError) as error:
            self._store.fail(tenant_id, run_id, str(error) or type(error).__name__, (), next_run_at)
            return
        self._store.apply(tenant_id, run_id, feed, f"sync:{provider.key}", next_run_at)


class CatalogueSyncService:
    """What the product screens show about the copy, and "Sync now"."""

    def __init__(
        self,
        connections: ConnectionStore,
        registry: ProviderRegistry,
        store: CatalogueSyncStore,
    ) -> None:
        self._connections = connections
        self._registry = registry
        self._store = store

    def status(self, context: TenantContext) -> SyncStatusView | None:
        """None when the business's products are not copied from anywhere."""
        context.require(Permission.VIEW_CATALOGUE)
        source = synced_source(self._connections, self._registry, context.tenant_id)
        if source is None:
            return None
        connection = source[0]
        latest = self._store.latest(context.tenant_id, connection.connection_id)
        return SyncStatusView(connection.connection_id, sync_minutes(connection.config), latest)

    def request(self, context: TenantContext) -> bool:
        """Queue a manual run; False when one is already waiting."""
        context.require(Permission.MANAGE_CATALOGUE)
        source = synced_source(self._connections, self._registry, context.tenant_id)
        if source is None:
            raise SyncNotAvailable("Your products are not copied from another system")
        return self._store.request(context.tenant_id, source[0].connection_id, context.subject)
