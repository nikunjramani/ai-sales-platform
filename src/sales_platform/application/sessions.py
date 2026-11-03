"""Browser sessions for people using the portal.

After any identity provider verifies a person, a random session token is issued in an HttpOnly
cookie; the browser's JavaScript never sees it. Only its SHA-256 hash is stored.
State-changing requests must also send a CSRF token derived from the session token. Another
website can make the browser send the cookie, but cannot read or compute this header value.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sales_platform.application.api_keys import AuthenticationFailed, hash_secret
from sales_platform.ports.identity import SessionRecord, SessionStore, VerifiedIdentity

DEFAULT_LIFETIME = timedelta(hours=8)


def utc_now() -> datetime:
    return datetime.now(UTC)


def csrf_token_for(session_token: str) -> str:
    return hashlib.sha256(b"csrf:" + session_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewSession:
    token: str
    csrf_token: str
    expires_at: datetime


class SessionService:
    def __init__(
        self,
        store: SessionStore,
        lifetime: timedelta = DEFAULT_LIFETIME,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._lifetime = lifetime
        self._clock = clock

    @property
    def lifetime(self) -> timedelta:
        return self._lifetime

    def start(self, identity: VerifiedIdentity) -> NewSession:
        token = secrets.token_urlsafe(32)
        expires_at = self._clock() + self._lifetime
        self._store.create(uuid4(), hash_secret(token), identity, expires_at)
        return NewSession(token, csrf_token_for(token), expires_at)

    def authenticate(self, token: str | None) -> SessionRecord:
        if not token or len(token) > 200:
            raise AuthenticationFailed("Not signed in")
        record = self._store.find(hash_secret(token))
        if record is None or record.revoked or record.expires_at <= self._clock():
            raise AuthenticationFailed("Not signed in")
        return record

    def check_csrf(self, token: str, supplied: str | None) -> None:
        if supplied is None or not hmac.compare_digest(csrf_token_for(token), supplied):
            raise AuthenticationFailed("Missing or invalid CSRF token")

    def end(self, token: str | None) -> None:
        if token and len(token) <= 200:
            self._store.revoke(hash_secret(token))
