"""Shared test configuration and fixtures for every test folder.

Layout: tests/unit mirrors src/sales_platform and runs offline. Every test under
tests/integration is marked `integration` automatically and runs only with --run-integration
plus explicit database URLs (see tests/integration/conftest.py).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from sales_platform.accounts.ports.api_keys import ApiKeyRecord
from sales_platform.catalogue.adapters.erp.simulated import SimulatedErp, demo_catalogues
from sales_platform.enquiries.domain.attachments import AttachmentStatus
from sales_platform.enquiries.domain.follow_ups import OpenEnquiry
from sales_platform.enquiries.domain.intake import EnquiryStatus, InboundEnvelope
from sales_platform.enquiries.ports.intake import (
    AttachmentView,
    EnquiryRouter,
    EnquiryView,
    Receipt,
)
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.platform.domain.money import RoundingMode
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.platform.ports.storage import ObjectNotFound
from sales_platform.providers.api import ApiProvider
from sales_platform.quotations.domain.pricing import PricingPolicy
from sales_platform.quotations.ports.pricing_policy import PricingPolicyNotConfigured
from sales_platform.seed import INDUSTRIAL_TENANT, OFFICE_TENANT

INTEGRATION_DIR = Path(__file__).parent / "integration"

type ContextFactory = Callable[[UUID, Role], TenantContext]
type PolicyFactory = Callable[[UUID, str], PricingPolicy]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration", action="store_true", help="Use a disposable PostgreSQL DB"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_integration = config.getoption("--run-integration")
    skip = pytest.mark.skip(reason="Pass --run-integration with test DB URLs")
    for item in items:
        if item.path.is_relative_to(INTEGRATION_DIR):
            item.add_marker(pytest.mark.integration)
        if "integration" in item.keywords and not run_integration:
            item.add_marker(skip)


class FakeMemberships:
    """In-memory MembershipReader. Tests add rows: {(tenant_id, subject): roles}."""

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str], frozenset[Role]] = {}

    def roles_for(self, tenant_id: UUID, subject: str) -> frozenset[Role]:
        return self.rows.get((tenant_id, subject), frozenset())


class FakePolicies:
    """In-memory PricingPolicyReader keyed by tenant. Tests may replace or remove entries."""

    def __init__(self, policies: dict[UUID, PricingPolicy]) -> None:
        self.policies = policies

    def get(self, context: TenantContext) -> PricingPolicy:
        try:
            return self.policies[context.tenant_id]
        except KeyError:
            raise PricingPolicyNotConfigured("No pricing policy for this tenant") from None


@pytest.fixture
def now() -> datetime:
    """The fixed 'current time' used by clocks in unit tests."""
    return datetime(2026, 10, 22, 10, 0, tzinfo=UTC)


@pytest.fixture
def industrial_tenant() -> UUID:
    return INDUSTRIAL_TENANT


@pytest.fixture
def office_tenant() -> UUID:
    return OFFICE_TENANT


@pytest.fixture
def make_context() -> ContextFactory:
    def build(tenant_id: UUID, role: Role) -> TenantContext:
        return TenantContext(tenant_id, f"{role}@example.test", frozenset({role}))

    return build


@pytest.fixture
def make_policy() -> PolicyFactory:
    """Synthetic policy: 2 decimals, half_up, 18% tax, 15 min price and 5 min stock age."""

    def build(tenant_id: UUID, currency: str) -> PricingPolicy:
        return PricingPolicy(
            tenant_id=tenant_id,
            version=1,
            currency=currency,
            minor_units=2,
            rounding=RoundingMode.HALF_UP,
            tax_rate=Decimal("0.18"),
            max_price_age=timedelta(minutes=15),
            max_stock_age=timedelta(minutes=5),
        )

    return build


@pytest.fixture
def simulated_erp(now: datetime, industrial_tenant: UUID, office_tenant: UUID) -> SimulatedErp:
    """Demo catalogues for both tenants, with the ERP clock fixed at `now`."""
    return SimulatedErp(demo_catalogues(industrial_tenant, office_tenant), clock=lambda: now)


@pytest.fixture
def membership_reader() -> FakeMemberships:
    return FakeMemberships()


@pytest.fixture
def policy_reader(
    make_policy: PolicyFactory, industrial_tenant: UUID, office_tenant: UUID
) -> FakePolicies:
    return FakePolicies(
        {
            industrial_tenant: make_policy(industrial_tenant, "INR"),
            office_tenant: make_policy(office_tenant, "EUR"),
        }
    )


class InMemoryStorage:
    """ObjectStorage fake. `fail_puts` makes the next put raise OSError."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_puts = False

    def put(self, key: str, data: bytes) -> None:
        if self.fail_puts:
            raise OSError("disk full")
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectNotFound(key) from None

    def exists(self, key: str) -> bool:
        return key in self.objects

    def move(self, source: str, destination: str) -> None:
        self.objects[destination] = self.get(source)
        del self.objects[source]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class FakeIntakeStore:
    """IntakeStore fake keyed like the real unique constraint.

    `fail_record` raises from record(); `concurrent_winner` simulates losing a duplicate race.
    """

    def __init__(self) -> None:
        self.receipts: dict[tuple[UUID, UUID, str], Receipt] = {}
        self.envelopes: dict[UUID, InboundEnvelope] = {}
        self.fail_record: Exception | None = None
        self.concurrent_winner: Receipt | None = None
        self.statuses: dict[UUID, EnquiryStatus] = {}
        self.asked: dict[UUID, datetime] = {}  # enquiry -> when our question was recorded
        self.follow_ups: dict[UUID, list[InboundEnvelope]] = {}

    def find_receipt(
        self, tenant_id: UUID, connection_ref: UUID, provider_event_id: str
    ) -> Receipt | None:
        return self.receipts.get((tenant_id, connection_ref, provider_event_id))

    def record(
        self,
        envelope: InboundEnvelope,
        payload_sha256: str,
        enquiry_id: UUID,
        router: EnquiryRouter | None = None,
    ) -> tuple[Receipt, bool]:
        """Open enquiries are those of the same (connection, sender) still in `statuses`
        (default received); tests set statuses/asked to simulate later workflow steps."""
        if self.fail_record is not None:
            raise self.fail_record
        if self.concurrent_winner is not None:
            return self.concurrent_winner, False
        key = (envelope.tenant_id, envelope.connection_ref, envelope.provider_event_id)
        follow_up = None
        if router is not None and envelope.sender_external_id is not None:
            follow_up = router(self._open(envelope), envelope.received_at)
        if follow_up is not None:
            if follow_up.reopen:
                self.statuses[follow_up.enquiry_id] = EnquiryStatus.RECEIVED
            self.follow_ups.setdefault(follow_up.enquiry_id, []).append(envelope)
            receipt = Receipt(
                follow_up.enquiry_id,
                self.statuses.get(follow_up.enquiry_id, EnquiryStatus.RECEIVED),
                payload_sha256,
                envelope.correlation_id,
                envelope.received_at,
                continued=True,
            )
            self.receipts[key] = receipt
            return receipt, True
        receipt = Receipt(
            enquiry_id,
            EnquiryStatus.RECEIVED,
            payload_sha256,
            envelope.correlation_id,
            envelope.received_at,
        )
        self.receipts[key] = receipt
        self.envelopes[enquiry_id] = envelope
        return receipt, True

    def _open(self, envelope: InboundEnvelope) -> list[OpenEnquiry]:
        return [
            OpenEnquiry(
                enquiry_id,
                self.statuses.get(enquiry_id, EnquiryStatus.RECEIVED),
                first.received_at,
                self.asked.get(enquiry_id),
                False,
            )
            for enquiry_id, first in self.envelopes.items()
            if (first.tenant_id, first.connection_ref, first.sender_external_id)
            == (envelope.tenant_id, envelope.connection_ref, envelope.sender_external_id)
        ]

    def get_enquiry(self, tenant_id: UUID, enquiry_id: UUID) -> EnquiryView | None:
        envelope = self.envelopes.get(enquiry_id)
        if envelope is None or envelope.tenant_id != tenant_id:
            return None
        return EnquiryView(
            enquiry_id,
            envelope.channel,
            EnquiryStatus.RECEIVED,
            envelope.sender_external_id,
            envelope.received_at,
            envelope.correlation_id,
            tuple(
                AttachmentView(
                    item.attachment_id,
                    item.filename,
                    item.declared_content_type,
                    item.size_bytes,
                    AttachmentStatus.QUARANTINED,
                    None,
                )
                for item in envelope.attachments
            ),
            connection_ref=envelope.connection_ref,
        )


