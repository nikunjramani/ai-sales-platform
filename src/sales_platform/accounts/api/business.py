"""A business's own settings, setup checklist, members, invitations and API keys (phase 2).

All under /api/v1/tenants/{t}; a non-member gets 404 like for an unknown business.

GET    /business                         member: name, country, time zone, status, PDF details
PUT    /business                         tenant:manage: name, country, time zone
PUT    /business/profile                 tenant:manage: PDF details (expected_version, 409)
PUT    /business/logo   DELETE /business/logo   tenant:manage: base64 PNG/JPEG <= 1 MB
GET    /business/logo                    member: the normalised PNG
GET    /pricing-policy  PUT /pricing-policy     member / tenant:manage (decimals as strings)
GET    /setup                            member: checklist computed from real rows
POST   /go-live                          tenant:manage: 200, or 409 {missing: [...]}
GET    /members                          members:manage
PUT    /members/{user_id}/roles          members:manage; 409 for the last administrator
DELETE /members/{user_id}                members:manage, or yourself (leaving)
GET    /invitations  POST /invitations   members:manage (the link is only ever in the email)
POST   /invitations/{id}/resend  DELETE /invitations/{id}
GET    /api-keys  POST /api-keys  DELETE /api-keys/{key_id}   integrations:manage
       (the new key is shown once, with Cache-Control: no-store)
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from sales_platform.accounts.api.accounts import guarded
from sales_platform.accounts.application.accounts import (
    ApiKeyAdminService,
    BusinessService,
    InvitationService,
    MemberService,
    ProfileService,
    parse_decimal,
)
from sales_platform.accounts.domain.accounts import REQUIRED_FOR_GO_LIVE, SetupItemKey
from sales_platform.accounts.ports.accounts import PricingSettings, ProfileUpdate
from sales_platform.platform.api.auth import PortalAuth
from sales_platform.platform.domain.tenancy import TenantContext, TenantStatus


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileOut(BaseModel):
    legal_name: str | None
    address: str | None
    tax_id_label: str | None
    tax_id: str | None
    has_logo: bool
    version: int


class BusinessOut(BaseModel):
    tenant_id: UUID
    name: str
    country: str | None
    time_zone: str | None
    status: TenantStatus
    went_live_at: datetime | None
    profile: ProfileOut


class BusinessIn(_Strict):
    name: str = Field(max_length=300)
    country: str = Field(max_length=2)
    time_zone: str = Field(max_length=64)


class ProfileIn(_Strict):
    legal_name: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=600)
    tax_id_label: str | None = Field(default=None, max_length=50)
    tax_id: str | None = Field(default=None, max_length=100)
    expected_version: int = Field(ge=1)


class VersionOut(BaseModel):
    version: int


class LogoIn(_Strict):
    # 1 MB of file is about 1.37 MB of base64; the decoder and the normaliser check the rest.
    content_base64: str = Field(max_length=1_400_000)


class PricingOut(BaseModel):
    currency: str | None
    minor_units: int | None
    rounding_mode: str | None
    tax_rate: str | None
    max_price_age_seconds: int | None
    max_stock_age_seconds: int | None
    quote_validity_days: int
    complete: bool
    version: int


class PricingIn(_Strict):
    currency: str = Field(max_length=3)
    minor_units: int
    rounding_mode: str = Field(max_length=16)
    tax_rate: str = Field(max_length=20)
    max_price_age_seconds: int
    max_stock_age_seconds: int
    quote_validity_days: int = Field(ge=1, le=3650)
    expected_version: int = Field(ge=1)


class SetupItemOut(BaseModel):
    key: SetupItemKey
    done: bool
    required: bool


class SetupOut(BaseModel):
    status: TenantStatus
    items: list[SetupItemOut]
    can_go_live: bool


class MemberOut(BaseModel):
    user_id: UUID
    email: str | None
    display_name: str | None
    roles: list[str]
    is_you: bool


class RolesIn(_Strict):
    roles: list[str] = Field(min_length=1, max_length=4)


class InviteIn(_Strict):
    email: str = Field(max_length=320)
    roles: list[str] = Field(min_length=1, max_length=4)


class InvitationOut(BaseModel):
    invitation_id: UUID
    email: str
    roles: list[str]
    status: str
    email_status: str
    expires_at: datetime | None
    invited_by: str
    created_at: datetime


class InvitationIdOut(BaseModel):
    invitation_id: UUID


class ApiKeyIn(_Strict):
    name: str = Field(max_length=200)


class ApiKeyOut(BaseModel):
    key_id: UUID
    name: str
    role: str
    created_at: datetime
    created_by: str | None
    revoked_at: datetime | None


class NewApiKeyOut(BaseModel):
    key_id: UUID
    token: str


def build_business_router(
    auth: PortalAuth,
    businesses: BusinessService,
    members: MemberService,
    invitations: InvitationService,
    keys: ApiKeyAdminService,
    profiles: ProfileService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["business settings"])
    Context = Annotated[TenantContext, Depends(auth.tenant_context())]
    not_found = HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    @router.get("/business", response_model=BusinessOut)
    def get_business(context: Context) -> BusinessOut:
        found = guarded(lambda: businesses.settings(context))
        profile = found.profile
        return BusinessOut(
            tenant_id=found.tenant_id,
            name=found.name,
            country=found.country,
            time_zone=found.time_zone,
            status=found.status,
            went_live_at=found.went_live_at,
            profile=ProfileOut(
                legal_name=profile.legal_name,
                address=profile.address,
                tax_id_label=profile.tax_id_label,
                tax_id=profile.tax_id,
                has_logo=profile.has_logo,
                version=profile.version,
            ),
        )

    @router.put("/business", status_code=status.HTTP_204_NO_CONTENT)
    def update_business(body: BusinessIn, context: Context) -> None:
        guarded(lambda: businesses.update_details(context, body.name, body.country, body.time_zone))

    @router.put("/business/profile", response_model=VersionOut, responses={409: {}, 422: {}})
    def update_profile(body: ProfileIn, context: Context) -> VersionOut:
        update = ProfileUpdate(body.legal_name, body.address, body.tax_id_label, body.tax_id)
        version = guarded(lambda: businesses.update_profile(context, update, body.expected_version))
        return VersionOut(version=version)

    @router.put("/business/logo", status_code=status.HTTP_204_NO_CONTENT, responses={422: {}})
    def upload_logo(body: LogoIn, context: Context) -> None:
        guarded(lambda: businesses.upload_logo(context, body.content_base64))

    @router.delete("/business/logo", status_code=status.HTTP_204_NO_CONTENT)
    def remove_logo(context: Context) -> None:
        guarded(lambda: businesses.remove_logo(context))

    @router.get("/business/logo", responses={200: {"content": {"image/png": {}}}, 404: {}})
    def get_logo(context: Context) -> Response:
        data = guarded(lambda: businesses.logo(context))
        if data is None:
            raise not_found
        return Response(
            data,
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    @router.get("/pricing-policy", response_model=PricingOut)
    def get_pricing(context: Context) -> PricingOut:
        stored = guarded(lambda: businesses.pricing(context))
        settings = stored.settings
        return PricingOut(
            currency=settings.currency,
            minor_units=settings.minor_units,
            rounding_mode=settings.rounding_mode,
            tax_rate=None
            if settings.tax_rate is None
            else format(settings.tax_rate.normalize(), "f"),
            max_price_age_seconds=settings.max_price_age_seconds,
            max_stock_age_seconds=settings.max_stock_age_seconds,
            quote_validity_days=settings.quote_validity_days,
            complete=settings.complete,
            version=stored.version,
        )

    @router.put("/pricing-policy", response_model=VersionOut, responses={409: {}, 422: {}})
    def update_pricing(body: PricingIn, context: Context) -> VersionOut:
        def save() -> int:
            settings = PricingSettings(
                currency=body.currency,
                minor_units=body.minor_units,
                rounding_mode=body.rounding_mode,
                tax_rate=parse_decimal(body.tax_rate, "Tax rate"),
                max_price_age_seconds=body.max_price_age_seconds,
                max_stock_age_seconds=body.max_stock_age_seconds,
                quote_validity_days=body.quote_validity_days,
            )
            return businesses.update_pricing(context, settings, body.expected_version)

        return VersionOut(version=guarded(save))

    @router.get("/setup", response_model=SetupOut)
    def setup(context: Context) -> SetupOut:
        items = guarded(lambda: businesses.checklist(context))
        settings = guarded(lambda: businesses.settings(context))
        done = {item.key for item in items if item.done}
        return SetupOut(
            status=settings.status,
            items=[SetupItemOut(key=i.key, done=i.done, required=i.required) for i in items],
            can_go_live=settings.status is TenantStatus.SETTING_UP and REQUIRED_FOR_GO_LIVE <= done,
        )

    @router.post("/go-live", response_model=SetupOut, responses={403: {}, 409: {}})
    def go_live(context: Context) -> SetupOut:
        missing = guarded(lambda: businesses.go_live(context))
        if missing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"message": "Finish the setup first", "missing": [key.value for key in missing]},
            )
        return setup(context)

    @router.get("/members", response_model=list[MemberOut])
    def list_members(context: Context) -> list[MemberOut]:
        return [
            MemberOut(
                user_id=member.user_id,
                email=member.email,
                display_name=member.display_name,
                roles=sorted(role.value for role in member.roles),
                is_you=member.subject == context.subject,
            )
            for member in guarded(lambda: members.list(context))
        ]

    @router.put(
        "/members/{user_id}/roles",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={403: {}, 409: {}, 422: {}},
    )
    def change_roles(user_id: UUID, body: RolesIn, context: Context) -> None:
        guarded(lambda: members.change_roles(context, user_id, body.roles))

    @router.delete(
        "/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, responses={403: {}, 409: {}}
    )
    def remove_member(user_id: UUID, context: Context) -> None:
        def remove() -> None:
            me = profiles.get(context.subject)
            members.remove(context, user_id, None if me is None else me.user_id)

        guarded(remove)

    @router.get("/invitations", response_model=list[InvitationOut])
    def list_invitations(context: Context) -> list[InvitationOut]:
        found = guarded(lambda: invitations.list(context))
        now = datetime.now().astimezone()
        return [
            InvitationOut(
                invitation_id=item.invitation_id,
                email=item.email,
                roles=sorted(role.value for role in item.roles),
                status=item.status(now).value,
                email_status=item.email_status,
                expires_at=item.expires_at,
                invited_by=item.invited_by,
                created_at=item.created_at,
            )
            for item in found
        ]

    @router.post(
        "/invitations",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=InvitationIdOut,
        responses={403: {}, 409: {}, 422: {}, 429: {}},
    )
    def invite(body: InviteIn, context: Context) -> InvitationIdOut:
        invitation_id = guarded(lambda: invitations.invite(context, body.email, body.roles))
        return InvitationIdOut(invitation_id=invitation_id)

    @router.post(
        "/invitations/{invitation_id}/resend",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=InvitationIdOut,
        responses={404: {}, 429: {}},
    )
    def resend(invitation_id: UUID, context: Context) -> InvitationIdOut:
        if not guarded(lambda: invitations.resend(context, invitation_id)):
            raise not_found
        return InvitationIdOut(invitation_id=invitation_id)

    @router.delete(
        "/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT, responses={404: {}}
    )
    def revoke_invitation(invitation_id: UUID, context: Context) -> None:
        if not guarded(lambda: invitations.revoke(context, invitation_id)):
            raise not_found

    @router.get("/api-keys", response_model=list[ApiKeyOut])
    def list_keys(context: Context) -> list[ApiKeyOut]:
        return [
            ApiKeyOut(
                key_id=key.key_id,
                name=key.name,
                role=key.role.value,
                created_at=key.created_at,
                created_by=key.created_by,
                revoked_at=key.revoked_at,
            )
            for key in guarded(lambda: keys.list(context))
        ]

    @router.post(
        "/api-keys",
        status_code=status.HTTP_201_CREATED,
        response_model=NewApiKeyOut,
        responses={403: {}, 422: {}},
    )
    def create_key(body: ApiKeyIn, context: Context, response: Response) -> NewApiKeyOut:
        new = guarded(lambda: keys.create(context, body.name))
        response.headers["Cache-Control"] = "no-store"
        return NewApiKeyOut(key_id=new.key_id, token=new.token)

    @router.delete(
        "/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, responses={404: {}}
    )
    def revoke_key(key_id: UUID, context: Context) -> None:
        if not guarded(lambda: keys.revoke(context, key_id)):
            raise not_found

    return router
