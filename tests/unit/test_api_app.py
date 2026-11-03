"""Health endpoints, with the database check replaced by stubs."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from sales_platform.api_app import create_app
from sales_platform.platform.config import Settings

DATABASE_READY = "sales_platform.api_app.database_ready"


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None, database_url=SecretStr("postgresql+psycopg://app:private@localhost/db")
    )
    with TestClient(create_app(settings)) as http:
        yield http


def test_liveness_does_not_need_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object) -> bool:
        raise AssertionError("Liveness must not query the database")

    monkeypatch.setattr(DATABASE_READY, forbidden)
    assert client.get("/health/live").json() == {"status": "ok"}


def test_readiness_failure_is_sanitized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object) -> bool:
        raise OperationalError("private connection info", {}, Exception("password=private"))

    monkeypatch.setattr(DATABASE_READY, unavailable)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "private" not in response.text


@pytest.mark.parametrize(("ready", "status_code"), [(False, 503), (True, 200)])
def test_readiness_requires_valid_schema_and_role(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, ready: bool, status_code: int
) -> None:
    monkeypatch.setattr(DATABASE_READY, lambda _: ready)
    assert client.get("/health/ready").status_code == status_code
