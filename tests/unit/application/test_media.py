"""Short-lived public PDF links (phase 5, C6): issued only for tools that fetch by URL."""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sales_platform.application.media import MediaLinkHook, MediaNotFound, MediaService
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.connections import Capabilities, Purpose
from sales_platform.domain.documents import StoredDocument
from sales_platform.domain.messages import MessageStatus, OutboundMessage, TemplateKey
from sales_platform.ports.media import MediaLinkTarget
from sales_platform.ports.messages import SendPreparation
from sales_platform.ports.providers import Attachment, SendReceipt
from tests.conftest import InMemoryStorage
from tests.unit.one_click import OneClickTool

PDF = b"%PDF-1.7 quotation"


@dataclass
class Links:
    rows: dict[str, MediaLinkTarget] = field(default_factory=dict)

    def issue(
        self,
        tenant_id: UUID,
        document_id: UUID,
        content_sha256: str,
        outbound_message_id: UUID,
        token_sha256: str,
        expires_at: datetime,
    ) -> None:
        self.rows[token_sha256] = MediaLinkTarget(
            tenant_id, document_id, content_sha256, expires_at
        )

    def find(self, token_sha256: str) -> MediaLinkTarget | None:
        return self.rows.get(token_sha256)


class Documents:
    def __init__(self, document: StoredDocument) -> None:
        self.stored = document

    def document(self, tenant_id: UUID, document_id: UUID) -> StoredDocument | None:
        return self.stored if document_id == self.stored.document_id else None


class Inner:
    def before_send(self, message: OutboundMessage) -> SendPreparation:
        return SendPreparation(attachment=Attachment("q.pdf", "application/pdf", PDF, "x"))

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None:
        return None


class ByUrl(OneClickTool):
    key = "by_url"
    capabilities = Capabilities(attachments=True, attachments_by_url=True)


def document(now: datetime) -> StoredDocument:
    return StoredDocument(
        uuid4(), uuid4(), 2, "c" * 64, "docs/q.pdf", hashlib.sha256(PDF).hexdigest(),
        len(PDF), "test", now, now + timedelta(days=7),
    )  # fmt: skip


def message(now: datetime, provider_key: str, document_id: UUID) -> OutboundMessage:
    return OutboundMessage(
        uuid4(), uuid4(), uuid4(), provider_key, Purpose.CUSTOMER_MESSAGES,
        TemplateKey.QUOTATION_DOCUMENT, {}, "+1500555", "key", "", {}, MessageStatus.PENDING,
        document_id, uuid4(), 2, None, None, None, None, uuid4(), None, None, now, now,
    )  # fmt: skip


def test_a_tool_that_fetches_by_url_gets_a_link_whose_token_is_never_stored(
    now: datetime,
) -> None:
    stored, links, storage = document(now), Links(), InMemoryStorage()
    storage.put(stored.storage_key, PDF)
    registry = ProviderRegistry([ByUrl(now), OneClickTool(now)])
    hook = MediaLinkHook(
        Inner(),
        links,
        Documents(stored),
        registry,
        "https://api.example.com",
        timedelta(minutes=15),
    )

    prepared = hook.before_send(message(now, "by_url", stored.document_id))
    plain = hook.before_send(message(now, OneClickTool.key, stored.document_id))

    assert plain.attachment_url is None
    assert prepared.attachment_url is not None
    token = prepared.attachment_url.rsplit("/", 1)[1]
    assert prepared.attachment_url == f"https://api.example.com/api/v1/media/{token}"
    assert token not in str(links.rows)  # only its SHA-256 is kept
    service = MediaService(links, Documents(stored), storage, clock=lambda: now)
    assert service.open(token) == ("Q-" + stored.quotation_id.hex[:8].upper() + "-v2.pdf", PDF)


def test_unknown_expired_or_changed_links_are_all_not_found(now: datetime) -> None:
    stored, links, storage = document(now), Links(), InMemoryStorage()
    storage.put(stored.storage_key, PDF)
    registry = ProviderRegistry([ByUrl(now)])
    hook = MediaLinkHook(
        Inner(), links, Documents(stored), registry, "https://a", timedelta(minutes=15), lambda: now
    )
    token = str(hook.before_send(message(now, "by_url", stored.document_id)).attachment_url)
    token = token.rsplit("/", 1)[1]

    later = MediaService(links, Documents(stored), storage, clock=lambda: now + timedelta(hours=1))
    with pytest.raises(MediaNotFound):
        later.open(token)
    with pytest.raises(MediaNotFound):
        MediaService(links, Documents(stored), storage, clock=lambda: now).open("guess")
    storage.put(stored.storage_key, b"%PDF- tampered")
    with pytest.raises(MediaNotFound):
        MediaService(links, Documents(stored), storage, clock=lambda: now).open(token)
