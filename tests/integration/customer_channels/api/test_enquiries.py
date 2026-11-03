"""End-to-end intake through the real app, PostgreSQL roles, local storage and the worker."""

import base64
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from sales_platform.accounts.application.api_keys import ApiKeyAuthenticator, parse_token
from sales_platform.accounts.persistence.api_keys import SqlApiKeyReader, revoke_api_key
from sales_platform.api_app import create_app
from sales_platform.enquiries.application.intake import EnquiryIntakeService
from sales_platform.enquiries.application.preparation import EnquiryPreparationHandler
from sales_platform.enquiries.domain.intake import AttachmentUpload, EnquirySubmission
from sales_platform.enquiries.persistence import intake as intake_persistence
from sales_platform.enquiries.persistence.intake import SqlIntakeStore, SqlPreparationStore
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.persistence.connections import SqlConnectionStore
from sales_platform.platform.adapters.storage.local import LocalObjectStorage
from sales_platform.platform.application.jobs import JobRunner
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.jobs import JobKind
from sales_platform.platform.domain.tenancy import Role
from sales_platform.platform.persistence.jobs import SqlJobQueue
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.providers.api import ApiProvider

type ApiKeyFactory = Callable[[UUID, Role], str]

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF"
FAKE_PDF = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8  # an image renamed to .pdf


def attachment(name: str, content_type: str, data: bytes) -> dict[str, str]:
    return {
        "filename": name,
        "content_type": content_type,
        "content_base64": base64.b64encode(data).decode(),
    }


@pytest.fixture
def client(integration_settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(integration_settings)) as http:
        yield http


def headers(token: str, key: str = "request-000001") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def count(owner_engine: Engine, tenant_id: UUID, table: str) -> int:
    with tenant_transaction(owner_engine, tenant_id) as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def stored_files(settings: Settings, pattern: str = "**/*") -> list[Path]:
    """Files under the configured storage root (temporary files excluded)."""
    return [
        path
        for path in settings.storage_dir.glob(pattern)
        if path.is_file() and not path.name.startswith(".upload-")
    ]


def test_enquiry_flows_from_api_through_worker(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    worker_engine: Engine,
    storage: LocalObjectStorage,
    integration_settings: Settings,
) -> None:
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    body = {
        "text": "Please quote 50 exhaust fans, see attached spec",
        "customer_reference": "CUST-42",
        "attachments": [
            attachment("spec.pdf", "application/pdf", PDF),
            attachment("drawing.pdf", "application/pdf", FAKE_PDF),
        ],
    }
    accepted = client.post("/api/v1/enquiries", json=body, headers=headers(token))
    assert accepted.status_code == 202
    url = f"/api/v1/enquiries/{accepted.json()['enquiry_id']}"
    before = client.get(url, headers=headers(token)).json()
    assert before["status"] == "received"
    assert {item["status"] for item in before["attachments"]} == {"quarantined"}

    runner = JobRunner(
        SqlJobQueue(worker_engine),
        {
            JobKind.PREPARE_ENQUIRY: EnquiryPreparationHandler(
                SqlPreparationStore(worker_engine), storage
            )
        },
    )
    assert runner.run_once() is True
    assert runner.run_once() is False  # the job succeeded and is not run again

    after = client.get(url, headers=headers(token)).json()
    assert after["status"] == "needs_review"
    statuses = {
        item["filename"]: (item["status"], item["rejection_reason"])
        for item in after["attachments"]
    }
    assert statuses == {"spec.pdf": ("clean", None), "drawing.pdf": ("rejected", "type_mismatch")}
    tenant = f"tenants/{industrial_tenant}"
    assert len(stored_files(integration_settings, f"{tenant}/clean/*")) == 1
    assert len(stored_files(integration_settings, f"{tenant}/quarantine/*")) == 1


