"""Staff views of customers (phase 4): inbox, conversation, contacts, merge, acknowledgement.

- Every staff role may look (conversations:view). Viewers see email and phone masked; other
  roles in full (contacts:details, Q12). Customer text is returned as plain text only.
- Merging two contacts (D13): administrators and sales operators (contacts:merge), never
  automatic, no undo; recorded in the admin event log.
- The "we are preparing your quotation" setting (D14): administrators (tenant:manage).
"""

from dataclasses import replace
from typing import Protocol
from uuid import UUID

from sales_platform.domain.contacts import mask_email, mask_phone
from sales_platform.domain.tenancy import Permission, TenantContext
from sales_platform.ports.contacts import (
    ContactDetail,
    ContactMerger,
    ContactReader,
    ContactSummary,
    ConversationSummary,
    IdentityView,
    ThreadEntry,
)

INBOX_LIMIT = 100
CONTACT_LIMIT = 100


class ContactNotFound(Exception):
    pass


class CustomerMessageSettings(Protocol):
    def acknowledge_enquiries(self, tenant_id: UUID) -> bool: ...

    def set_acknowledge_enquiries(self, tenant_id: UUID, value: bool, actor: str) -> None: ...


class ContactService:
    def __init__(
        self,
        reader: ContactReader,
        merger: ContactMerger,
        settings: CustomerMessageSettings,
    ) -> None:
        self._reader = reader
        self._merger = merger
        self._settings = settings

    def inbox(self, context: TenantContext) -> list[ConversationSummary]:
        context.require(Permission.VIEW_CONVERSATIONS)
        return self._reader.conversations(context.tenant_id, INBOX_LIMIT)

    def conversation(
        self, context: TenantContext, conversation_id: UUID
    ) -> tuple[ConversationSummary, IdentityView, list[ThreadEntry]]:
        context.require(Permission.VIEW_CONVERSATIONS)
        found = self._reader.conversation(context.tenant_id, conversation_id)
        if found is None:
            raise ContactNotFound("Not found")
        return found

    def enquiry_thread(self, context: TenantContext, enquiry_id: UUID) -> list[ThreadEntry]:
        context.require(Permission.VIEW_CONVERSATIONS)
        return self._reader.enquiry_thread(context.tenant_id, enquiry_id)

    def contacts(self, context: TenantContext, query: str | None) -> list[ContactSummary]:
        context.require(Permission.VIEW_CONVERSATIONS)
        found = self._reader.contacts(context.tenant_id, (query or "").strip()[:100], CONTACT_LIMIT)
        return [self._visible(context, item) for item in found]

    def contact(self, context: TenantContext, contact_id: UUID) -> ContactDetail:
        context.require(Permission.VIEW_CONVERSATIONS)
        found = self._reader.contact(context.tenant_id, contact_id)
        if found is None:
            raise ContactNotFound("Not found")
        return replace(
            found,
            contact=self._visible(context, found.contact),
            suggestions=tuple(self._visible(context, item) for item in found.suggestions),
        )

    def merge(self, context: TenantContext, source_id: UUID, target_id: UUID) -> None:
        """Move everything of source into target. No undo (D13)."""
        context.require(Permission.MERGE_CONTACTS)
        if not self._merger.merge(context.tenant_id, source_id, target_id, context.subject):
            raise ContactNotFound("Not found")

    def acknowledge_enquiries(self, context: TenantContext) -> bool:
        context.require(Permission.VIEW_CONVERSATIONS)
        return self._settings.acknowledge_enquiries(context.tenant_id)

    def set_acknowledge_enquiries(self, context: TenantContext, value: bool) -> None:
        context.require(Permission.MANAGE_TENANT)
        self._settings.set_acknowledge_enquiries(context.tenant_id, value, context.subject)

    @staticmethod
    def _visible(context: TenantContext, contact: ContactSummary) -> ContactSummary:
        if Permission.VIEW_CONTACT_DETAILS in context.permissions:
            return contact
        return replace(contact, email=mask_email(contact.email), phone=mask_phone(contact.phone))
