"""API key lookup and creation.

Each key is also an `api` connection (account_ref "api-key:<id>", ADR 0018), created and
disabled together with the key, so enquiries through the API belong to a real connection.
"""

from uuid import UUID

from sqlalchemy import Connection, Engine, func, insert, select, text, update

from sales_platform.adapters.providers.api import PROVIDER_KEY
from sales_platform.domain.connections import ConnectionStatus, Purpose
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.persistence.accounts import audit as _audit
from sales_platform.persistence.connections import ensure_connection, set_status
from sales_platform.persistence.models import ApiKey, ConnectionRow
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.ports.accounts import ApiKeySummary
from sales_platform.ports.api_keys import ApiKeyRecord

# API keys are the `api` provider's accounts: each key is one `api` connection.
API_PROVIDER_KEY = PROVIDER_KEY


def api_key_account(key_id: UUID) -> str:
    """The subject an API key acts as, which is also its connection's account_ref."""
    return f"api-key:{key_id}"


class SqlApiKeyReader:
    """Runtime-role lookup. The api_key_lookup RLS policy exposes only the requested key row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find(self, key_id: UUID) -> ApiKeyRecord | None:
        with self._engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.api_key_id', :key_id, true)"), {"key_id": str(key_id)}
            )
            row = connection.execute(
                select(
                    ApiKey.id,
                    ApiKey.tenant_id,
                    ApiKey.role,
                    ApiKey.secret_sha256,
                    ApiKey.revoked_at,
                ).where(ApiKey.id == key_id)
            ).one_or_none()
        if row is None:
            return None
        return ApiKeyRecord(
            key_id=row.id,
            tenant_id=row.tenant_id,
            role=Role(row.role),
            secret_sha256=row.secret_sha256,
            revoked=row.revoked_at is not None,
        )


def _insert(
    connection: Connection,
    key_id: UUID,
    tenant_id: UUID,
    name: str,
    role: Role,
    secret_sha256: str,
    created_by: str | None,
) -> None:
    connection.execute(
        insert(ApiKey).values(
            id=key_id,
            tenant_id=tenant_id,
            name=name,
            role=role.value,
            secret_sha256=secret_sha256,
            created_by=created_by,
        )
    )
    ensure_connection(
        connection,
        tenant_id=tenant_id,
        provider_key=API_PROVIDER_KEY,
        account_ref=api_key_account(key_id),
        display_name=name,
        purposes=[Purpose.CUSTOMER_MESSAGES],
        created_by=created_by or "api-key",
    )


def _revoke(connection: Connection, tenant_id: UUID, key_id: UUID, revoked_by: str | None) -> bool:
    """Revoke the key and disable its connection (no more intake, no more deliveries)."""
    result = connection.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=func.now(), revoked_by=revoked_by)
    )
    if result.rowcount != 1:
        return False
    connection_id = connection.execute(
        select(ConnectionRow.id).where(
            ConnectionRow.tenant_id == tenant_id,
            ConnectionRow.provider_key == API_PROVIDER_KEY,
            ConnectionRow.account_ref == api_key_account(key_id),
        )
    ).scalar_one_or_none()
    if connection_id is not None:
        set_status(
            connection, tenant_id, connection_id, ConnectionStatus.DISABLED, revoked_by or "api-key"
        )
    return True


def insert_api_key(
    owner_engine: Engine, key_id: UUID, tenant_id: UUID, name: str, role: Role, secret_sha256: str
) -> None:
    """Owner credentials, for tests and the demo tools (people use the portal, phase 2)."""
    with tenant_transaction(owner_engine, tenant_id) as connection:
        _insert(connection, key_id, tenant_id, name, role, secret_sha256, None)


def revoke_api_key(owner_engine: Engine, tenant_id: UUID, key_id: UUID) -> bool:
    with tenant_transaction(owner_engine, tenant_id) as connection:
        return _revoke(connection, tenant_id, key_id, None)


class SqlApiKeyAdminStore:
    """Keys created and revoked in the portal (runtime role, phase 2 Q17), with audit rows."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self, context: TenantContext, key_id: UUID, secret_sha256: str, name: str, role: Role
    ) -> None:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            _insert(
                connection, key_id, context.tenant_id, name, role, secret_sha256, context.subject
            )
            _audit(connection, context, "api_key_created", str(key_id), {"name": name})

    def list(self, context: TenantContext) -> list[ApiKeySummary]:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            rows = connection.execute(
                select(
                    ApiKey.id,
                    ApiKey.name,
                    ApiKey.role,
                    ApiKey.created_at,
                    ApiKey.created_by,
                    ApiKey.revoked_at,
                )
                .where(ApiKey.tenant_id == context.tenant_id)
                .order_by(ApiKey.created_at.desc())
            ).all()
        return [
            ApiKeySummary(
                row.id, row.name, Role(row.role), row.created_at, row.created_by, row.revoked_at
            )
            for row in rows
        ]

    def revoke(self, context: TenantContext, key_id: UUID) -> bool:
        with tenant_transaction(self._engine, context.tenant_id) as connection:
            if not _revoke(connection, context.tenant_id, key_id, context.subject):
                return False
            _audit(connection, context, "api_key_revoked", str(key_id))
        return True
