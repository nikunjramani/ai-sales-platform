"""The simulated provider: a fake manager phone plus a demo product catalogue (development only).

It behaves like a real provider in the ways that matter for security:
- Outbound: messages are "delivered" as JSON files in a local outbox (see outbox.py).
- Inbound: callbacks are signed with HMAC-SHA256 using the connection's own signing secret,
  kept in the secret store. Unsigned or altered callbacks are refused before parsing. The body
  names no business: the business is always the connection's.
- Catalogue: the demo catalogue named in the connection's config ("industrial" or "office").
- Phase 5: every message to a manager is a file with its message_id; a "reply" event answers
  one of them by that ID (a written reply, read by the AI when a model is configured), and a
  "confirm" event presses Yes or No on a confirmation question.
"""

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sales_platform.adapters.erp.simulated import DEMO_CATALOGUES, SimulatedErp, demo_catalogue
from sales_platform.adapters.providers.simulated.outbox import write_message
from sales_platform.adapters.providers.simulated.signing import SIGNATURE_HEADER, signature_valid
from sales_platform.domain.connections import Capabilities, Connection, Purpose
from sales_platform.domain.intake import InboundEvent, ManagerAction, ManagerReply
from sales_platform.ports.erp import ErpReader
from sales_platform.ports.providers import (
    InboundRejected,
    InboundRequest,
    InvalidInbound,
    OperationUnsupported,
    ProviderDefaults,
    ReceiptKind,
    RenderedMessage,
    SendReceipt,
    SendRejected,
    TestResult,
)
from sales_platform.ports.secrets import SecretNotFound, SecretStore, SecretStoreUnavailable

PROVIDER_KEY = "simulated"
SIGNING_SECRET = "signing_secret"  # name of the secret reference on the connection


class SimulatedEvent(BaseModel):
    """Body of a callback on the generic hook route. No tenant or connection fields."""

    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=200)
    external_user_id: str = Field(min_length=1, max_length=200)
    type: Literal["enroll", "decision", "reply", "confirm"]
    code: str | None = Field(default=None, max_length=50)
    action_reference: str | None = Field(default=None, max_length=200)
    action: str | None = Field(default=None, max_length=32)
    reason: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, max_length=4000)
    reply_to: str | None = Field(default=None, max_length=200)  # one of our message IDs

    def to_action(self) -> ManagerAction | ManagerReply:
        if self.type == "reply":
            return ManagerReply(
                self.event_id, self.external_user_id, self.text or "", self.reply_to
            )
        return ManagerAction(
            provider_event_id=self.event_id,
            external_user_id=self.external_user_id,
            kind=self.type,
            code=self.code,
            reference=self.action_reference,
            action=self.action,
            reason=self.reason,
        )


class SimulatedProvider(ProviderDefaults):
    key = PROVIDER_KEY
    display_name = "Simulated phone and demo catalogue (development)"
    purposes = frozenset({Purpose.MANAGER_APPROVALS, Purpose.CATALOGUE})
    capabilities = Capabilities(webhooks=True, buttons=True, max_inbound_bytes=16 * 1024)
    dev_only = True

    def __init__(self, outbox: Path) -> None:
        self._outbox = outbox.resolve()
        self._catalogues: dict[tuple[UUID, str], SimulatedErp] = {}
        self._lock = threading.Lock()

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult:
        return TestResult(ok=True)

    def verify_inbound(
        self, conn: Connection, secrets: SecretStore, request: InboundRequest
    ) -> None:
        reference = conn.secret_refs.get(SIGNING_SECRET)
        if reference is None:
            raise InboundRejected("No signing secret is configured")
        try:
            secret = secrets.get(conn.tenant_id, reference)
        except (SecretNotFound, SecretStoreUnavailable):
            raise InboundRejected("No signing secret is available") from None
        signature = request.headers.get(SIGNATURE_HEADER.lower())
        if not signature_valid(secret, request.body, signature):
            raise InboundRejected("Invalid signature")

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        try:
            return [SimulatedEvent.model_validate_json(request.body).to_action()]
        except ValidationError:
            raise InvalidInbound("Invalid event") from None

    def send(self, conn: Connection, secrets: SecretStore, message: RenderedMessage) -> SendReceipt:
        metadata = message.metadata
        common: dict[str, object] = {
            "tenant_id": str(conn.tenant_id),
            "connection_id": conn.legacy_key,
            "to": message.recipient,
        }
        if "notice_id" in metadata:
            name = f"notice-{metadata['notice_id']}.json"
            body = {**common, "notice_id": metadata["notice_id"], "text": message.text}
        elif "task_id" in metadata:
            if message.action_reference is None or message.expires_at is None:
                raise SendRejected("An approval request needs an action reference")
            name = f"{metadata['task_id']}.json"
            body = {
                **common,
                "message_id": str(message.message_id),
                "task_id": metadata["task_id"],
                "quotation": metadata["quotation"],
                "version": int(metadata["version"]),
                "text": message.text,
                "actions": list(message.actions),
                "action_reference": message.action_reference,
                "expires_at": message.expires_at.isoformat(),
                "link": message.link,
            }
        else:
            # Phase 5: questions and confirmations about an approval request.
            name = f"message-{message.message_id}.json"
            body = {
                **common,
                "message_id": str(message.message_id),
                "text": message.text,
                "actions": list(message.actions),
                "action_reference": message.action_reference,
            }
        write_message(self._outbox, conn.tenant_id, message.recipient, name, body)
        # One ID per message: the same file name is used for every account that gets this task.
        return SendReceipt(ReceiptKind.SENT, provider_message_id=str(message.message_id))

    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ErpReader:
        name = str(conn.config.get("catalogue", ""))
        if Purpose.CATALOGUE not in conn.purposes or name not in DEMO_CATALOGUES:
            raise OperationUnsupported("This connection has no demo catalogue")
        # One in-memory catalogue per business and name, so stock and prices stay consistent.
        with self._lock:
            reader = self._catalogues.get((conn.tenant_id, name))
            if reader is None:
                reader = SimulatedErp({conn.tenant_id: demo_catalogue(name)})
                self._catalogues[(conn.tenant_id, name)] = reader
        return reader
