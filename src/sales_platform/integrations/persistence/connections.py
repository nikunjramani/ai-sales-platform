"""Connection rows (ADR 0018): tenant-scoped reads, the webhook lookup and owner-only writes.

Nothing here stores or returns secret values; `secret_refs` holds references into the
SecretStore. In phase 1 only the owner role creates connections (API keys, seed); phase 5 adds
self-service enabling with its own grants.
"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ColumnElement,
    Engine,
    Row,
    String,
    and_,
    cast,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy import Connection as DbConnection
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from sales_platform.integrations.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.integrations.domain.hosted_chat import handle_from_name
from sales_platform.integrations.persistence.tables import ConnectionEventRow, ConnectionRow
from sales_platform.platform.persistence.models import Tenant
from sales_platform.platform.persistence.tenancy import tenant_transaction

_COLUMNS = (
    ConnectionRow.id,
    ConnectionRow.tenant_id,
    ConnectionRow.provider_key,
    ConnectionRow.display_name,
    ConnectionRow.purposes,
    ConnectionRow.config,
    ConnectionRow.secret_refs,
    ConnectionRow.account_ref,
    ConnectionRow.status,
    ConnectionRow.last_error,
    ConnectionRow.created_by,
    ConnectionRow.created_at,
    ConnectionRow.updated_at,
    ConnectionRow.public_handle,
    ConnectionRow.public_handle_changed_at,
    ConnectionRow.pending_action,
)


def _connection(row: Row[Any]) -> Connection:
    return Connection(
        connection_id=row.id,
        tenant_id=row.tenant_id,
        provider_key=row.provider_key,
        display_name=row.display_name,
        purposes=frozenset(Purpose(value) for value in row.purposes if value in Purpose),
        config=dict(row.config),
        secret_refs=dict(row.secret_refs),
        account_ref=row.account_ref,
        status=ConnectionStatus(row.status),
        last_error=row.last_error,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        public_handle=row.public_handle,
        public_handle_changed=row.public_handle_changed_at is not None,
        pending_action=None if row.pending_action is None else dict(row.pending_action),
    )


def matches_key(provider_key: str, legacy_key: str) -> ColumnElement[bool]:
    """Find a connection by provider and its legacy key (account_ref, or the ID without one)."""
    return and_(
        ConnectionRow.provider_key == provider_key,
        or_(
            ConnectionRow.account_ref == legacy_key,
            and_(
                ConnectionRow.account_ref.is_(None),
                cast(ConnectionRow.id, String) == legacy_key,
            ),
        ),
    )


class SqlConnectionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, tenant_id: UUID, connection_id: UUID) -> Connection | None:
        return self._one(tenant_id, ConnectionRow.id == connection_id)

    def find_by_account(
        self, tenant_id: UUID, provider_key: str, account_ref: str
    ) -> Connection | None:
        return self._one(
            tenant_id,
            ConnectionRow.provider_key == provider_key,
            ConnectionRow.account_ref == account_ref,
        )

    def list_for(self, tenant_id: UUID, purpose: Purpose | None = None) -> list[Connection]:
        query = (
            select(*_COLUMNS)
            .where(ConnectionRow.tenant_id == tenant_id)
            .order_by(ConnectionRow.created_at, ConnectionRow.id)
        )
        if purpose is not None:
            query = query.where(ConnectionRow.purposes.contains([purpose.value]))
        with tenant_transaction(self._engine, tenant_id) as connection:
            return [_connection(row) for row in connection.execute(query).all()]

    def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
        return [
            item
            for item in self.list_for(tenant_id, Purpose.CATALOGUE)
            if item.status is ConnectionStatus.ACTIVE
        ]

    def _one(self, tenant_id: UUID, *conditions: Any) -> Connection | None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            row = connection.execute(
                select(*_COLUMNS).where(ConnectionRow.tenant_id == tenant_id, *conditions)
            ).one_or_none()
        return None if row is None else _connection(row)


class SqlHookLookup:
    """Runtime-role lookup by ID only; the connection_hook_lookup policy reveals that row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_for_hook(self, connection_id: UUID) -> tuple[Connection, str | None] | None:
        with self._engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.connection_id', :id, true)"),
                {"id": str(connection_id)},
            )
            row = connection.execute(
                select(*_COLUMNS, ConnectionRow.webhook_token_sha256).where(
                    ConnectionRow.id == connection_id
                )
            ).one_or_none()
        if row is None:
            return None
        return _connection(row), row.webhook_token_sha256


