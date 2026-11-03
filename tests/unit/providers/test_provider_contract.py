"""The Provider contract (ADR 0018): every installed provider, plus a fake third one, passes.

Adding a provider means adding its sample here (a test fails until it exists). The fake "echo"
provider is wired only through the registry and the generic services, proving that a new tool
needs no core change.
"""

import hashlib
import hmac
import json
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from sales_platform.catalogue.ports.erp import ProductSource
from sales_platform.catalogue.ports.sources import product_source_of
from sales_platform.integrations.application.inbound import InboundService
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import (
    Capabilities,
    Connection,
    ConnectionStatus,
    DeliveryMode,
    Purpose,
)
from sales_platform.integrations.domain.events import InboundEvent, ManagerAction
from sales_platform.integrations.ports.providers import (
    InboundRejected,
    InboundRequest,
    OperationUnsupported,
    Provider,
    ProviderDefaults,
    ProviderEnvironment,
    ReceiptKind,
    RenderedMessage,
    SendReceipt,
    TestResult,
)
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.platform.ports.secrets import SecretNotFound
from sales_platform.seed import DEMO_CATALOGUES
from tests.support.catalogue_store import InMemoryCatalogueStore

NOW = datetime(2026, 10, 27, tzinfo=UTC)
A = UUID("00000000-0000-4000-8000-00000000000a")
B = UUID("00000000-0000-4000-8000-00000000000b")
SECRET = "contract-signing-secret"


class OneSecret:
    """Every tenant's "signing" reference resolves to the same synthetic secret."""

    def get(self, tenant_id: UUID, reference: str) -> str:
        if reference != "local:signing":
            raise SecretNotFound("Secret not found")
        return SECRET

    def put(self, tenant_id: UUID, name: str, value: str) -> str:
        raise NotImplementedError

    def delete(self, tenant_id: UUID, reference: str) -> None:
        raise NotImplementedError

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        raise NotImplementedError

    def get_platform(self, provider_key: str, name: str) -> str:
        raise NotImplementedError


class EchoProvider(ProviderDefaults):
    """A fake third provider: HMAC header "x-echo-signature", body {"id", "from", "code"}."""

    key = "echo"
    display_name = "Echo (test)"
    purposes = frozenset({Purpose.MANAGER_APPROVALS})
    capabilities = Capabilities(webhooks=True)
    dev_only = False

    def test(self, conn: Connection, secrets: Any) -> TestResult:
        return TestResult(ok=True)

    def verify_inbound(self, conn: Connection, secrets: Any, request: InboundRequest) -> None:
        secret = secrets.get(conn.tenant_id, conn.secret_refs.get("signing_secret", ""))
        expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("x-echo-signature", "")):
            raise InboundRejected("Invalid signature")

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        data = json.loads(request.body)
        return [ManagerAction(data["id"], data["from"], "enroll", code=data["code"])]

    def send(self, conn: Connection, secrets: Any, message: RenderedMessage) -> SendReceipt:
        return SendReceipt(ReceiptKind.SENT, str(message.message_id))

    def catalogue_reader(self, conn: Connection, secrets: Any) -> ProductSource:
        raise OperationUnsupported("Echo has no products")


@dataclass(frozen=True)
class Sample:
    """A body this provider accepts, and how to sign it (None: the provider has no webhooks).

    Platform-webhook providers (phase 5) sign with company-level secrets instead
    (platform_sign, platform_secrets). network: sending calls the tool's real API, so the
    send outcome mapping is tested in the package's own tests with recorded answers.
    config and secret_refs: what an enabled connection of this provider holds (phase 6).
    """

    body: bytes
    sign: Callable[[bytes, str], dict[str, str]] | None
    platform_sign: Callable[[bytes, str], dict[str, str]] | None = None
    platform_secrets: dict[str, str] = field(default_factory=dict)
    network: bool = False
    config: dict[str, object] = field(default_factory=dict)
    secret_refs: dict[str, str] = field(default_factory=dict)


