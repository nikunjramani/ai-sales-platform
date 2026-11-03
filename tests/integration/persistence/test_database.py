from sqlalchemy import Engine

from sales_platform.persistence.database import database_ready


def test_runtime_role_is_ready(runtime_engine: Engine) -> None:
    assert database_ready(runtime_engine)


def test_owner_is_not_a_valid_runtime_role(owner_engine: Engine) -> None:
    assert not database_ready(owner_engine)