class FakeConnections:
    """ConnectionStore fake: every (business, provider, account) has an active connection,
    except accounts listed in `disabled`. IDs are stable, so replays find the same one."""

    def __init__(self) -> None:
        self.disabled: set[str] = set()

    def find_by_account(
        self, tenant_id: UUID, provider_key: str, account_ref: str
    ) -> Connection | None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        connection_id = uuid5(NAMESPACE_URL, f"fake:{tenant_id}:{provider_key}:{account_ref}")
        status = (
            ConnectionStatus.DISABLED if account_ref in self.disabled else ConnectionStatus.ACTIVE
        )
        return Connection(
            connection_id, tenant_id, provider_key, provider_key,
            frozenset({Purpose.CUSTOMER_MESSAGES}), {}, {}, account_ref, status, None, "test",
            now, now,
        )  # fmt: skip

    def get(self, tenant_id: UUID, connection_id: UUID) -> Connection | None:
        raise NotImplementedError

    def list_for(self, tenant_id: UUID, purpose: Purpose | None = None) -> list[Connection]:
        raise NotImplementedError

    def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
        raise NotImplementedError


@pytest.fixture
def connection_store() -> FakeConnections:
    return FakeConnections()


@pytest.fixture
def provider_registry() -> ProviderRegistry:
    return ProviderRegistry([ApiProvider()])


class FakeApiKeys:
    """ApiKeyReader fake; tests add ApiKeyRecord values to `.records`."""

    def __init__(self) -> None:
        self.records: dict[UUID, ApiKeyRecord] = {}

    def find(self, key_id: UUID) -> ApiKeyRecord | None:
        return self.records.get(key_id)


@pytest.fixture
def memory_storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def intake_store() -> FakeIntakeStore:
    return FakeIntakeStore()


@pytest.fixture
def api_key_reader() -> FakeApiKeys:
    return FakeApiKeys()


def build_text_pdf(lines: list[str]) -> bytes:
    """Minimal one-page PDF with a real text layer (Helvetica), built by hand for tests."""
    escaped = [line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)") for line in lines]
    stream = "BT /F1 12 Tf 50 750 Td 16 TL " + " ".join(f"({line}) Tj T*" for line in escaped)
    stream += " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = "%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output.encode("latin-1")))
        output += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(output.encode("latin-1"))
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    output += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return output.encode("latin-1")


@pytest.fixture
def make_pdf() -> Callable[[list[str]], bytes]:
    return build_text_pdf
