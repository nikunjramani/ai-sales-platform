"""Staff views of customers and conversations, and the manual contact merge (phase 4)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: UUID
    contact_id: UUID
    contact_name: str | None
    provider_key: str
    connection_name: str
    last_text: str
    last_direction: str
    last_at: datetime
    enquiry_id: UUID | None  # the newest enquiry of the conversation
    enquiry_status: str | None
    quotation_id: UUID | None
    quotation_status: str | None


@dataclass(frozen=True)
class ThreadEntry:
    seq: int
    direction: str
    author: str
    text: str
    at: datetime
    enquiry_id: UUID | None
    template_key: str | None
    outbound_status: str | None  # our messages: pending, sent, failed (with reason)...
    outbound_error: str | None
    # Phase 5: Resend. A failed customer message not resent yet may be sent again.
    outbound_message_id: UUID | None = None
    outbound_purpose: str | None = None
    resent: bool = False


@dataclass(frozen=True)
class IdentityView:
    identity_id: UUID
    provider_key: str
    connection_name: str
    external_id: str
    verified: bool
    consent: str


@dataclass(frozen=True)
class ContactSummary:
    contact_id: UUID
    name: str | None
    company: str | None
    email: str | None
    phone: str | None
    email_verified: bool
    phone_verified: bool
    identities: int
    last_activity: datetime | None


@dataclass(frozen=True)
class ContactEnquiry:
    enquiry_id: UUID
    status: str
    received_at: datetime
    quotation_id: UUID | None
    quotation_status: str | None


@dataclass(frozen=True)
class ContactDetail:
    contact: ContactSummary
    identities: tuple[IdentityView, ...]
    conversations: tuple[UUID, ...]
    enquiries: tuple[ContactEnquiry, ...]
    suggestions: tuple[ContactSummary, ...]  # same verified email/phone (D13)


class ContactReader(Protocol):
    def conversations(self, tenant_id: UUID, limit: int) -> list[ConversationSummary]: ...

    def conversation(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> tuple[ConversationSummary, IdentityView, list[ThreadEntry]] | None: ...

    def enquiry_thread(self, tenant_id: UUID, enquiry_id: UUID) -> list[ThreadEntry]:
        """Every message of the enquiry's conversation that belongs to it."""
        ...

    def contacts(self, tenant_id: UUID, query: str | None, limit: int) -> list[ContactSummary]:
        """Contacts that were not merged into another, newest activity first."""
        ...

    def contact(self, tenant_id: UUID, contact_id: UUID) -> ContactDetail | None: ...


class ContactMerger(Protocol):
    def merge(self, tenant_id: UUID, source_id: UUID, target_id: UUID, actor: str) -> bool:
        """Move the source's identities, conversations and enquiries to the target and mark the
        source merged, with an admin event. False if either is unknown or already merged."""
        ...
