"""Portal session routes (moved from platform/api/auth.py, ADR 0026).

GET  /api/v1/auth/config     password login (always) and whether the dev login is offered
POST /api/v1/auth/dev-login  dev only: sign in as a stored synthetic member, sets the cookie
GET  /api/v1/auth/session    who is signed in, their businesses and the CSRF token
POST /api/v1/auth/logout     ends the session (needs the CSRF header)

Password sign-up and login live in accounts/api/accounts.py and use the same sessions. The
cookie, the CSRF check and the PortalAuth guard every router uses are in platform/api/auth.py.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.accounts.application.accounts import ProfileService
from sales_platform.accounts.domain.accounts import User
from sales_platform.platform.api.auth import (
    COOKIE_PATH,
    SESSION_COOKIE,
    PortalAuth,
    SignedIn,
    set_session_cookie,
)
from sales_platform.platform.application.sessions import csrf_token_for
from sales_platform.platform.domain.security import AuthenticationFailed
from sales_platform.platform.domain.tenancy import TenantContext, TenantStatus
from sales_platform.platform.ports.identity import IdentityProvider, IdentityRejected
from sales_platform.platform.ports.tenancy import MembershipDirectory

_UNAVAILABLE = "Temporarily unavailable; try again"


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
