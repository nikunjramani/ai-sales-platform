"""Hosted customer chat (website chat, phase 4): visitors, sessions, messages and answers.

Flow:
1. config(handle): the public settings for the chat page (business name, greeting, limits).
   Unknown, disabled, suspended or non-chat handles are all the same ChatNotFound.
2. start(...): a visitor gives a name and an email or a phone (D12; stored as claimed, Q2).
   Anti-bot (Q8): an empty hidden field, a form open at least 2 seconds, 20 new chats per hour
   per IP address per business. Creates contact, identity, conversation and a session. The
   visitor gets a token "cs_<session>_<secret>"; only the secret's SHA-256 is stored.
3. send(...): a message (text and up to 5 files, Q11) goes through the same routing as API
   enquiries (IntakeStore.record with the follow-up router), so it continues the visitor's open
   enquiry or starts one. STOP/UNSUBSCRIBE/OPT OUT/START as the whole message (D15) become a
   consent event instead, processed by the worker (which sends the confirmation).
4. thread(...): what the visitor sees, polled by the widget.
5. act(...): Accept/Decline buttons, through the shared answer core.
Limits (Q9): 2,000 characters, 10 messages a minute, 100 a day, 20 files a day per session;
sessions expire after 30 idle days.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sales_platform.conversations.domain.contacts import (
    ClaimedDetails,
    ConsentStatus,
    consent_command,
)
from sales_platform.customer_channels.domain.chat import ChatLimits, origin_allowed
from sales_platform.customer_channels.ports.chat import (
    ChatEndpoint,
    ChatSession,
    ChatStore,
    ThreadMessage,
)
from sales_platform.enquiries.domain.attachments import StorageArea, attachment_key
from sales_platform.enquiries.domain.intake import (
    AttachmentRecord,
    AttachmentUpload,
    EnquirySubmission,
    InboundEnvelope,
    validate_idempotency_key,
)
from sales_platform.enquiries.ports.intake import EnquiryRouter, IdempotencyConflict, IntakeStore
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.events import ConsentChange, OrderStatusRequest
from sales_platform.integrations.ports.inbound import InboundStore
from sales_platform.orders.domain.orders import order_status_command
from sales_platform.platform.domain.rate_limits import RateRule
from sales_platform.platform.domain.tenancy import accepts_intake
from sales_platform.platform.ports.rate_limits import RateLimiter
from sales_platform.platform.ports.storage import ObjectStorage
from sales_platform.quotations.application.customer_actions import (
    CustomerActionRejected,
    CustomerActionService,
)
from sales_platform.quotations.application.documents import read_verified
from sales_platform.quotations.domain.acceptance import RecordedResponse, ResponseAction
from sales_platform.quotations.domain.documents import document_filename

MAX_KEY_LENGTH = 150  # the stored event ID is "<session>:<key>" (at most 200)
POLL_LIMIT = 100
DEFAULT_LIMITS = ChatLimits()


def utc_now() -> datetime:
    return datetime.now(UTC)


class ChatNotFound(Exception):
    """Unknown, disabled, suspended or not a chat: one answer for all."""


class SessionRejected(Exception):
    """Missing, malformed, unknown, revoked, expired or another chat's token."""


class ChatRefused(Exception):
    """The request breaks a rule the visitor can fix (message shown to them)."""


class ChatRateLimited(Exception):
    def __init__(self, retry_after: timedelta) -> None:
        super().__init__("Too many messages; please wait a moment")
        self.retry_after = retry_after


@dataclass(frozen=True)
class ChatConfig:
    business_name: str
    greeting: str
    max_message_chars: int
    files_per_message: int
    max_file_bytes: int


@dataclass(frozen=True)
class NewChatSession:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class ChatReceipt:
    duplicate: bool
    received_at: datetime


def session_token(session_id: UUID, secret: str) -> str:
    return f"cs_{session_id.hex}_{secret}"


