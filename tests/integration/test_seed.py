from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from sales_platform.platform.config import Settings
from sales_platform.seed import DEMO_TENANTS, seed_demo_tenants


def test_seed_is_idempotent_and_tenants_are_isolated(
    integration_settings: Settings, runtime_engine: Engine
) -> None:
    seed_demo_tenants(integration_settings)  # runtime_engine already seeded once
    with runtime_engine.connect() as connection:
        assert list(connection.execute(text("SELECT id FROM tenants")).scalars()) == []
    for tenant_id, _ in DEMO_TENANTS:
        with runtime_engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :id, true)"), {"id": str(tenant_id)}
            )
            ids: list[UUID] = list(connection.execute(text("SELECT id FROM tenants")).scalars())
            assert ids == [tenant_id]
    # A reused pooled connection cannot retain the previous transaction's tenant.
    with runtime_engine.connect() as connection:
        assert list(connection.execute(text("SELECT id FROM tenants")).scalars()) == []
    # Phase 2: settings may rename the current business, but nothing is reachable without one,
    # and businesses are only created through create_business().
    with runtime_engine.connect() as connection, connection.begin() as transaction:
        changed = connection.execute(text("UPDATE tenants SET name = 'forbidden'")).rowcount
        transaction.rollback()
    assert changed == 0
    with pytest.raises(ProgrammingError), runtime_engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id, name) VALUES (gen_random_uuid(), 'x')"))
