from fastapi.testclient import TestClient
from sqlalchemy import Engine

from sales_platform.api_app import create_app
from sales_platform.platform.config import Settings


def test_readiness_with_real_database(
    integration_settings: Settings, runtime_engine: Engine
) -> None:
    with TestClient(create_app(integration_settings)) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
