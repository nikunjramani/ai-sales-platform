"""Follow-up routing on PostgreSQL: contacts, the conversation lock, reopening and races."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from sales_platform.adapters.providers.api import ApiProvider
from sales_platform.adapters.storage.local import LocalObjectStorage
from sales_platform.application.api_keys import ApiKeyAuthenticator
from sales_platform.application.intake import EnquiryIntakeService, IntakeResult
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.intake import EnquiryStatus, EnquirySubmission
from sales_platform.domain.tenancy import Role, TenantContext
from sales_platform.persistence.api_keys import SqlApiKeyReader
from sales_platform.persistence.connections import SqlConnectionStore
from sales_platform.persistence.intake import SqlIntakeStore
from sales_platform.persistence.tenancy import tenant_transaction

type ApiKeyFactory = Callable[[UUID, Role], str]


@pytest.fixture
def service(runtime_engine: Engine, storage: LocalObjectStorage) -> EnquiryIntakeService:
    return EnquiryIntakeService(
        SqlIntakeStore(runtime_engine),
        storage,
        SqlConnectionStore(runtime_engine),
        ProviderRegistry([ApiProvider()]),
    )


@pytest.fixture
def key_context(
    runtime_engine: Engine, make_api_key: ApiKeyFactory, industrial_tenant: UUID
) -> Callable[[], TenantContext]:
    def create() -> TenantContext:
        token = make_api_key(industrial_tenant, Role.INTAKE_SERVICE)
        return ApiKeyAuthenticator(SqlApiKeyReader(runtime_engine)).authenticate(token)

    return create


def rows(owner_engine: Engine, tenant_id: UUID, query: str, **values: object) -> list[tuple]:  # type: ignore[type-arg]
    with tenant_transaction(owner_engine, tenant_id) as connection:
        return [tuple(row) for row in connection.execute(text(query), values)]


def ask_question(owner_engine: Engine, tenant_id: UUID, enquiry_id: UUID, ago: timedelta) -> None:
    """What the extraction workflow leaves behind when it asks the customer something."""
    with tenant_transaction(owner_engine, tenant_id) as connection:
        connection.execute(
            text("UPDATE enquiries SET status = 'needs_clarification' WHERE id = :e"),
            {"e": enquiry_id},
        )
        connection.execute(
            text(
                "INSERT INTO workflow_runs (id, tenant_id, enquiry_id, graph_version, status, "
                "updated_at) VALUES (gen_random_uuid(), :t, :e, 'enquiry-extraction-v1', "
                "'needs_clarification', now() - :ago)"
            ),
            {"t": tenant_id, "e": enquiry_id, "ago": ago},
        )


def send(
    service: EnquiryIntakeService, context: TenantContext, text_: str, key: str, ref: str | None
) -> IntakeResult:
    return service.submit(context, EnquirySubmission(text_, ref), key, "api")


def test_first_message_creates_contact_conversation_and_message(
    service: EnquiryIntakeService,
    key_context: Callable[[], TenantContext],
    owner_engine: Engine,
    industrial_tenant: UUID,
) -> None:
    result = send(service, key_context(), "cables price?", "request-000001", "CUST-1")
    assert not result.receipt.continued
    [(name, verified)] = rows(
        owner_engine,
        industrial_tenant,
        "SELECT c.display_name, i.verified FROM contacts c "
        "JOIN contact_identities i ON i.contact_id = c.id",
    )
    assert (name, verified) == ("CUST-1", True)
    [(conversation, contact, payload)] = rows(
        owner_engine,
        industrial_tenant,
        "SELECT e.conversation_id IS NOT NULL, e.contact_id IS NOT NULL, j.payload "
        "FROM enquiries e JOIN jobs j ON j.payload->>'enquiry_id' = e.id::text",
    )
    assert conversation and contact
    assert payload == {"enquiry_id": str(result.receipt.enquiry_id), "acknowledge": "true"}


def test_an_answer_reopens_the_waiting_enquiry_once(
    service: EnquiryIntakeService,
    key_context: Callable[[], TenantContext],
    owner_engine: Engine,
    industrial_tenant: UUID,
) -> None:
    context = key_context()
    first = send(service, context, "can you get cables price?", "request-000001", "CUST-1")
    enquiry_id = first.receipt.enquiry_id
    ask_question(owner_engine, industrial_tenant, enquiry_id, timedelta(hours=1))

    answer = send(service, context, "10 meter?", "request-000002", "CUST-1")
    assert answer.receipt.enquiry_id == enquiry_id and answer.receipt.continued
    assert answer.receipt.status is EnquiryStatus.RECEIVED
    replay = send(service, context, "10 meter?", "request-000002", "CUST-1")
    assert replay.duplicate and replay.receipt.continued
    assert replay.receipt.enquiry_id == enquiry_id

    texts = rows(
        owner_engine,
        industrial_tenant,
        "SELECT text FROM conversation_messages WHERE enquiry_id = :e ORDER BY seq",
        e=enquiry_id,
    )
    assert texts == [("can you get cables price?",), ("10 meter?",)]
    jobs = rows(
        owner_engine,
        industrial_tenant,
        "SELECT payload FROM jobs WHERE kind = 'prepare_enquiry' ORDER BY created_at",
    )
    assert jobs == [
        ({"enquiry_id": str(enquiry_id), "acknowledge": "true"},),
        ({"enquiry_id": str(enquiry_id)},),
    ]
    assert rows(owner_engine, industrial_tenant, "SELECT count(*) FROM enquiries") == [(1,)]


def test_old_questions_other_keys_and_no_reference_start_new_enquiries(
    service: EnquiryIntakeService,
    key_context: Callable[[], TenantContext],
    owner_engine: Engine,
    industrial_tenant: UUID,
) -> None:
    context, other_key = key_context(), key_context()
    first = send(service, context, "cables price?", "request-000001", "CUST-1")
    ask_question(owner_engine, industrial_tenant, first.receipt.enquiry_id, timedelta(days=7))
    assert not send(service, context, "10 meter", "request-000002", "CUST-1").receipt.continued

    waiting = send(service, other_key, "cables price?", "request-000003", "CUST-2")
    ask_question(owner_engine, industrial_tenant, waiting.receipt.enquiry_id, timedelta(hours=1))
    assert not send(service, context, "10 meter", "request-000004", "CUST-2").receipt.continued

    anonymous = send(service, context, "cables price?", "request-000005", None)
    assert not send(service, context, "10 meter", "request-000006", None).receipt.continued
    [(conversation,)] = rows(
        owner_engine,
        industrial_tenant,
        "SELECT conversation_id FROM conversation_messages WHERE enquiry_id = :e",
        e=anonymous.receipt.enquiry_id,
    )
    assert conversation is None  # Q5: no contact, no conversation


def test_quoted_and_review_enquiries_are_never_reopened(
    service: EnquiryIntakeService,
    key_context: Callable[[], TenantContext],
    owner_engine: Engine,
    industrial_tenant: UUID,
) -> None:
    context = key_context()
    first = send(service, context, "cables price?", "request-000001", "CUST-1")
    with tenant_transaction(owner_engine, industrial_tenant) as connection:
        connection.execute(
            text("UPDATE enquiries SET status = 'needs_review' WHERE id = :e"),
            {"e": first.receipt.enquiry_id},
        )
    later = send(service, context, "10 meter", "request-000002", "CUST-1")
    assert later.receipt.enquiry_id != first.receipt.enquiry_id


def test_two_answers_at_once_reopen_once(
    service: EnquiryIntakeService,
    key_context: Callable[[], TenantContext],
    owner_engine: Engine,
    industrial_tenant: UUID,
) -> None:
    context = key_context()
    first = send(service, context, "cables price?", "request-000001", "CUST-1")
    ask_question(owner_engine, industrial_tenant, first.receipt.enquiry_id, timedelta(hours=1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda n: send(service, context, f"answer {n}", f"request-00010{n}", "CUST-1"),
                range(4),
            )
        )
    assert {result.receipt.enquiry_id for result in results} == {first.receipt.enquiry_id}
    assert rows(owner_engine, industrial_tenant, "SELECT count(*) FROM enquiries") == [(1,)]
    assert rows(owner_engine, industrial_tenant, "SELECT count(*) FROM contacts") == [(1,)]
    assert rows(owner_engine, industrial_tenant, "SELECT count(*) FROM conversation_messages") == [
        (5,)
    ]
    # Only the first answer found it waiting; the others joined it while it was being read.
    reopen_jobs = rows(
        owner_engine,
        industrial_tenant,
        "SELECT count(*) FROM jobs WHERE kind = 'prepare_enquiry' AND NOT payload ? 'acknowledge'",
    )
    assert reopen_jobs == [(1,)]


def test_first_messages_of_a_new_customer_at_once_create_one_contact(
    service: EnquiryIntakeService,
    key_context: Callable[[], TenantContext],
    owner_engine: Engine,
    industrial_tenant: UUID,
) -> None:
    context = key_context()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda n: send(service, context, f"need {n} fans", f"request-00020{n}", "NEW-1"),
                range(4),
            )
        )
    assert rows(owner_engine, industrial_tenant, "SELECT count(*) FROM contacts") == [(1,)]
    assert rows(owner_engine, industrial_tenant, "SELECT count(*) FROM conversations") == [(1,)]
    # Q3: all four arrive while the first is still being read, so they form one enquiry.
    assert rows(owner_engine, industrial_tenant, "SELECT count(*) FROM enquiries") == [(1,)]
