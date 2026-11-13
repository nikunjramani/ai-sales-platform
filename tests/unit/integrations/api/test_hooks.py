"""HTTP answers of the generic hook route (the service is tested in test_inbound.py)."""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from sales_platform.integrations.api.hooks import MAX_HOOK_BYTES, build_hook_router
from sales_platform.integrations.application.inbound import InboundService
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection
from sales_platform.platform.api.errors import add_error_handlers
from sales_platform.platform.ports.secrets import SecretNotFound
from sales_platform.providers.simulated import SimulatedProvider
from sales_platform.providers.simulated.signing import SIGNATURE_HEADER, sign

SECRET = "unit-signing-secret"
TOKEN = "unit-hook-token"


class OneSecret:
    def get(self, tenant_id: Any, reference: str) -> str:
        if reference != "local:demo":
            raise SecretNotFound("Secret not found")
        return SECRET

    def put(self, tenant_id: Any, name: str, value: str) -> str:
        raise NotImplementedError

    def delete(self, tenant_id: Any, reference: str) -> None:
        raise NotImplementedError

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        raise NotImplementedError

    def get_platform(self, provider_key: str, name: str) -> str:
        raise NotImplementedError


@pytest.fixture
def phone(harness: Any) -> Connection:
    base = harness.connections.find_by_account(harness.tenant_id, "simulated", "simulated-local")
    added: Connection = harness.connections.add(
        replace(base, secret_refs={"signing_secret": "local:demo"}),
        hashlib.sha256(TOKEN.encode()).hexdigest(),
    )
    return added


@pytest.fixture
def client(harness: Any) -> Iterator[TestClient]:
    service = InboundService(
        harness.connections,
        ProviderRegistry([SimulatedProvider(Path("unused"))]),
        OneSecret(),
        harness.inbox,
    )
    app = FastAPI()
    add_error_handlers(app)
    app.include_router(build_hook_router(service))
    with TestClient(app) as http:
        yield http


def post(client: TestClient, url: str, body: bytes, secret: str = SECRET) -> Any:
    return client.post(url, content=body, headers={SIGNATURE_HEADER: sign(secret, body)})


def test_answers(client: TestClient, phone: Connection) -> None:
    url = f"/api/v1/hooks/{phone.connection_id}/{TOKEN}"
    body = json.dumps(
        {"event_id": "evt-1", "external_user_id": "phone", "type": "enroll", "code": "X"}
    ).encode()
    first = post(client, url, body)
    assert (first.status_code, first.json()) == (200, {"received": 1, "duplicates": 0})
    assert post(client, url, body).json() == {"received": 1, "duplicates": 1}

    not_found = {"detail": "Not found"}
    for wrong in (f"/api/v1/hooks/{phone.connection_id}/wrong", f"/api/v1/hooks/{uuid4()}/{TOKEN}"):
        answer = post(client, wrong, body)
        assert (answer.status_code, answer.json()) == (404, not_found)
    forged = post(client, url, body, secret="attacker")
    assert (forged.status_code, forged.json()) == (401, {"detail": "Invalid signature"})
    garbage = post(client, url, b"not json")
    assert (garbage.status_code, garbage.json()) == (400, {"detail": "Invalid event"})
    large = post(client, url, b"x" * (16 * 1024 + 1))
    assert (large.status_code, large.json()) == (413, {"detail": "Event too large"})
    huge = post(client, url, b"x" * (MAX_HOOK_BYTES + 1))
    assert huge.status_code == 413


def test_database_trouble_asks_the_provider_to_retry(
    client: TestClient, phone: Connection, harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise OperationalError("insert", {}, Exception("down"))

    monkeypatch.setattr(harness.inbox, "record", unavailable)
    body = json.dumps(
        {"event_id": "evt-2", "external_user_id": "phone", "type": "enroll", "code": "X"}
    ).encode()
    answer = post(client, f"/api/v1/hooks/{phone.connection_id}/{TOKEN}", body)
    assert (answer.status_code, answer.json()) == (503, {"detail": "Try again later"})
