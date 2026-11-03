"""Accept/Decline references given to customers with a quotation (phase 4, D11, Q1)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class CustomerActionRef:
    """One offered version for one customer identity; answered at most once."""

    ref_id: UUID
    tenant_id: UUID
    quotation_id: UUID
    version: int
    content_sha256: str
    identity_id: UUID
    conversation_id: UUID
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class NewActionRef:
    tenant_id: UUID
    quotation_id: UUID
    version: int
    content_sha256: str
    identity_id: UUID
    conversation_id: UUID
    outbound_message_id: UUID
    expires_at: datetime
    token_sha256: str | None  # a secure link's token hash; None for button-only tools


class CustomerActionStore(Protocol):
    def issue(self, new: NewActionRef) -> UUID:
        """Create the reference once per (version, identity); a later call (a retried send)
        returns the same ID and replaces the link token, whose earlier value is unknown."""
        ...

    def get(self, tenant_id: UUID, ref_id: UUID) -> CustomerActionRef | None: ...

    def find_by_token(self, token_sha256: str) -> CustomerActionRef | None:
        """Before the tenant is known (the secure-link page): only the presented token's row."""
        ...

    def for_message(self, tenant_id: UUID, outbound_message_id: UUID) -> CustomerActionRef | None:
        """The reference offered with one outbound message (website chat quotation card)."""
        ...