def record_event(
    connection: DbConnection,
    tenant_id: UUID,
    connection_id: UUID,
    kind: str,
    actor: str,
    detail: Mapping[str, object] | None = None,
) -> None:
    connection.execute(
        insert(ConnectionEventRow).values(
            id=uuid4(),
            tenant_id=tenant_id,
            connection_id=connection_id,
            kind=kind,
            actor=actor,
            detail=dict(detail or {}),
        )
    )


def ensure_connection(
    connection: DbConnection,
    *,
    tenant_id: UUID,
    provider_key: str,
    account_ref: str,
    display_name: str,
    purposes: Sequence[Purpose],
    created_by: str,
    config: Mapping[str, object] | None = None,
) -> UUID:
    """Owner-only upsert on (tenant, provider, account): creates it active, or merges purposes.

    Runs inside the caller's transaction with the tenant context already set.
    """
    created = connection.execute(
        insert(ConnectionRow)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            provider_key=provider_key,
            display_name=display_name,
            purposes=sorted(purpose.value for purpose in purposes),
            config=dict(config or {}),
            account_ref=account_ref,
            status=ConnectionStatus.ACTIVE.value,
            created_by=created_by,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "provider_key", "account_ref"])
        .returning(ConnectionRow.id)
    ).scalar_one_or_none()
    if created is not None:
        record_event(connection, tenant_id, created, "created", created_by)
        return created
    row = connection.execute(
        select(ConnectionRow.id, ConnectionRow.purposes, ConnectionRow.config).where(
            ConnectionRow.tenant_id == tenant_id,
            ConnectionRow.provider_key == provider_key,
            ConnectionRow.account_ref == account_ref,
        )
    ).one()
    wanted = sorted(set(row.purposes) | {purpose.value for purpose in purposes})
    merged = {**row.config, **(config or {})}
    if wanted != sorted(row.purposes) or merged != row.config:
        connection.execute(
            update(ConnectionRow)
            .where(ConnectionRow.tenant_id == tenant_id, ConnectionRow.id == row.id)
            .values(purposes=wanted, config=merged, updated_at=text("now()"))
        )
    existing: UUID = row.id
    return existing


def set_status(
    connection: DbConnection,
    tenant_id: UUID,
    connection_id: UUID,
    status: ConnectionStatus,
    actor: str,
) -> None:
    connection.execute(
        update(ConnectionRow)
        .where(ConnectionRow.tenant_id == tenant_id, ConnectionRow.id == connection_id)
        .values(status=status.value, updated_at=text("now()"))
    )
    record_event(connection, tenant_id, connection_id, "status_changed", actor, {"to": status})


