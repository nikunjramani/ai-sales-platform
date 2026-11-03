"""Login and session contracts, independent of the identity provider.

An identity provider (the dev login now, OIDC later) only proves who someone is and returns a
VerifiedIdentity. Sessions, tenant membership and permissions are the same for every provider.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from quotation_app.domain.validation import require_text


class IdentityRejected(Exception):
    """The provider could not verify the person. One message for every reason."""


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str  # stable person ID: "user:<uuid>" for password sign-ups, an email for demo people
    provider: str

    def __post_init__(self) -> None:
        require_text(self.subject, "subject")
        require_text(self.provider, "provider", 32)


class IdentityProvider(Protocol):
    """Proves who someone is (the dev login now, OIDC later); raises IdentityRejected."""

    def verify(self, subject: str) -> VerifiedIdentity: ...


@dataclass(frozen=True)
class SessionRecord:
    session_id: UUID
    subject: str
    provider: str
    expires_at: datetime
    revoked: bool


class SessionStore(Protocol):
    """Only a SHA-256 hash of the session token is stored."""

    def create(
        self,
        session_id: UUID,
        token_sha256: str,
        identity: VerifiedIdentity,
        expires_at: datetime,
    ) -> None: ...

    def find(self, token_sha256: str) -> SessionRecord | None: ...

    def revoke(self, token_sha256: str) -> None: ...

    def revoke_all_for(self, subject: str, keep_token_sha256: str | None = None) -> int:
        """Sign the person out everywhere (except the kept session); returns how many."""
        ...
