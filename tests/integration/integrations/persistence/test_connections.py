"""Connections on PostgreSQL: isolation, the webhook lookup, audit, rules and API keys."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from sales_platform.accounts.application.api_keys import generate_api_key
from sales_platform.accounts.persistence.api_keys import insert_api_key, revoke_api_key
from sales_platform.integrations.domain.connections import ConnectionStatus, Purpose
from sales_platform.integrations.persistence.connections import (
    SqlConnectionStore,
    SqlHookLookup,
    ensure_connection,
    set_status,
)
from sales_platform.platform.domain.tenancy import Role
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.quotations.persistence.channels import SqlChannelStore, SqlNotificationStore
from sales_platform.seed import INDUSTRIAL_TENANT, OFFICE_TENANT


def demo_connection(store: SqlConnectionStore, tenant_id: UUID) -> UUID:
    found = store.find_by_account(tenant_id, "simulated", "simulated-local")
    assert found is not None
    return found.connection_id


def test_seed_gives_each_demo_business_its_product_list(runtime_engine: Engine) -> None:
    store = SqlConnectionStore(runtime_engine)
    for tenant_id in (INDUSTRIAL_TENANT, OFFICE_TENANT):
        [source] = store.catalogue_sources(tenant_id)
        assert source.provider_key == "local_catalogue" and source.active
        assert source.purposes == {Purpose.CATALOGUE}


def test_each_business_sees_only_its_own_connections(runtime_engine: Engine) -> None:
    store = SqlConnectionStore(runtime_engine)
    office = demo_connection(store, OFFICE_TENANT)
    assert store.get(INDUSTRIAL_TENANT, office) is None
    assert all(item.tenant_id == INDUSTRIAL_TENANT for item in store.list_for(INDUSTRIAL_TENANT))
    with runtime_engine.connect() as connection:  # no tenant context: nothing visible
        assert connection.execute(text("SELECT count(*) FROM connections")).scalar_one() == 0


def test_hook_lookup_reveals_only_the_named_connection(
    runtime_engine: Engine, owner_engine: Engine
) -> None:
    connection_id = demo_connection(SqlConnectionStore(runtime_engine), INDUSTRIAL_TENANT)
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        connection.execute(
            text("UPDATE connections SET webhook_token_sha256 = :hash WHERE id = :id"),
            {"hash": "a" * 64, "id": connection_id},
        )
    found = SqlHookLookup(runtime_engine).find_for_hook(connection_id)
    assert found is not None and found[0].tenant_id == INDUSTRIAL_TENANT and found[1] == "a" * 64
    assert SqlHookLookup(runtime_engine).find_for_hook(uuid4()) is None
    with runtime_engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.connection_id', :id, true)"),
                           {"id": str(connection_id)})  # fmt: skip
        assert connection.execute(text("SELECT count(*) FROM connections")).scalar_one() == 1


def test_runtime_roles_cannot_change_connections_and_events_are_append_only(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    # Since phase 2 the API may change a connection's status (revoking an API key disables its
    # connection), since phase 4 a hosted chat's settings (config) and since phase 5 both roles
    # apply one-click setup steps (status, config, secret references, pending step). Neither
    # may change what a connection is (provider, owner) or its webhook token.
    statements = {
        runtime_engine: "UPDATE connections SET webhook_token_sha256 = NULL",
        worker_engine: "UPDATE connections SET provider_key = 'api'",
    }
    for engine, statement in statements.items():
        with (
            pytest.raises(ProgrammingError),
            tenant_transaction(engine, INDUSTRIAL_TENANT) as connection,
        ):
            connection.execute(text(statement))
    with (
        pytest.raises(ProgrammingError, match="immutable"),
        tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection,
    ):
        connection.execute(text("DELETE FROM connection_events"))


def test_one_active_catalogue_source_per_business(owner_engine: Engine) -> None:
    with (
        pytest.raises(IntegrityError),
        tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection,
    ):
        ensure_connection(
            connection, tenant_id=INDUSTRIAL_TENANT, provider_key="simulated",
            account_ref="second-catalogue", display_name="Second", created_by="test",
            purposes=[Purpose.CATALOGUE],
        )  # fmt: skip


def test_api_keys_create_and_disable_their_connection(
    owner_engine: Engine, runtime_engine: Engine
) -> None:
    key = generate_api_key()
    insert_api_key(owner_engine, key.key_id, INDUSTRIAL_TENANT, "Shop site", Role.INTAKE_SERVICE,
                   key.secret_sha256)  # fmt: skip
    store = SqlConnectionStore(runtime_engine)
    created = store.find_by_account(INDUSTRIAL_TENANT, "api", f"api-key:{key.key_id}")
    assert created is not None and created.active and created.display_name == "Shop site"
    assert created.purposes == {Purpose.CUSTOMER_MESSAGES}
    assert revoke_api_key(owner_engine, INDUSTRIAL_TENANT, key.key_id)
    disabled = store.get(INDUSTRIAL_TENANT, created.connection_id)
    assert disabled is not None and disabled.status is ConnectionStatus.DISABLED
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        query = "SELECT kind FROM connection_events WHERE connection_id = :id ORDER BY created_at"
        kinds = connection.execute(text(query), {"id": created.connection_id}).scalars().all()
    assert kinds == ["created", "status_changed"]


def test_bindings_need_an_active_connection(
    owner_engine: Engine, runtime_engine: Engine, worker_engine: Engine
) -> None:
    channels = SqlChannelStore(runtime_engine)
    manager = "manager@industrial.example"
    with tenant_transaction(runtime_engine, INDUSTRIAL_TENANT) as connection:
        now = connection.execute(text("SELECT now()")).scalar_one()
    channels.create_enrollment(INDUSTRIAL_TENANT, manager, "simulated", "c" * 64,
                               now + timedelta(hours=1))  # fmt: skip
    # An account on a connection this business does not have is refused.
    assert channels.enroll(INDUSTRIAL_TENANT, "simulated", "other", "phone", "c" * 64, now) is None
    binding = channels.enroll(
        INDUSTRIAL_TENANT, "simulated", "simulated-local", "phone", "c" * 64, now
    )
    assert binding is not None
    notifications = SqlNotificationStore(worker_engine)
    assert [b.binding_id for b in notifications.approver_bindings(INDUSTRIAL_TENANT)] == [
        binding.binding_id
    ]
    connection_id = demo_connection(SqlConnectionStore(runtime_engine), INDUSTRIAL_TENANT)
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        set_status(connection, INDUSTRIAL_TENANT, connection_id, ConnectionStatus.DISABLED, "t")
    try:
        assert notifications.approver_bindings(INDUSTRIAL_TENANT) == []
    finally:
        with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
            set_status(connection, INDUSTRIAL_TENANT, connection_id, ConnectionStatus.ACTIVE, "t")
