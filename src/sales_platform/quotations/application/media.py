"""Public PDF links for tools that fetch attachments by URL (phase 5, contracts C6).

MediaLinkHook wraps a message's send hook: when the message carries a quotation PDF and the
connection's tool downloads attachments from a URL (capabilities.attachments_by_url), it adds
a link with a fresh random token. The token exists only in what the provider receives.
MediaService serves GET /api/v1/media/<token>: unknown, expired or changed documents are all
the same "not found", and the PDF's hash is checked again before it is returned.
"""

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.messages import OutboundMessage
from sales_platform.integrations.ports.messages import SendHook, SendPreparation
from sales_platform.integrations.ports.providers import SendReceipt
from sales_platform.platform.ports.storage import ObjectNotFound, ObjectStorage
from sales_platform.quotations.application.documents import read_verified
from sales_platform.quotations.domain.documents import DocumentIntegrityError, document_filename
from sales_platform.quotations.ports.media import DocumentLookup, MediaLinkStore

MAX_TOKEN = 100


def utc_now() -> datetime:
    return datetime.now(UTC)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MediaNotFound(Exception):
    """Unknown, expired or no longer matching (one message for every reason)."""


class MediaLinkHook:
    def __init__(
        self,
        inner: SendHook,
        links: MediaLinkStore,
        deliveries: DocumentLookup,
        registry: ProviderRegistry,
        public_base_url: str,
        lifetime: timedelta,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._inner = inner
        self._links = links
        self._deliveries = deliveries
        self._registry = registry
        self._base = public_base_url.rstrip("/")
        self._lifetime = lifetime
        self._clock = clock

    def before_send(self, message: OutboundMessage) -> SendPreparation:
        prepared = self._inner.before_send(message)
        provider = self._registry.find(message.provider_key)
        if (
            prepared.cancel_reason is not None
            or prepared.attachment is None
            or message.document_id is None
            or provider is None
            or not provider.capabilities.attachments_by_url
        ):
            return prepared
        document = self._deliveries.document(message.tenant_id, message.document_id)
        if document is None:
            return SendPreparation(cancel_reason="The quotation document is not available")
        token = secrets.token_urlsafe(32)
        self._links.issue(
            message.tenant_id,
            document.document_id,
            document.content_sha256,
            message.message_id,
            token_sha256(token),
            self._clock() + self._lifetime,
        )
        return replace(prepared, attachment_url=f"{self._base}/api/v1/media/{token}")

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None:
        self._inner.after_send(message, preparation, receipt)


class MediaService:
    def __init__(
        self,
        links: MediaLinkStore,
        deliveries: DocumentLookup,
        storage: ObjectStorage,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._links = links
        self._deliveries = deliveries
        self._storage = storage
        self._clock = clock

    def open(self, token: str) -> tuple[str, bytes]:
        """(file name, PDF bytes); raises MediaNotFound."""
        if not token or len(token) > MAX_TOKEN:
            raise MediaNotFound("Not found")
        target = self._links.find(token_sha256(token))
        if target is None or target.expires_at <= self._clock():
            raise MediaNotFound("Not found")
        document = self._deliveries.document(target.tenant_id, target.document_id)
        if document is None or document.content_sha256 != target.content_sha256:
            raise MediaNotFound("Not found")
        try:
            data = read_verified(self._storage, document)
        except (DocumentIntegrityError, ObjectNotFound):
            raise MediaNotFound("Not found") from None
        return document_filename(document.quotation_id, document.version), data