class SampleSecrets(OneSecret):
    """OneSecret plus the sample's company-level secrets."""

    def __init__(self, platform: dict[str, str]) -> None:
        self._platform = platform

    def get_platform(self, provider_key: str, name: str) -> str:
        try:
            return self._platform[name]
        except KeyError:
            raise SecretNotFound("Secret not found") from None


def _hmac_header(name: str) -> Callable[[bytes, str], dict[str, str]]:
    def sign(body: bytes, secret: str) -> dict[str, str]:
        return {name: hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}

    return sign


SAMPLES: dict[str, Sample] = {
    "api": Sample(b"{}", None),
    "simulated": Sample(
        json.dumps(
            {"event_id": "evt-1", "external_user_id": "phone", "type": "enroll", "code": "X"}
        ).encode(),
        _hmac_header("x-simulated-signature"),
    ),
    "local_catalogue": Sample(b"{}", None),
    "web_chat": Sample(b"{}", None),
    "telegram": Sample(
        json.dumps(
            {
                "update_id": 7,
                "business_message": {
                    "message_id": 3,
                    "business_connection_id": "bc-1",
                    "chat": {"id": 15005550101, "type": "private"},
                    "from": {"id": 15005550101, "is_bot": False},
                    "date": 1793000000,
                    "text": "Please quote 2 fans",
                },
            }
        ).encode(),
        None,
        platform_sign=lambda body, secret: {"x-telegram-bot-api-secret-token": secret},
        platform_secrets={
            "config": json.dumps(
                {
                    "bot_token": "123456:contract-token",
                    "bot_username": "contract_bot",
                    "webhook_secret_token": SECRET,
                }
            )
        },
        network=True,
    ),
    "erpnext": Sample(
        b"{}",
        None,
        network=True,
        config={
            "erpnext": {
                "base_url": "https://erp.example",
                "price_list": "Standard Selling",
                "warehouses": ["Stores - EX"],
            }
        },
        secret_refs={"api_key": "local:signing", "api_secret": "local:signing"},
    ),
    "echo": Sample(
        json.dumps({"id": "evt-1", "from": "phone", "code": "X"}).encode(),
        _hmac_header("x-echo-signature"),
    ),
}


def installed() -> list[Provider]:
    # Providers may write files (the simulated outbox): a throwaway folder, never var/.
    environment = ProviderEnvironment(
        "development",
        Path(tempfile.mkdtemp(prefix="contract-")),
        catalogue_store=InMemoryCatalogueStore.from_demo({A: DEMO_CATALOGUES["industrial"]}),
    )
    registry = ProviderRegistry.load(environment)
    return [*registry.all(), EchoProvider()]


PROVIDERS = installed()


def connection(provider: Provider, tenant_id: UUID = A) -> Connection:
    sample = SAMPLES[provider.key]
    return Connection(
        uuid4(), tenant_id, provider.key, provider.display_name, provider.purposes,
        {"catalogue": "industrial", **sample.config},
        {"signing_secret": "local:signing", **sample.secret_refs}, "account-1",
        ConnectionStatus.ACTIVE, None, "test", NOW, NOW,
    )  # fmt: skip


def request(body: bytes, headers: dict[str, str] | None = None) -> InboundRequest:
    return InboundRequest(headers or {}, body, {}, NOW)


@pytest.fixture(params=PROVIDERS, ids=lambda provider: provider.key)
def provider(request: pytest.FixtureRequest) -> Provider:
    found: Provider = request.param
    return found


def test_every_installed_provider_has_a_contract_sample() -> None:
    names = {entry.name for entry in entry_points(group="sales_platform.providers")}
    assert names <= set(SAMPLES), f"add contract samples for {sorted(names - set(SAMPLES))}"