class SqlHostedChatAdmin:
    """Enabling and configuring a hosted chat in the portal (API role, phase 4)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        tenant_id: UUID,
        provider_key: str,
        display_name: str,
        purposes: Sequence[str],
        config: Mapping[str, object],
        actor: str,
    ) -> UUID:
        """A new active connection whose handle comes from the business name; a number is
        added when the handle is taken by another business (Q13)."""
        connection_id = uuid4()
        with tenant_transaction(self._engine, tenant_id) as connection:
            name = connection.execute(
                select(Tenant.name).where(Tenant.id == tenant_id)
            ).scalar_one()
            connection.execute(
                insert(ConnectionRow).values(
                    id=connection_id,
                    tenant_id=tenant_id,
                    provider_key=provider_key,
                    display_name=display_name,
                    purposes=list(purposes),
                    config=dict(config),
                    status=ConnectionStatus.ACTIVE.value,
                    created_by=actor,
                )
            )
            candidate = _assign_handle(connection, tenant_id, connection_id, name)
            _event(connection, tenant_id, connection_id, "created", actor, {"handle": candidate})
        return connection_id

    def ensure_handle(self, tenant_id: UUID, connection_id: UUID, actor: str) -> None:
        """Give an existing chat without an address one (e.g. after a downgrade/upgrade)."""
        with tenant_transaction(self._engine, tenant_id) as connection:
            name = connection.execute(
                select(Tenant.name).where(Tenant.id == tenant_id)
            ).scalar_one()
            candidate = _assign_handle(connection, tenant_id, connection_id, name)
            _event(
                connection, tenant_id, connection_id, "settings_changed", actor,
                {"handle": candidate},
            )  # fmt: skip

    def change_handle(self, tenant_id: UUID, connection_id: UUID, handle: str, actor: str) -> bool:
        """Once only (Q13). False if the handle is taken (by anyone)."""
        with tenant_transaction(self._engine, tenant_id) as connection:
            if not _try_handle(connection, tenant_id, connection_id, handle, changing=True):
                return False
            _event(
                connection, tenant_id, connection_id, "handle_changed", actor, {"handle": handle}
            )
        return True

    def update_config(
        self, tenant_id: UUID, connection_id: UUID, config: Mapping[str, object], actor: str
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                update(ConnectionRow)
                .where(ConnectionRow.tenant_id == tenant_id, ConnectionRow.id == connection_id)
                .values(config=dict(config), updated_at=func.now())
            )
            _event(connection, tenant_id, connection_id, "settings_changed", actor, {})

    def set_status(
        self, tenant_id: UUID, connection_id: UUID, status: ConnectionStatus, actor: str
    ) -> None:
        with tenant_transaction(self._engine, tenant_id) as connection:
            connection.execute(
                update(ConnectionRow)
                .where(ConnectionRow.tenant_id == tenant_id, ConnectionRow.id == connection_id)
                .values(status=status.value, updated_at=func.now())
            )
            _event(
                connection,
                tenant_id,
                connection_id,
                "status_changed",
                actor,
                {"status": status.value},
            )


def _assign_handle(
    connection: DbConnection, tenant_id: UUID, connection_id: UUID, business_name: str
) -> str:
    base = handle_from_name(business_name)
    for attempt in range(1, 100):
        candidate = base if attempt == 1 else f"{base[:58]}-{attempt}"
        if _try_handle(connection, tenant_id, connection_id, candidate):
            return candidate
    raise RuntimeError("No free chat address")


def _try_handle(
    connection: DbConnection,
    tenant_id: UUID,
    connection_id: UUID,
    handle: str,
    changing: bool = False,
) -> bool:
    """Set the handle under a savepoint, so a taken one does not end the transaction."""
    values: dict[str, object] = {"public_handle": handle}
    if changing:
        values["public_handle_changed_at"] = func.now()
    query = update(ConnectionRow).where(
        ConnectionRow.tenant_id == tenant_id, ConnectionRow.id == connection_id
    )
    if changing:
        query = query.where(ConnectionRow.public_handle_changed_at.is_(None))
    try:
        with connection.begin_nested():
            return bool(connection.execute(query.values(**values)).rowcount)
    except IntegrityError:
        return False


def _event(
    connection: DbConnection,
    tenant_id: UUID,
    connection_id: UUID,
    kind: str,
    actor: str,
    detail: Mapping[str, object],
) -> None:
    connection.execute(
        insert(ConnectionEventRow).values(
            id=uuid4(),
            tenant_id=tenant_id,
            connection_id=connection_id,
            kind=kind,
            actor=actor,
            detail=dict(detail),
        )
    )