def test_replay_and_conflict_store_one_enquiry(
    client: TestClient, make_api_key: ApiKeyFactory, industrial_tenant: UUID, owner_engine: Engine
) -> None:
    token = make_api_key(industrial_tenant, Role.SALES_OPERATOR)
    body = {"text": "Quote 10 desks"}
    first = client.post("/api/v1/enquiries", json=body, headers=headers(token))
    second = client.post("/api/v1/enquiries", json=body, headers=headers(token))
    conflict = client.post(
        "/api/v1/enquiries", json={"text": "Quote 11 desks"}, headers=headers(token)
    )
    assert (first.status_code, second.status_code, conflict.status_code) == (202, 200, 409)
    assert first.json()["enquiry_id"] == second.json()["enquiry_id"]
    for table in ("inbox_events", "enquiries", "jobs"):
        assert count(owner_engine, industrial_tenant, table) == 1


def test_concurrent_duplicates_create_exactly_one_enquiry(
    runtime_engine: Engine,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    owner_engine: Engine,
    storage: LocalObjectStorage,
    integration_settings: Settings,
) -> None:
    context = ApiKeyAuthenticator(SqlApiKeyReader(runtime_engine)).authenticate(
        make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    )
    service = EnquiryIntakeService(
        SqlIntakeStore(runtime_engine),
        storage,
        SqlConnectionStore(runtime_engine),
        ProviderRegistry([ApiProvider()]),
    )
    submission = EnquirySubmission(
        "Quote 5 pumps", None, (AttachmentUpload("a.txt", "text/plain", b"spec"),)
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: service.submit(context, submission, "race-000001", "api"), range(8))
        )
    assert sum(not result.duplicate for result in results) == 1
    assert len({result.receipt.enquiry_id for result in results}) == 1
    for table in ("inbox_events", "enquiries", "attachments", "jobs"):
        assert count(owner_engine, industrial_tenant, table) == 1
    assert len(stored_files(integration_settings)) == 1  # losers removed their copies


def test_failed_transaction_accepts_nothing(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    owner_engine: Engine,
    integration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the last insert of the transaction (the job) to violate a CHECK constraint.
    monkeypatch.setattr(intake_persistence, "MAX_JOB_ATTEMPTS", 0)
    token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    body = {"text": "Quote 3 chairs", "attachments": [attachment("a.txt", "text/plain", b"x")]}
    response = client.post("/api/v1/enquiries", json=body, headers=headers(token))
    assert response.status_code == 503
    for table in ("inbox_events", "enquiries", "attachments", "jobs"):
        assert count(owner_engine, industrial_tenant, table) == 0
    assert stored_files(integration_settings) == []


def test_keys_are_tenant_scoped_and_revocable(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    office_tenant: UUID,
    owner_engine: Engine,
) -> None:
    industrial = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
    office = make_api_key(office_tenant, Role.VIEWER)
    created = client.post(
        "/api/v1/enquiries", json={"text": "Quote 1 fan"}, headers=headers(industrial)
    )
    url = f"/api/v1/enquiries/{created.json()['enquiry_id']}"
    assert client.get(url, headers=headers(office)).status_code == 404
    assert (
        client.post("/api/v1/enquiries", json={"text": "x"}, headers=headers(office)).status_code
        == 403
    )

    key_id, _ = parse_token(industrial)
    assert revoke_api_key(owner_engine, industrial_tenant, key_id)
    assert client.get(url, headers=headers(industrial)).status_code == 401


def test_api_role_cannot_read_queue_or_other_tenants(
    client: TestClient,
    make_api_key: ApiKeyFactory,
    industrial_tenant: UUID,
    office_tenant: UUID,
    runtime_engine: Engine,
) -> None:
    client.post(
        "/api/v1/enquiries",
        json={"text": "Quote 1 fan"},
        headers=headers(make_api_key(industrial_tenant, Role.INTAKE_SERVICE)),
    )
    with tenant_transaction(runtime_engine, office_tenant) as connection:
        assert connection.execute(text("SELECT count(*) FROM enquiries")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM inbox_events")).scalar_one() == 0
    with pytest.raises(ProgrammingError), runtime_engine.connect() as connection:
        connection.execute(text("SELECT count(*) FROM jobs"))