def test_webhooks_refuse_forged_or_missing_signatures(provider: Provider) -> None:
    sample = SAMPLES[provider.key]
    conn = connection(provider)
    if provider.capabilities.platform_webhooks:
        assert sample.platform_sign is not None
        secrets = SampleSecrets(sample.platform_secrets)
        for headers in ({}, sample.platform_sign(sample.body, "forged-secret-token")):
            with pytest.raises(InboundRejected):
                provider.verify_platform_inbound(secrets, request(sample.body, headers))
        with pytest.raises(InboundRejected):  # without company settings nothing is accepted
            provider.verify_platform_inbound(
                SampleSecrets({}), request(sample.body, sample.platform_sign(sample.body, SECRET))
            )
        provider.verify_platform_inbound(
            secrets, request(sample.body, sample.platform_sign(sample.body, SECRET))
        )
        assert provider.routing_keys(request(sample.body))  # the request names its business
    if not provider.capabilities.webhooks:
        with pytest.raises(OperationUnsupported):
            provider.verify_inbound(conn, OneSecret(), request(sample.body))
        if not provider.capabilities.platform_webhooks:
            with pytest.raises(OperationUnsupported):
                provider.parse_inbound(conn, request(sample.body))
        return
    assert sample.sign is not None
    for headers in ({}, sample.sign(sample.body, "forged-secret")):
        with pytest.raises(InboundRejected):
            provider.verify_inbound(conn, OneSecret(), request(sample.body, headers))
    provider.verify_inbound(
        conn, OneSecret(), request(sample.body, sample.sign(sample.body, SECRET))
    )


def test_parsing_is_stable_and_never_depends_on_the_business(provider: Provider) -> None:
    if not (provider.capabilities.webhooks or provider.capabilities.platform_webhooks):
        pytest.skip("no webhooks")
    body = SAMPLES[provider.key].body
    first = provider.parse_inbound(connection(provider, A), request(body))
    again = provider.parse_inbound(connection(provider, A), request(body))
    other_business = provider.parse_inbound(connection(provider, B), request(body))
    assert first == again == other_business  # same IDs; the business comes from the connection
    assert all(not hasattr(event, "tenant_id") for event in first)


def test_send_reports_what_happened(provider: Provider) -> None:
    message = RenderedMessage(uuid4(), "key-1", "recipient-1", "text", {"notice_id": str(uuid4())})
    if Purpose.MANAGER_APPROVALS not in provider.purposes and (
        Purpose.CUSTOMER_MESSAGES not in provider.purposes
    ):
        pytest.skip("sends nothing")
    if SAMPLES[provider.key].network:
        pytest.skip("sends through the tool's API: see the package's recorded-answer tests")
    receipt = provider.send(connection(provider), OneSecret(), message)
    pull = provider.capabilities.delivery_mode is DeliveryMode.PULL
    assert receipt.kind is (ReceiptKind.AVAILABLE if pull else ReceiptKind.SENT)


def test_catalogue_reader_exists_exactly_for_catalogue_providers(provider: Provider) -> None:
    conn = connection(provider)
    if Purpose.CATALOGUE not in provider.purposes:
        with pytest.raises(OperationUnsupported):
            product_source_of(provider).catalogue_reader(conn, OneSecret())
        return
    reader = product_source_of(provider).catalogue_reader(conn, OneSecret())
    context = TenantContext(A, "contract", frozenset({Role.VIEWER}))
    assert list(reader.list_categories(context))  # a working reader (ERP contract suite: WI-10)


def test_a_new_provider_works_through_the_generic_services(harness: Any) -> None:
    """The fake third provider receives an enrollment on the generic hook with no core change."""
    echo = EchoProvider()
    token = "echo-token"
    conn = harness.connections.add(
        connection(echo, harness.tenant_id), hashlib.sha256(token.encode()).hexdigest()
    )
    code = harness.channels.start_enrollment(
        harness.context("manager@industrial.example"), "", conn.connection_id
    ).code
    body = json.dumps({"id": "evt-9", "from": "echo-user", "code": code}).encode()
    service = InboundService(
        harness.connections, ProviderRegistry([echo]), OneSecret(), harness.inbox
    )
    service.receive(
        conn.connection_id, token, request(body, _hmac_header("x-echo-signature")(body, SECRET))
    )
    harness.run_jobs()
    [row] = harness.inbox.rows.values()
    assert row.outcome == "enrolled"
