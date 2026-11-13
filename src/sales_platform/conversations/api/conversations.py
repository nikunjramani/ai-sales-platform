"""Portal routes for customers and conversations (phase 4), session cookie + CSRF as usual.

GET  /api/v1/tenants/{t}/conversations                 inbox, newest activity first
GET  /api/v1/tenants/{t}/conversations/{id}            header, identity and every message
GET  /api/v1/tenants/{t}/enquiries/{id}/messages       the messages of one enquiry
GET  /api/v1/tenants/{t}/contacts?q=                   contacts (viewers: masked details)
GET  /api/v1/tenants/{t}/contacts/{id}                 identities, enquiries, merge suggestions
POST /api/v1/tenants/{t}/contacts/{id}/merge           {into_contact_id}; no undo (D13)
GET|PUT /api/v1/tenants/{t}/customer-messages          {acknowledge_enquiries} (D14)
POST /api/v1/tenants/{t}/messages/{id}/resend          a failed customer message again, as a
                                                       new message (phase 5); 404 otherwise
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from sales_platform.conversations.application.contacts import ContactNotFound, ContactService
from sales_platform.conversations.application.resend import MessageNotResendable, ResendService
from sales_platform.conversations.ports.contacts import (
    ContactEnquiry,
    ContactSummary,
    ConversationSummary,
    IdentityView,
    ThreadEntry,
)
from sales_platform.platform.api.auth import PortalAuth
from sales_platform.platform.domain.tenancy import TenantContext


class ConversationOut(BaseModel):
    conversation_id: UUID
    contact_id: UUID
    contact_name: str | None
    provider_key: str
    connection_name: str
    last_text: str
    last_direction: str
    last_at: datetime
    enquiry_id: UUID | None
    enquiry_status: str | None
    quotation_id: UUID | None
    quotation_status: str | None


class MessageOut(BaseModel):
    seq: int
    direction: str
    author: str
    text: str
    at: datetime
    enquiry_id: UUID | None
    template_key: str | None
    outbound_status: str | None
    outbound_error: str | None
    outbound_message_id: UUID | None = None
    can_resend: bool = False  # a failed customer message not resent yet (phase 5)


class ResentOut(BaseModel):
    message_id: UUID


class IdentityOut(BaseModel):
    identity_id: UUID
    provider_key: str
    connection_name: str
    external_id: str
    verified: bool
    consent: str


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    identity: IdentityOut
    messages: list[MessageOut]


class ContactOut(BaseModel):
    contact_id: UUID
    name: str | None
    company: str | None
    email: str | None
    phone: str | None
    email_verified: bool
    phone_verified: bool
    identities: int
    last_activity: datetime | None


class ContactEnquiryOut(BaseModel):
    enquiry_id: UUID
    status: str
    received_at: datetime
    quotation_id: UUID | None
    quotation_status: str | None


class ContactDetailOut(BaseModel):
    contact: ContactOut
    identities: list[IdentityOut]
    conversations: list[UUID]
    enquiries: list[ContactEnquiryOut]
    suggestions: list[ContactOut]


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    into_contact_id: UUID


class CustomerMessagesIO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_enquiries: bool


def _conversation(item: ConversationSummary) -> ConversationOut:
    return ConversationOut(**vars(item))


def _message(item: ThreadEntry) -> MessageOut:
    values = vars(item)
    return MessageOut(
        **{name: value for name, value in values.items() if name in MessageOut.model_fields},
        can_resend=item.outbound_status == "failed"
        and item.outbound_purpose == "customer_messages"
        and not item.resent,
    )


def _identity(item: IdentityView) -> IdentityOut:
    return IdentityOut(**vars(item))


def _contact(item: ContactSummary) -> ContactOut:
    return ContactOut(**vars(item))


def _enquiry(item: ContactEnquiry) -> ContactEnquiryOut:
    return ContactEnquiryOut(**vars(item))


def build_conversation_router(
    auth: PortalAuth, contacts: ContactService, resend: ResendService | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["portal conversations"])
    Context = Annotated[TenantContext, Depends(auth.tenant_context())]

    def guarded[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except (ContactNotFound, MessageNotResendable):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None

    @router.get("/conversations", response_model=list[ConversationOut])
    def inbox(context: Context) -> list[ConversationOut]:
        return [_conversation(item) for item in guarded(lambda: contacts.inbox(context))]

    @router.get(
        "/conversations/{conversation_id}",
        response_model=ConversationDetailOut,
        responses={404: {}},
    )
    def conversation(conversation_id: UUID, context: Context) -> ConversationDetailOut:
        head, identity, messages = guarded(lambda: contacts.conversation(context, conversation_id))
        return ConversationDetailOut(
            conversation=_conversation(head),
            identity=_identity(identity),
            messages=[_message(item) for item in messages],
        )

    @router.get("/enquiries/{enquiry_id}/messages", response_model=list[MessageOut])
    def enquiry_messages(enquiry_id: UUID, context: Context) -> list[MessageOut]:
        return [
            _message(item) for item in guarded(lambda: contacts.enquiry_thread(context, enquiry_id))
        ]

    @router.get("/contacts", response_model=list[ContactOut])
    def list_contacts(
        context: Context, q: Annotated[str | None, Query(max_length=100)] = None
    ) -> list[ContactOut]:
        return [_contact(item) for item in guarded(lambda: contacts.contacts(context, q))]

    @router.get("/contacts/{contact_id}", response_model=ContactDetailOut, responses={404: {}})
    def contact(contact_id: UUID, context: Context) -> ContactDetailOut:
        found = guarded(lambda: contacts.contact(context, contact_id))
        return ContactDetailOut(
            contact=_contact(found.contact),
            identities=[_identity(item) for item in found.identities],
            conversations=list(found.conversations),
            enquiries=[_enquiry(item) for item in found.enquiries],
            suggestions=[_contact(item) for item in found.suggestions],
        )

    @router.post(
        "/contacts/{contact_id}/merge",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={403: {}, 404: {}},
    )
    def merge(contact_id: UUID, body: MergeIn, context: Context) -> None:
        guarded(lambda: contacts.merge(context, contact_id, body.into_contact_id))

    @router.get("/customer-messages", response_model=CustomerMessagesIO)
    def get_customer_messages(context: Context) -> CustomerMessagesIO:
        value = guarded(lambda: contacts.acknowledge_enquiries(context))
        return CustomerMessagesIO(acknowledge_enquiries=value)

    @router.put("/customer-messages", response_model=CustomerMessagesIO, responses={403: {}})
    def set_customer_messages(body: CustomerMessagesIO, context: Context) -> CustomerMessagesIO:
        guarded(lambda: contacts.set_acknowledge_enquiries(context, body.acknowledge_enquiries))
        return body

    @router.post(
        "/messages/{message_id}/resend",
        response_model=ResentOut,
        status_code=status.HTTP_201_CREATED,
        responses={403: {}, 404: {}},
    )
    def resend_message(message_id: UUID, context: Context) -> ResentOut:
        if resend is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        return ResentOut(message_id=guarded(lambda: resend.resend(context, message_id)))

    return router
