"""Provider `web_chat`: our own chat for a business's website (phase 4, Q13).

We host both sides, so there is no outside service: visitors write through our public chat API
(/api/v1/public/chat/<handle>, page /chat/<handle>), which calls the core directly, and
"sending" to a visitor means the message is already in their conversation, where the widget
reads it. Accept/Decline are buttons. Visitors cannot be messaged first: only inside a chat
they started. The core never names this provider; it acts on capabilities (hosted_chat).
"""

from collections.abc import Sequence

from sales_platform.domain.chat import MAX_ATTACHMENT_BYTES
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

PROVIDER_KEY = "web_chat"
_NO_WEBHOOK = "Website chat messages arrive on the public chat API, not on a webhook"


class WebChatProvider(ProviderDefaults):
    key = PROVIDER_KEY
    display_name = "Website chat"
    purposes = frozenset({Purpose.CUSTOMER_MESSAGES})
    capabilities = Capabilities(
        delivery_mode=DeliveryMode.PUSH,
        webhooks=False,
        attachments=True,
        max_attachment_bytes=MAX_ATTACHMENT_BYTES,
        buttons=True,
        outbound_initiation=False,
        delivery_receipts=False,
        manager_enrollment="none",
        hosted_chat=True,
    )
    dev_only = False

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult:
        return TestResult(ok=True)

    def verify_inbound(
        self, conn: Connection, secrets: SecretStore, request: InboundRequest
    ) -> None:
        raise OperationUnsupported(_NO_WEBHOOK)

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        raise OperationUnsupported(_NO_WEBHOOK)

    def send(self, conn: Connection, secrets: SecretStore, message: RenderedMessage) -> SendReceipt:
        # The message is already a row of the visitor's conversation; the widget polls it.
        return SendReceipt(ReceiptKind.SENT, str(message.message_id))

    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ErpReader:
        raise OperationUnsupported("Website chat is not a product source")


def build(environment: ProviderEnvironment) -> WebChatProvider:
    """Entry point `web_chat` in group sales_platform.providers."""
    return WebChatProvider()
