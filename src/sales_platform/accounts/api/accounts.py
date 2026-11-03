"""Sign-up, password login, email links, profile, invitations and creating a business (phase 2).

POST  /api/v1/auth/signup                 202 always "check your email" (Q7); 422 rules; 429
POST  /api/v1/auth/verify-email           {token}: 200, or 400 for any unusable link
POST  /api/v1/auth/verify-email/resend    signed in: a new verification email
POST  /api/v1/auth/login                  {email, password}: session cookie, or 401 for any failure
POST  /api/v1/auth/password/forgot        202 always
POST  /api/v1/auth/password/reset         {token, new_password}: 204; signs out everywhere
POST  /api/v1/auth/password/change        signed in: 204; signs out the other sessions
PATCH /api/v1/account/profile             signed in: display name
POST  /api/v1/invitations/preview         {token}: business, roles and a hint of the address
POST  /api/v1/invitations/accept          signed in: joins the business
POST  /api/v1/businesses                  signed in, verified email: 201 {tenant_id}

Requests without a session accept only application/json: a form on another website cannot
send JSON to us without a CORS preflight, which we never allow (CSRF protection for routes that
have no session yet). Tokens travel in request bodies, never in URLs we log.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.accounts.api.auth import SessionOut, SessionView, UserOut, user_out
from sales_platform.accounts.application.accounts import (
    BusinessService,
    ClientInfo,
    InvalidLink,
    InvitationService,
    LoginService,
    PasswordRejected,
    ProfileService,
    RateLimited,
    SignupService,
    parse_decimal,
)
from sales_platform.accounts.domain.accounts import InvalidEmail, LastAdministrator
from sales_platform.accounts.ports.accounts import (
    AlreadyMember,
    InvalidLogo,
    InvitationNotUsable,
    NewBusiness,
    NotAllowed,
    VersionConflict,
)
from sales_platform.platform.api.auth import PortalAuth, SignedIn, set_session_cookie
from sales_platform.platform.domain.security import AuthenticationFailed, hash_secret
from sales_platform.platform.domain.tenancy import PermissionDenied
from sales_platform.platform.domain.text import UnrenderableContent
from sales_platform.platform.ports.identity import IdentityRejected

logger = logging.getLogger(__name__)
_UNAVAILABLE = "Temporarily unavailable; try again"
_BAD_LINK = "This link is not valid any more"
_BAD_INVITATION = "This invitation cannot be used"


def guarded[T](action: Callable[[], T]) -> T:
    """One mapping from account errors to HTTP answers, shared by the phase 2 routers."""
    try:
        return action()
    except RateLimited as error:
        seconds = max(1, int(error.retry_after.total_seconds()))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts; try again later",
            headers={"Retry-After": str(seconds)},
        ) from None
    except PasswordRejected as error:
        detail = {"message": str(error), "problems": [p.value for p in error.problems]}
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from None
    except (PermissionDenied, NotAllowed) as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error) or "Not permitted") from None
    except InvitationNotUsable:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _BAD_INVITATION) from None
    except (AlreadyMember, LastAdministrator, VersionConflict) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None
    except (InvalidEmail, InvalidLogo, UnrenderableContent, ValueError) as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except (SQLAlchemyError, OSError):
        logger.exception("Account request failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None


def require_json(request: Request) -> None:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Send application/json")


def client_info(request: Request, trusted_proxies: int) -> ClientInfo:
    """The caller's IP. Behind N trusted proxies, the Nth address from the right of
    X-Forwarded-For (earlier entries can be forged by the client)."""
    if trusted_proxies > 0:
        hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",")]
        hops = [hop for hop in hops if hop]
        if len(hops) >= trusted_proxies:
            return ClientInfo(hops[-trusted_proxies])
    return ClientInfo(request.client.host if request.client else "unknown")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupIn(_Strict):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1000)
    display_name: str | None = Field(default=None, max_length=200)


class LoginIn(_Strict):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1000)


class EmailIn(_Strict):
    email: str = Field(max_length=320)


class TokenIn(_Strict):
    token: str = Field(max_length=200)


class ResetIn(_Strict):
    token: str = Field(max_length=200)
    new_password: str = Field(max_length=1000)


class ChangePasswordIn(_Strict):
    current_password: str = Field(max_length=1000)
    new_password: str = Field(max_length=1000)


class ProfileIn(_Strict):
    display_name: str | None = Field(default=None, max_length=200)


class StatusOut(BaseModel):
    status: str


class InvitationPreviewOut(BaseModel):
    business_name: str
    roles: list[str]
    email_hint: str
    expires_at: datetime


class TenantIdOut(BaseModel):
    tenant_id: UUID


class PricingStartIn(_Strict):
    currency: str | None = Field(default=None, max_length=3)
    tax_rate: str | None = Field(default=None, max_length=20)  # decimal as text, e.g. "0.18"


class NewBusinessIn(_Strict):
    name: str = Field(max_length=300)
    country: str = Field(max_length=2)
    time_zone: str = Field(max_length=64)
    quote_validity_days: int = Field(default=7, ge=1, le=3650)
    pricing: PricingStartIn | None = None


def build_account_router(
    auth: PortalAuth,
    session_out: SessionView,
    signup: SignupService,
    login: LoginService,
    profiles: ProfileService,
    invitations: InvitationService,
    businesses: BusinessService,
    cookie_secure: bool,
    trusted_proxies: int,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["accounts"])
    signed_in = auth.signed_in()
    Person = Annotated[SignedIn, Depends(signed_in)]
    Json = [Depends(require_json)]

    def client(request: Request) -> ClientInfo:
        return client_info(request, trusted_proxies)

    @router.post(
        "/auth/signup",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=StatusOut,
        dependencies=Json,
        responses={415: {}, 422: {}, 429: {}},
    )
    def sign_up(body: SignupIn, request: Request) -> StatusOut:
        guarded(
            lambda: signup.sign_up(body.email, body.password, body.display_name, client(request))
        )
        return StatusOut(status="check_email")

    @router.post(
        "/auth/verify-email", response_model=StatusOut, dependencies=Json, responses={400: {}}
    )
    def verify_email(body: TokenIn) -> StatusOut:
        try:
            guarded(lambda: signup.verify_email(body.token))
        except InvalidLink:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_LINK) from None
        return StatusOut(status="verified")

    @router.post(
        "/auth/verify-email/resend",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=StatusOut,
        responses={429: {}},
    )
    def resend(person: Person) -> StatusOut:
        guarded(lambda: signup.resend_verification(person.record.subject))
        return StatusOut(status="check_email")

    @router.post(
        "/auth/login",
        response_model=SessionOut,
        dependencies=Json,
        responses={401: {}, 415: {}, 429: {}},
    )
    def password_login(body: LoginIn, request: Request, response: Response) -> SessionOut:
        try:
            identity = guarded(lambda: login.login(body.email, body.password, client(request)))
            new = guarded(lambda: auth.sessions.start(identity))
            record = auth.sessions.authenticate(new.token)
        except (IdentityRejected, AuthenticationFailed):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Email or password is incorrect"
            ) from None
        set_session_cookie(
            response, new.token, int(auth.sessions.lifetime.total_seconds()), cookie_secure
        )
        response.headers["Cache-Control"] = "no-store"
        return guarded(lambda: session_out(SignedIn(record, new.token)))

    @router.post(
        "/auth/password/forgot",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=StatusOut,
        dependencies=Json,
        responses={429: {}},
    )
    def forgot(body: EmailIn, request: Request) -> StatusOut:
        guarded(lambda: login.forgot_password(body.email, client(request)))
        return StatusOut(status="check_email")

    @router.post(
        "/auth/password/reset",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=Json,
        responses={400: {}, 422: {}},
    )
    def reset(body: ResetIn) -> None:
        try:
            guarded(lambda: login.reset_password(body.token, body.new_password))
        except InvalidLink:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_LINK) from None

    @router.post(
        "/auth/password/change",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={400: {}, 422: {}, 429: {}},
    )
    def change(body: ChangePasswordIn, person: Person) -> None:
        try:
            guarded(
                lambda: login.change_password(
                    person.record.subject,
                    body.current_password,
                    body.new_password,
                    hash_secret(person.token),
                )
            )
        except IdentityRejected as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from None

    @router.patch("/account/profile", response_model=UserOut)
    def update_profile(body: ProfileIn, person: Person) -> UserOut:
        return user_out(guarded(lambda: profiles.update(person.record.subject, body.display_name)))

    @router.post(
        "/invitations/preview",
        response_model=InvitationPreviewOut,
        dependencies=Json,
        responses={404: {}, 429: {}},
    )
    def preview(body: TokenIn, request: Request) -> InvitationPreviewOut:
        try:
            found = guarded(lambda: invitations.preview(body.token, client(request)))
        except InvalidLink:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _BAD_INVITATION) from None
        return InvitationPreviewOut(
            business_name=found.business_name,
            roles=sorted(role.value for role in found.roles),
            email_hint=found.email,
            expires_at=found.expires_at,
        )

    @router.post("/invitations/accept", response_model=TenantIdOut, responses={404: {}, 429: {}})
    def accept(body: TokenIn, person: Person, request: Request) -> TenantIdOut:
        try:
            tenant_id = guarded(
                lambda: invitations.accept(person.record.subject, body.token, client(request))
            )
        except InvalidLink:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _BAD_INVITATION) from None
        return TenantIdOut(tenant_id=tenant_id)

    @router.post(
        "/businesses",
        status_code=status.HTTP_201_CREATED,
        response_model=TenantIdOut,
        responses={403: {}, 422: {}, 429: {}},
    )
    def create_business(body: NewBusinessIn, person: Person) -> TenantIdOut:
        def create() -> UUID:
            pricing = body.pricing or PricingStartIn()
            tax: Decimal | None = (
                None if pricing.tax_rate is None else parse_decimal(pricing.tax_rate, "Tax rate")
            )
            return businesses.create(
                person.record.subject,
                NewBusiness(
                    name=body.name,
                    country=body.country,
                    time_zone=body.time_zone,
                    quote_validity_days=body.quote_validity_days,
                    currency=pricing.currency,
                    tax_rate=tax,
                ),
            )

        return TenantIdOut(tenant_id=guarded(create))

    return router