def _parse_token(token: str) -> tuple[UUID, str]:
    parts = token.split("_", 2) if isinstance(token, str) else []
    if len(parts) != 3 or parts[0] != "cs" or not parts[2]:
        raise SessionRejected("Not signed in")
    try:
        return UUID(hex=parts[1]), parts[2]
    except ValueError:
        raise SessionRejected("Not signed in") from None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class HostedChatService:
    def __init__(
        self,
        chats: ChatStore,
        intake: IntakeStore,
        inbound: InboundStore,
        storage: ObjectStorage,
        limiter: RateLimiter,
        registry: ProviderRegistry,
        actions: CustomerActionService,
        router: EnquiryRouter,
        ip_key: bytes,
        own_origin: str | None,
        limits: ChatLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._chats = chats
        self._intake = intake
        self._inbound = inbound
        self._storage = storage
        self._limiter = limiter
        self._registry = registry
        self._actions = actions
        self._router = router
        self._ip_key = ip_key
        self._own_origin = own_origin
        self._limits = limits
        self._rules = limits.rules()
        self._clock = clock

    # --- public, before a session -------------------------------------------------------
    def endpoint(self, handle: str, origin: str | None) -> ChatEndpoint:
        found = self._chats.endpoint(handle) if handle else None
        provider = None if found is None else self._registry.find(found.connection.provider_key)
        if (
            found is None
            or provider is None
            or not provider.capabilities.hosted_chat
            or not found.connection.active
            or not accepts_intake(found.tenant_status)
        ):
            raise ChatNotFound("Not found")
        if not origin_allowed(origin, self.allowed_origins(found), self._own_origin):
            raise ChatNotFound("Not found")
        return found

    @staticmethod
    def allowed_origins(endpoint: ChatEndpoint) -> list[str]:
        value = endpoint.connection.config.get("allowed_origins", [])
        return [str(item) for item in value] if isinstance(value, list) else []

    def config(self, endpoint: ChatEndpoint) -> ChatConfig:
        greeting = endpoint.connection.config.get("greeting")
        return ChatConfig(
            endpoint.business_name,
            str(greeting) if greeting else "",
            self._limits.max_message_chars,
            self._limits.files_per_message,
            self._limits.max_file_bytes,
        )

    def start(
        self,
        endpoint: ChatEndpoint,
        name: str,
        email: str | None,
        phone: str | None,
        honeypot: str,
        form_ms: int,
        origin: str | None,
        client_ip: str,
    ) -> NewChatSession:
        """Raises ChatRefused (bot checks, invalid details: InvalidContact) or ChatRateLimited."""
        if honeypot or form_ms < self._limits.min_form_time.total_seconds() * 1000:
            raise ChatRefused("Please try again")
        details = ClaimedDetails.parse(name, email, phone)  # InvalidContact
        ip_hmac = hmac.new(self._ip_key, client_ip.encode("utf-8"), hashlib.sha256).hexdigest()
        self._hit(self._rules.start_per_ip, f"{endpoint.connection.connection_id}:{ip_hmac}")
        secret = secrets.token_urlsafe(32)
        expires_at = self._clock() + self._limits.session_idle
        session = self._chats.start(endpoint, details, _sha256(secret), ip_hmac, origin, expires_at)
        return NewChatSession(session_token(session.session_id, secret), expires_at)

    # --- with a session ------------------------------------------------------------------
    def authenticate(self, endpoint: ChatEndpoint, token: str) -> ChatSession:
        session_id, secret = _parse_token(token)
        stored = self._chats.session(session_id)
        expected = stored.secret_sha256 if stored is not None else "0" * 64
        matches = hmac.compare_digest(_sha256(secret), expected)
        now = self._clock()
        if (
            stored is None
            or not matches
            or stored.revoked
            or stored.session.expires_at <= now
            or stored.session.connection_id != endpoint.connection.connection_id
        ):
            raise SessionRejected("Not signed in")
        expires_at = now + self._limits.session_idle
        if expires_at - stored.session.expires_at > timedelta(hours=1):
            self._chats.touch(stored.session, expires_at)  # sliding idle expiry (Q9)
        return stored.session

    def send(
        self,
        endpoint: ChatEndpoint,
        session: ChatSession,
        text: str,
        attachments: Sequence[AttachmentUpload],
        idempotency_key: str,
    ) -> ChatReceipt:
        """Raises ChatRefused, ChatRateLimited or IdempotencyConflict."""
        key = validate_idempotency_key(idempotency_key)
        if len(key) > MAX_KEY_LENGTH:
            raise ChatRefused(f"Idempotency-Key is limited to {MAX_KEY_LENGTH} characters")
        if not text.strip() or len(text) > self._limits.max_message_chars:
            raise ChatRefused(f"Messages are 1-{self._limits.max_message_chars} characters")
        if len(attachments) > self._limits.files_per_message:
            raise ChatRefused(f"At most {self._limits.files_per_message} files per message")
        event_id = f"{session.session_id.hex}:{key}"
        connection = endpoint.connection
        submission = EnquirySubmission(text, session.external_id, tuple(attachments))
        fingerprint = submission.fingerprint()
        existing = self._intake.find_receipt(session.tenant_id, connection.connection_id, event_id)
        if existing is not None:
            if existing.payload_sha256 != fingerprint:
                raise IdempotencyConflict("Idempotency-Key was already used for another message")
            return ChatReceipt(True, existing.received_at)
        self._hit(self._rules.per_minute, str(session.session_id))
        self._hit(self._rules.per_day, str(session.session_id))
        for _ in attachments:
            self._hit(self._rules.files_per_day, str(session.session_id))
        now = self._clock()
        consent = consent_command(text)
        if consent is not None and not attachments:
            change = ConsentChange(event_id, session.external_id, consent is ConsentStatus.OPTED_IN)
            recorded = self._inbound.record(connection, [change], now, queue_jobs=True)
            return ChatReceipt(not recorded[0].new, now)
        query = order_status_command(text)
        if query is not None and not attachments:
            ask = OrderStatusRequest(event_id, session.external_id, query.order_number)
            recorded = self._inbound.record(connection, [ask], now, queue_jobs=True)
            return ChatReceipt(not recorded[0].new, now)
        records = tuple(
            AttachmentRecord(
                attachment_id=attachment_id,
                filename=upload.filename,
                declared_content_type=upload.declared_content_type,
                size_bytes=len(upload.data),
                sha256=upload.sha256,
                storage_key=attachment_key(
                    session.tenant_id, attachment_id, StorageArea.QUARANTINE
                ),
            )
            for upload, attachment_id in ((upload, uuid4()) for upload in attachments)
        )
        envelope = InboundEnvelope(
            event_id=uuid4(),
            tenant_id=session.tenant_id,
            channel=connection.provider_key,
            connection_id=connection.legacy_key,
            connection_ref=connection.connection_id,
            reply_to=session.external_id,
            provider_event_id=event_id,
            sender_external_id=session.external_id,
            received_at=now,
            text=text,
            attachments=records,
            correlation_id=uuid4(),
        )
        try:
            for upload, record in zip(attachments, records, strict=True):
                self._storage.put(record.storage_key, upload.data)
            receipt, created = self._intake.record(
                envelope, fingerprint, enquiry_id=uuid4(), router=self._router
            )
        except BaseException:
            self._discard(records)
            raise
        if not created:
            self._discard(records)
            if receipt.payload_sha256 != fingerprint:
                raise IdempotencyConflict("Idempotency-Key was already used for another message")
        return ChatReceipt(not created, receipt.received_at)

    def thread(self, session: ChatSession, after_seq: int) -> list[ThreadMessage]:
        return self._chats.thread(session, max(after_seq, 0), POLL_LIMIT)

    def act(
        self,
        session: ChatSession,
        ref_id: UUID,
        action: ResponseAction,
        reason: str | None,
    ) -> tuple[RecordedResponse, bool]:
        """Raises CustomerActionRejected and the answer core's errors."""
        ref = self._actions.by_button(session.tenant_id, ref_id, session.identity_id)
        if ref.conversation_id != session.conversation_id:
            raise CustomerActionRejected("This link or button is no longer valid")
        return self._actions.act(ref, action, reason)

    def document(self, session: ChatSession, outbound_message_id: UUID) -> tuple[str, bytes]:
        """(file name, PDF bytes) of a quotation sent in this chat; hash re-checked."""
        stored = self._chats.document(session, outbound_message_id)
        if stored is None:
            raise ChatNotFound("Not found")
        data = read_verified(self._storage, stored)
        return document_filename(stored.quotation_id, stored.version), data

    def end(self, session: ChatSession) -> None:
        self._chats.revoke(session)

    def _hit(self, rule: RateRule, key: str) -> None:
        decision = self._limiter.hit(rule, key, self._clock())
        if not decision.allowed:
            raise ChatRateLimited(decision.retry_after)

    def _discard(self, records: tuple[AttachmentRecord, ...]) -> None:
        for record in records:
            self._storage.delete(record.storage_key)
