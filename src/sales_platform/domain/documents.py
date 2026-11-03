"""Quotation documents (PDFs) and their delivery to the customer.

A quotation document is the customer-facing PDF of one approved version. It is rendered once,
stored privately, and recorded with its own SHA-256 so anyone can check that the file sent is
the file stored. Only approved versions get a document; drafts can only be previewed, and every
preview page says it is not an offer.

A delivery is one attempt to make that document reach the customer through the channel the
enquiry came in on. Its status says what we actually know, never more:
- pending:   queued, not yet attempted (or a transient failure that will be retried);
- sending:   handed to the channel; if the worker crashes here we cannot know if it arrived;
- available: pull channels (the API): the customer can now download it;
- sent:      push channels (email, messaging): the provider accepted it;
- failed:    it cannot be delivered (no channel adapter, rejected, rendering impossible);
- unknown:   the provider may or may not have it (timeout after sending). Never resent
             automatically, because the customer could get it twice; a person must check.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from sales_platform.domain.connections import DeliveryMode
from sales_platform.domain.quotations import quotation_number
from sales_platform.domain.validation import require_aware

RENDERER_VERSION = "quotation-pdf-v2"  # v2 (phase 2): seller details and logo
MAX_ERROR_CHARACTERS = 300


class DocumentKind(StrEnum):
    APPROVED = "approved"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    AVAILABLE = "available"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


# Statuses after which the worker does nothing more (a person may act on failed/unknown).
FINAL_DELIVERY_STATUSES = frozenset(
    {DeliveryStatus.AVAILABLE, DeliveryStatus.SENT, DeliveryStatus.FAILED, DeliveryStatus.UNKNOWN}
)
# The customer may fetch the document once a delivery reached one of these.
OFFERED_STATUSES = frozenset({DeliveryStatus.AVAILABLE, DeliveryStatus.SENT})


class DocumentNotAvailable(Exception):
    """No approved document (yet) for this quotation, or it belongs to another tenant."""


class UnrenderableContent(Exception):
    """The content cannot be put into the PDF faithfully (e.g. unsupported characters).

    Rendering never drops or replaces characters silently: a changed name or description in a
    commercial document is worse than no document.
    """


# The built-in PDF fonts show Windows-1252 (Western European) text only.
PDF_TEXT_ENCODING = "cp1252"


def printable_text(value: object, field: str) -> str:
    """One line of text the PDF can show exactly, or UnrenderableContent.

    Shared by the renderer and by the business profile form (phase 2), so a saved profile can
    never make every later PDF of the business fail.
    """
    text = " ".join(str(value).split())
    try:
        text.encode(PDF_TEXT_ENCODING)
    except UnicodeEncodeError:
        raise UnrenderableContent(
            f"{field} contains characters the PDF fonts cannot show"
        ) from None
    return text


@dataclass(frozen=True)
class SellerDetails:
    """The business as printed on its quotations (phase 2 PDF details, Q16).

    legal_name and address are required for Go live; the rest is optional. The logo is a
    normalised PNG (re-encoded by the logo adapter, never the uploaded bytes).
    """

    legal_name: str
    address_lines: tuple[str, ...]
    tax_id_label: str | None = None
    tax_id: str | None = None
    logo_png: bytes | None = None


class DocumentIntegrityError(Exception):
    """A stored document's bytes no longer match the hash recorded when it was rendered."""


def document_id(
    tenant_id: UUID, quotation_id: UUID, version: int, kind: DocumentKind, sha256: str
) -> UUID:
    """Deterministic ID from the version and the PDF bytes' hash.

    A retried render of the same bytes reuses the same storage key; different bytes (e.g. after
    a renderer upgrade) get a different key, so a stored document is never overwritten.
    """
    name = f"quotation-document:{tenant_id}:{quotation_id}:{version}:{kind}:{sha256}"
    return uuid5(NAMESPACE_URL, name)


def document_key(tenant_id: UUID, document: UUID) -> str:
    """Server-generated storage key under the tenant's private folder."""
    return f"tenants/{tenant_id}/documents/{document}"


def document_filename(quotation_id: UUID, version: int, *, preview: bool = False) -> str:
    suffix = "-preview" if preview else ""
    return f"{quotation_number(quotation_id)}-v{version}{suffix}.pdf"


class NoDeliveryAddress(Exception):
    """The enquiry carries no address to send the quotation back to."""


def delivery_recipient(
    mode: DeliveryMode, connection_key: str, sender_external_id: str | None
) -> str:
    """Who receives the approved quotation: always whoever sent the enquiry, never an address
    taken from message text. Decided once, at intake (stored as the envelope's reply_to).

    - Pull providers (the API): the connection (API key) that submitted it. The API's
      sender_external_id is the customer's own reference label, not an address.
    - Push providers (email, chat): the provider's verified sender address or chat ID.
    """
    if mode is DeliveryMode.PULL:
        return connection_key
    if not sender_external_id:
        raise NoDeliveryAddress("The enquiry has no sender address to reply to")
    return sender_external_id


def clean_error(message: str) -> str:
    """Short, single-line error text for delivery records (no stack traces or payloads)."""
    text = " ".join(message.split()) or "Unknown error"
    return text[:MAX_ERROR_CHARACTERS]


@dataclass(frozen=True)
class DocumentSource:
    """Everything printed on a quotation PDF, read from the stored immutable version.

    approved_at is None for previews of versions that are not (yet) approved.
    """

    tenant_name: str
    quotation_id: UUID
    version: int
    content: dict[str, object]
    content_sha256: str
    customer_reference: str | None
    approved_at: datetime | None
    valid_until: datetime | None = None  # set for the customer document (M6), not previews
    seller: SellerDetails | None = None  # phase 2; None prints the business name only
    # Where the seller's logo is stored, as (storage key, SHA-256); read and checked just before
    # rendering, because the stores that build this source cannot read files.
    seller_logo_ref: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        if self.approved_at is not None:
            require_aware(self.approved_at, "approved_at")
        if self.valid_until is not None:
            require_aware(self.valid_until, "valid_until")

    @property
    def number(self) -> str:
        return quotation_number(self.quotation_id)


@dataclass(frozen=True)
class StoredDocument:
    document_id: UUID
    quotation_id: UUID
    version: int
    content_sha256: str  # of the version it was rendered from
    storage_key: str
    sha256: str  # of the PDF bytes
    size_bytes: int
    renderer: str
    created_at: datetime
    valid_until: datetime  # printed on the PDF; the customer can answer until then (M6)


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: UUID
    quotation_id: UUID
    version: int
    channel: str
    recipient: str
    status: DeliveryStatus
    document_id: UUID | None
    provider_reference: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None
    first_downloaded_at: datetime | None
