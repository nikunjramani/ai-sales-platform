"""Provider `api`: customer systems send enquiries and fetch quotations with an API key.

It is a pull provider: nothing is pushed to the customer. "Sending" a quotation makes it
available for download through GET /api/v1/enquiries/{id}/quotation(.pdf). Each API key is
one `api` connection (account_ref "api-key:<id>"), created together with the key.
"""

from collections.abc import Sequence

from sales_platform.domain.connections import Capabilities, Connection, DeliveryMode, Purpose
from sales_platform.domain.intake import InboundEvent
from sales_platform.ports.erp import ErpReader
from sales_platform.ports.providers import (
    InboundRequest,
    OperationUnsupported,
    ProviderDefaults,
    ProviderEnvironment,
    ReceiptKind,
    RenderedMessage,
    SendReceipt,
    TestResult,
)
from sales_platform.ports.secrets import SecretStore

PROVIDER_KEY = "api"


class ApiProvider(ProviderDefaults):
    key = PROVIDER_KEY
    display_name = "API (customer systems)"
    purposes = frozenset({Purpose.CUSTOMER_MESSAGES})
    capabilities = Capabilities(
        delivery_mode=DeliveryMode.PULL, webhooks=False, outbound_initiation=False
    )
    dev_only = False

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult:
        return TestResult(ok=True)

    def verify_inbound(
        self, conn: Connection, secrets: SecretStore, request: InboundRequest
    ) -> None:
        raise OperationUnsupported("API enquiries arrive on /api/v1/enquiries, not on a webhook")

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        raise OperationUnsupported("API enquiries arrive on /api/v1/enquiries, not on a webhook")

    def send(self, conn: Connection, secrets: SecretStore, message: RenderedMessage) -> SendReceipt:
        # Pull: the customer's system downloads it with its API key; nothing leaves here.
        return SendReceipt(ReceiptKind.AVAILABLE)

    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ErpReader:
        raise OperationUnsupported("The API provider is not a product source")


def build(environment: ProviderEnvironment) -> ApiProvider:
    """Entry point `api` in group sales_platform.providers."""
    return ApiProvider()
