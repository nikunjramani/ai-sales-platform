"""Customer messages that arrive on a provider's webhook (phase 5; phase 4's WI-6).

They take the same road as website chat and API messages (IntakeStore.record with the
follow-up router), so a reply continues the customer's open enquiry. Rules (user answers,
2026-10-29, same as website chat):
- 10 messages a minute and 100 a day per sender; over a limit the message is still stored but
  held for staff review, never read by the AI (the business-wide daily reading cap of the
  extraction step applies as well);
- files: PDF, text, PNG, JPEG up to 5 MB, at most 5 per message and 20 a day per sender,
  downloaded from the tool into quarantine; other kinds (voice, video, stickers) are skipped
  and noted for staff;
- STOP / START as the whole message are consent changes (D15); "where is my order?" (or an
  order number) as the whole message asks for the customer's orders (phase 7);
- a message the business itself typed on its own account is a staff message: kept in the
  conversation, never read as the customer's words or as a command;
- nothing is taken from a suspended business.
"""

import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from sales_platform.conversations.domain.contacts import ConsentStatus, consent_command
from sales_platform.customer_channels.domain.chat import ChatLimits
from sales_platform.enquiries.domain.attachments import StorageArea, attachment_key
from sales_platform.enquiries.domain.intake import (
    AttachmentRecord,
    AttachmentUpload,
    EnquirySubmission,
    InboundEnvelope,
)
from sales_platform.enquiries.ports.intake import EnquiryRouter, IntakeStore
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.domain.events import (
    ConsentChange,
    CustomerMessage,
    InboundFile,
    OrderStatusRequest,
)
from sales_platform.integrations.ports.inbound import InboundStore
from sales_platform.orders.domain.orders import order_status_command
from sales_platform.platform.domain.rate_limits import RateRule
from sales_platform.platform.domain.tenancy import accepts_intake
from sales_platform.platform.ports.rate_limits import RateLimiter
from sales_platform.platform.ports.secrets import SecretStore
from sales_platform.platform.ports.storage import ObjectStorage
from sales_platform.platform.ports.tenancy import TenantStatusReader

logger = logging.getLogger(__name__)
ACCEPTED_FILE_TYPES = frozenset({"application/pdf", "text/plain", "image/png", "image/jpeg"})
SHORTENED = " [shortened]"
DEFAULT_LIMITS = ChatLimits()


def utc_now() -> datetime:
    return datetime.now(UTC)


