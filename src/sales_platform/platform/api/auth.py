"""The dependencies every portal router uses: the session cookie, the CSRF check and PortalAuth.

The session routes themselves (/api/v1/auth/...) are in accounts/api/auth.py (ADR 0026).

GET  /api/v1/auth/config     password login (always) and whether the dev login is offered
POST /api/v1/auth/dev-login  dev only: sign in as a stored synthetic member, sets the cookie
GET  /api/v1/auth/session    who is signed in, their businesses and the CSRF token
POST /api/v1/auth/logout     ends the session (needs the CSRF header)

Password sign-up and login live in api/accounts.py (phase 2) and use the same sessions.

The session token lives only in an HttpOnly, SameSite=Strict cookie, so page scripts cannot
read it and other sites cannot make the browser send it. Every state-changing request must
also carry the X-CSRF-Token header.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.platform.application.sessions import SessionService
from sales_platform.platform.application.tenancy import TenantContextResolver
from sales_platform.platform.domain.security import AuthenticationFailed
from sales_platform.platform.domain.tenancy import AccessDenied, TenantContext
from sales_platform.platform.ports.identity import SessionRecord

SESSION_COOKIE = "sales_session"
CSRF_HEADER = "X-CSRF-Token"
COOKIE_PATH = "/api"
_UNAVAILABLE = "Temporarily unavailable; try again"


@dataclass(frozen=True)
class SignedIn:
    """The current person plus their raw session token (needed for the CSRF check)."""

    record: SessionRecord
    token: str


@dataclass(frozen=True)
class PortalAuth:
    """FastAPI dependencies shared by every portal router."""

    sessions: SessionService
    resolver: TenantContextResolver

    def signed_in(self) -> Callable[[Request], SignedIn]:
        def dependency(request: Request) -> SignedIn:
            token = request.cookies.get(SESSION_COOKIE)
            try:
                record = self.sessions.authenticate(token)
            except AuthenticationFailed:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in") from None
            except SQLAlchemyError:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None
            assert token is not None
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                try:
                    self.sessions.check_csrf(token, request.headers.get(CSRF_HEADER))
                except AuthenticationFailed:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing CSRF token") from None
            return SignedIn(record, token)

        return dependency

    def tenant_context(self) -> Callable[[UUID, SignedIn], TenantContext]:
        signed_in = self.signed_in()

        def dependency(
            tenant_id: UUID, person: Annotated[SignedIn, Depends(signed_in)]
        ) -> TenantContext:
            try:
                return self.resolver.resolve(person.record.subject, tenant_id)
            except AccessDenied:
                # Same answer for "not a member" and "no such tenant".
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
            except SQLAlchemyError:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None

        return dependency


def set_session_cookie(response: Response, token: str, max_age: int, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path=COOKIE_PATH,
        httponly=True,
        secure=secure,
        samesite="strict",
    )
