"""Portal session routes, plus the dependencies other portal routes use.

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
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from quotation_app.application.accounts import ProfileService
from quotation_app.application.api_keys import AuthenticationFailed
from quotation_app.application.sessions import SessionService, csrf_token_for
from quotation_app.application.tenancy import TenantContextResolver
from quotation_app.domain.accounts import User
from quotation_app.domain.tenancy import AccessDenied, TenantContext, TenantStatus
from quotation_app.ports.identity import IdentityProvider, IdentityRejected, SessionRecord
from quotation_app.ports.tenancy import MembershipDirectory

SESSION_COOKIE = "quotation_session"
CSRF_HEADER = "X-CSRF-Token"
COOKIE_PATH = "/api"
_UNAVAILABLE = "Temporarily unavailable; try again"


@dataclass(frozen=True)
class SignedIn:
    """The current person plus their raw session token (needed for the CSRF check)."""

    record: SessionRecord
    token: str


class AuthConfigOut(BaseModel):
    password_login: Literal[True] = True
    dev_login: bool
    dev_subjects: list[str]


class DevLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=3, max_length=200)


class TenantOut(BaseModel):
    tenant_id: UUID
    name: str
    status: TenantStatus
    roles: list[str]
    permissions: list[str]


class UserOut(BaseModel):
    user_id: UUID
    email: str | None
    display_name: str | None
    email_verified: bool


class SessionOut(BaseModel):
    subject: str
    provider: str
    expires_at: datetime
    csrf_token: str
    user: UserOut | None
    tenants: list[TenantOut]


def user_out(user: User) -> UserOut:
    return UserOut(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        email_verified=user.email_verified,
    )


@dataclass(frozen=True)
class SessionView:
    """Builds the session answer: the person, their businesses (with status) and permissions."""

    directory: MembershipDirectory
    profiles: ProfileService

    def __call__(self, person: SignedIn) -> SessionOut:
        subject = person.record.subject
        user = self.profiles.get(subject)
        return SessionOut(
            subject=subject,
            provider=person.record.provider,
            expires_at=person.record.expires_at,
            csrf_token=csrf_token_for(person.token),
            user=None if user is None else user_out(user),
            tenants=[
                TenantOut(
                    tenant_id=item.tenant_id,
                    name=item.tenant_name,
                    status=item.status,
                    roles=sorted(role.value for role in item.roles),
                    permissions=sorted(
                        permission.value
                        for permission in TenantContext(
                            item.tenant_id, subject, item.roles
                        ).permissions
                    ),
                )
                for item in self.directory.tenants_for(subject)
            ],
        )


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


def build_auth_router(
    auth: PortalAuth,
    session_out: SessionView,
    dev: IdentityProvider | None,
    dev_subjects: list[str],
    cookie_secure: bool,
) -> APIRouter:
    """dev is the development identity provider, or None when the dev login is disabled."""
    router = APIRouter(prefix="/api/v1/auth", tags=["portal auth"])
    signed_in = auth.signed_in()

    @router.get("/config", response_model=AuthConfigOut)
    def config() -> AuthConfigOut:
        return AuthConfigOut(
            dev_login=dev is not None, dev_subjects=dev_subjects if dev is not None else []
        )

    @router.post("/dev-login", response_model=SessionOut, responses={401: {}, 404: {}})
    def dev_login(body: DevLoginIn, response: Response) -> SessionOut:
        if dev is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        try:
            identity = dev.verify(body.subject)
            new = auth.sessions.start(identity)
            record = auth.sessions.authenticate(new.token)
        except (IdentityRejected, AuthenticationFailed):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed") from None
        except SQLAlchemyError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None
        set_session_cookie(
            response, new.token, int(auth.sessions.lifetime.total_seconds()), cookie_secure
        )
        return session_out(SignedIn(record, new.token))

    @router.get("/session", response_model=SessionOut, responses={401: {}})
    def current(person: Annotated[SignedIn, Depends(signed_in)]) -> SessionOut:
        try:
            return session_out(person)
        except SQLAlchemyError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, responses={401: {}, 403: {}})
    def logout(person: Annotated[SignedIn, Depends(signed_in)], response: Response) -> None:
        auth.sessions.end(person.token)
        response.delete_cookie(
            SESSION_COOKIE, path=COOKIE_PATH, httponly=True, secure=cookie_secure, samesite="strict"
        )

    return router
