"""Short-lived public links to one approved quotation PDF (phase 5, contracts C6).

Some tools download attachments from a URL instead of receiving the bytes. The link carries a
random token (only its SHA-256 is stored), names one document, and stops working after
SALES_MEDIA_LINK_MINUTES. The API finds it by the token's hash alone, like an API key.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sales_platform.domain.documents import StoredDocument


@dataclass(frozen=True)
class MediaLinkTarget:
    tenant_id: UUID
    document_id: UUID
    content_sha256: str  # the version the link was made for; re-checked on every download
    expires_at: datetime


class MediaLinkStore(Protocol):
    def issue(
        self,
        tenant_id: UUID,
        document_id: UUID,
        content_sha256: str,
        outbound_message_id: UUID,
        token_sha256: str,
        expires_at: datetime,
    ) -> None:
        """Worker role: at send time."""
        ...

    def find(self, token_sha256: str) -> MediaLinkTarget | None:
        """API role, before the business is known: exactly the link with that hash."""
        ...


class DocumentLookup(Protocol):
    """The one read a public link needs (the delivery store has it)."""

    def document(self, tenant_id: UUID, document_id: UUID) -> StoredDocument | None: ...