class CustomerInbox:
    def __init__(
        self,
        intake: IntakeStore,
        inbound: InboundStore,
        storage: ObjectStorage,
        limiter: RateLimiter,
        registry: ProviderRegistry,
        secrets: SecretStore,
        statuses: TenantStatusReader,
        router: EnquiryRouter,
        limits: ChatLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._intake = intake
        self._inbound = inbound
        self._storage = storage
        self._limiter = limiter
        self._registry = registry
        self._secrets = secrets
        self._statuses = statuses
        self._router = router
        self._limits = limits
        self._rules = limits.rules()
        self._clock = clock

    def receive(self, connection: Connection, message: CustomerMessage) -> bool:
        """Store one customer message; False if it was already stored (or not taken)."""
        tenant_id = connection.tenant_id
        now = self._clock()
        if message.from_business:
            recorded = self._inbound.record(connection, [message], now, queue_jobs=True)
            return recorded[0].new
        if not accepts_intake(self._statuses.status(tenant_id)):
            logger.info("Business %s is suspended; a customer message was not taken", tenant_id)
            return False
        key = intake_key(message.provider_event_id)
        existing = self._intake.find_receipt(tenant_id, connection.connection_id, key)
        if existing is not None:
            return False
        consent = consent_command(message.text)
        if consent is not None and not message.files:
            change = ConsentChange(
                message.provider_event_id,
                message.sender_external_id,
                consent is ConsentStatus.OPTED_IN,
            )
            return self._inbound.record(connection, [change], now, queue_jobs=True)[0].new
        query = order_status_command(message.text)
        if query is not None and not message.files:
            ask = OrderStatusRequest(
                message.provider_event_id, message.sender_external_id, query.order_number
            )
            return self._inbound.record(connection, [ask], now, queue_jobs=True)[0].new
        sender = f"{connection.connection_id}:{message.sender_external_id}"
        held = not (
            self._allowed(self._rules.per_minute, sender, now)
            and self._allowed(self._rules.per_day, sender, now)
        )
        uploads, skipped = self._files(connection, message.files, sender, now)
        text = _text(message.text, [*message.skipped, *skipped], len(uploads))
        records = tuple(
            AttachmentRecord(
                attachment_id=attachment_id,
                filename=upload.filename,
                declared_content_type=upload.declared_content_type,
                size_bytes=len(upload.data),
                sha256=upload.sha256,
                storage_key=attachment_key(tenant_id, attachment_id, StorageArea.QUARANTINE),
            )
            for upload, attachment_id in ((upload, uuid4()) for upload in uploads)
        )
        envelope = InboundEnvelope(
            event_id=uuid4(),
            tenant_id=tenant_id,
            channel=connection.provider_key,
            connection_id=connection.legacy_key,
            connection_ref=connection.connection_id,
            reply_to=message.sender_external_id,
            provider_event_id=key,
            sender_external_id=message.sender_external_id,
            received_at=now,
            text=text,
            attachments=records,
            correlation_id=uuid4(),
        )
        fingerprint = EnquirySubmission(
            text, message.sender_external_id, tuple(uploads)
        ).fingerprint()
        try:
            for upload, record in zip(uploads, records, strict=True):
                self._storage.put(record.storage_key, upload.data)
            _, created = self._intake.record(
                envelope,
                fingerprint,
                enquiry_id=uuid4(),
                router=self._router,
                hold_for_review=held,
            )
        except BaseException:
            self._discard(records)
            raise
        if not created:
            self._discard(records)
        return created

    def _files(
        self, connection: Connection, files: Sequence[InboundFile], sender: str, now: datetime
    ) -> tuple[list[AttachmentUpload], list[str]]:
        uploads: list[AttachmentUpload] = []
        skipped: list[str] = []
        provider = self._registry.get(connection.provider_key)
        limit = self._limits.max_file_bytes
        for item in files:
            name = item.filename or "file"
            if len(uploads) >= self._limits.files_per_message:
                skipped.append(f"{name} (more than {self._limits.files_per_message} files)")
                continue
            if item.content_type not in ACCEPTED_FILE_TYPES:
                skipped.append(f"{name} (this kind of file is not read)")
                continue
            if item.size_bytes is not None and item.size_bytes > limit:
                skipped.append(f"{name} (larger than {limit // (1024 * 1024)} MB)")
                continue
            if not self._allowed(self._rules.files_per_day, sender, now):
                skipped.append(f"{name} (daily file limit reached)")
                continue
            try:
                data = provider.fetch_file(connection, self._secrets, item.file_ref, limit)
                uploads.append(AttachmentUpload(name[:255], item.content_type, data))
            except ValueError:
                skipped.append(f"{name} (could not be downloaded)")
        return uploads, skipped

    def _allowed(self, rule: RateRule, key: str, now: datetime) -> bool:
        return self._limiter.hit(rule, key, now).allowed

    def _discard(self, records: tuple[AttachmentRecord, ...]) -> None:
        for record in records:
            self._storage.delete(record.storage_key)


def intake_key(provider_event_id: str) -> str:
    """The stored event ID of a webhook message: always 8-200 characters (the intake rule)
    and the same for every retry of the same provider event."""
    key = f"message:{provider_event_id}"
    if len(key) > 200:
        key = "message:" + hashlib.sha256(provider_event_id.encode("utf-8")).hexdigest()
    return key


def _text(text: str, skipped: Sequence[str], files: int, limit: int = 2000) -> str:
    """The customer's words, shortened to the chat limit, plus our notes in brackets about
    what was not read (staff see them; the reader treats all of it as customer data)."""
    body = text.strip()
    if len(body) > limit:
        body = body[: limit - len(SHORTENED)] + SHORTENED
    notes = [f"[not read: {item}]" for item in skipped]
    if not body and files:
        notes.insert(0, f"[sent {files} file{'s' if files != 1 else ''}]")
    return "\n".join(part for part in (body, *notes) if part) or "[empty message]"
