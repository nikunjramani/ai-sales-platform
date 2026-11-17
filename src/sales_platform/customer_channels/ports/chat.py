"""Storage for hosted customer chat: public addresses, visitor sessions, the visitor's thread."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sales_platform.integrations.domain.connections import Connection
from sales_platform.platform.domain.tenancy import TenantStatus


@dataclass(frozen=True)
class ChatEndpoint:
    """A chat found by its public handle, before anyone is signed in."""

    connection: Connection
    business_name: str
    tenant_status: TenantStatus


@dataclass(frozen=True)
class ChatSession:
    session_id: UUID
    tenant_id: UUID
    connection_id: UUID
    identity_id: UUID
    conversation_id: UUID
    external_id: str  # the visitor's identity on this connection ("v_<uuid>")
    expires_at: datetime


@dataclass(frozen=True)
class StoredSession:
    session: ChatSession
    secret_sha256: str
    revoked: bool


@dataclass(frozen=True)
class ThreadMessage:
    """One message as the visitor sees it. Our messages appear once they were sent."""

    seq: int
    direction: str  # inbound | outbound
    template_key: str | None
    text: str
    at: datetime
    outbound_message_id: UUID | None
    metadata: Mapping[str, str]
    document_id: UUID | None
    action_ref: UUID | None  # Accept/Decline reference offered with this message
    action_ref_open: bool  # not answered and not expired
