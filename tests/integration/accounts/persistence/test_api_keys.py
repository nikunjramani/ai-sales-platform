from collections.abc import Callable
from uuid import UUID

from sqlalchemy import Engine, text

from sales_platform.accounts.application.api_keys import parse_token
from sales_platform.accounts.persistence.api_keys import SqlApiKeyReader
from sales_platform.platform.domain.tenancy import Role

type ApiKeyFactory = Callable[[UUID, Role], str]


def test_lookup_exposes_only_the_presented_key(
    runtime_engine: Engine,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    office_tenant: UUID,
) -> None:
    industrial_id, _ = parse_token(make_api_key(industrial_tenant, Role.INTAKE_SERVICE))
    make_api_key(office_tenant, Role.VIEWER)

    with runtime_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM api_keys")).scalar_one() == 0
    with runtime_engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.api_key_id', :id, true)"), {"id": str(industrial_id)}
        )
        rows = connection.execute(text("SELECT id FROM api_keys")).scalars().all()
        assert rows == [industrial_id]

    record = SqlApiKeyReader(runtime_engine).find(industrial_id)
    assert record is not None and record.tenant_id == industrial_tenant and not record.revoked
