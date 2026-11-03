"""Application factory. Startup does not mutate or migrate the database."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from sales_platform.accounts.adapters.accounts.password import (
    Argon2PasswordHasher,
    load_common_passwords,
)
from sales_platform.accounts.adapters.documents.logo import PillowLogoNormalizer
from sales_platform.accounts.api.accounts import build_account_router
from sales_platform.accounts.api.auth import SessionView, build_auth_router
from sales_platform.accounts.api.business import build_business_router
from sales_platform.accounts.application.accounts import (
    ApiKeyAdminService,
    BusinessService,
    InvitationService,
    LoginService,
    MemberService,
    ProfileService,
    SignupService,
)
from sales_platform.accounts.application.api_keys import ApiKeyAuthenticator
from sales_platform.accounts.persistence.accounts import (
    SqlInvitationStore,
    SqlMemberStore,
    SqlUserStore,
)
from sales_platform.accounts.persistence.api_keys import SqlApiKeyAdminStore, SqlApiKeyReader
from sales_platform.catalogue.api.catalogue import build_catalogue_router
from sales_platform.catalogue.application.catalogue_admin import ProductService
from sales_platform.composition import (
    DEV_PUBLIC_BASE_URL,
    build_catalogue_store,
    build_core,
    build_runtime,
)
from sales_platform.conversations.api.conversations import build_conversation_router
from sales_platform.conversations.application.contacts import ContactService
from sales_platform.conversations.application.resend import ResendService
from sales_platform.conversations.persistence.contacts import (
    SqlContactMerger,
    SqlContactReader,
    SqlCustomerMessageSettings,
)
from sales_platform.customer_channels.api.enquiries import build_enquiry_router
from sales_platform.customer_channels.api.public_chat import build_public_chat_router
from sales_platform.customer_channels.application.chat import HostedChatService
from sales_platform.customer_channels.application.customer_inbox import CustomerInbox
from sales_platform.customer_channels.persistence.chat import SqlChatStore
from sales_platform.enquiries.application.follow_ups import default_router
from sales_platform.enquiries.application.intake import EnquiryIntakeService, EnquiryListService
from sales_platform.enquiries.persistence.intake import SqlEnquiryListReader, SqlIntakeStore
from sales_platform.integrations.api.connections import build_connection_router
from sales_platform.integrations.api.hooks import build_hook_router
from sales_platform.integrations.application.connections import ConnectionService
from sales_platform.integrations.application.inbound import InboundService
from sales_platform.integrations.persistence.connections import (
    SqlHookLookup,
    SqlHostedChatAdmin,
)
from sales_platform.integrations.persistence.provider_state import (
    SqlRouteLookup,
)
from sales_platform.orders.api.orders import build_order_router
from sales_platform.platform.adapters.identity.dev import DevIdentityProvider
from sales_platform.platform.adapters.storage.local import LocalObjectStorage
from sales_platform.platform.api.auth import PortalAuth
from sales_platform.platform.application.sessions import SessionService
from sales_platform.platform.config import Settings
from sales_platform.platform.persistence.database import build_engine, database_ready
from sales_platform.platform.persistence.rate_limits import SqlRateLimiter
from sales_platform.platform.persistence.sessions import SqlSessionStore
from sales_platform.quotations.adapters.documents.pdf import ReportLabQuotationRenderer
from sales_platform.quotations.api.approval_links import build_approval_link_router
from sales_platform.quotations.api.enrollments import build_enrollment_router
from sales_platform.quotations.api.media import build_media_router
from sales_platform.quotations.api.portal import build_portal_router
from sales_platform.quotations.application.approval_links import ApprovalLinkService
from sales_platform.quotations.application.documents import QuotationDocumentService
from sales_platform.quotations.application.media import MediaService
from sales_platform.quotations.application.quotations import QuotationService
from sales_platform.quotations.persistence.documents import SqlDocumentReader
from sales_platform.quotations.persistence.manager_replies import SqlApprovalLinks
from sales_platform.quotations.persistence.media import SqlMediaLinks
from sales_platform.quotations.persistence.quotations import (
    SqlQuotationReader,
    SqlRevisionStore,
)
from sales_platform.seed import DEMO_MEMBERSHIPS


class HealthResponse(BaseModel):
    status: Literal["ok", "ready", "not_ready"]


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()
    engine = build_engine(configuration.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Sales Platform", version="0.1.0", lifespan=lifespan)

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
        tags=["health"],
    )
    def ready(response: Response) -> HealthResponse:
        try:
            available = database_ready(engine)
        except SQLAlchemyError:
            available = False
        if not available:
            response.status_code = 503
        return HealthResponse(status="ready" if available else "not_ready")

    # Composition root: wire adapters to application services. Nothing connects until used.
    storage = LocalObjectStorage(configuration.storage_dir)
    runtime = build_runtime(configuration, engine)
    # The services the worker uses too, built by the same function (ADR 0026 step 1): each
    # business's product source, pricing, customer answers, approvals, connections, orders...
    core = build_core(configuration, engine, runtime)
    connections = core.connections
    intake = EnquiryIntakeService(SqlIntakeStore(engine), storage, connections, runtime.registry)
    # Quotation PDFs (M5): previews for staff, approved documents for staff and customers.
    documents = QuotationDocumentService(
        SqlDocumentReader(engine), storage, ReportLabQuotationRenderer()
    )
    pricing = core.pricing
    responses = core.responses
    business_store = core.businesses
    orders = core.orders
    app.include_router(
        build_enquiry_router(
            ApiKeyAuthenticator(SqlApiKeyReader(engine)),
            intake,
            documents,
            responses,
            business_store,
            orders,
        )
    )

    # Portal (M4): sessions, quotations, approvals and manager channels.
    memberships = core.memberships
    resolver = core.resolver
    session_store = SqlSessionStore(engine)
    auth = PortalAuth(
        SessionService(session_store, timedelta(hours=configuration.session_hours)), resolver
    )
    users = SqlUserStore(engine)
    profiles = ProfileService(users)
    session_view = SessionView(memberships, profiles)
    dev_login = configuration.dev_login_enabled
    app.include_router(
        build_auth_router(
            auth,
            session_view,
            DevIdentityProvider(memberships) if dev_login else None,
            sorted({subject for _, subject, _ in DEMO_MEMBERSHIPS}) if dev_login else [],
            configuration.cookie_secure,
        )
    )
    # Accounts and onboarding (phase 2): sign-up, password login, businesses, members, keys.
    hasher = Argon2PasswordHasher(max_concurrent=configuration.password_hash_concurrency)
    common = load_common_passwords()
    limiter = SqlRateLimiter(engine)
    catalogue_store = build_catalogue_store(engine)
    products = ProductService(
        catalogue_store, catalogue_store, business_store, connections, runtime.registry
    )
    businesses = BusinessService(
        business_store,
        users,
        limiter,
        storage,
        PillowLogoNormalizer(),
        product_data=products.has_product_data,
    )
    invitations = InvitationService(SqlInvitationStore(engine), business_store, users, limiter)
    app.include_router(
        build_account_router(
            auth,
            session_view,
            SignupService(users, hasher, limiter, common),
            LoginService(users, hasher, limiter, session_store, common),
            profiles,
            invitations,
            businesses,
            configuration.cookie_secure,
            configuration.trusted_proxy_count,
        )
    )
    app.include_router(
        build_business_router(
            auth,
            businesses,
            MemberService(SqlMemberStore(engine), business_store),
            invitations,
            ApiKeyAdminService(SqlApiKeyAdminStore(engine), business_store),
            profiles,
        )
    )
    approvals = core.approvals
    channels = core.channel_actions
    lifecycle = core.lifecycle
    app.include_router(
        build_portal_router(
            auth,
            QuotationService(
                SqlQuotationReader(engine),
                SqlRevisionStore(engine),
                pricing,
            ),
            approvals,
            channels,
            EnquiryListService(SqlEnquiryListReader(engine)),
            documents,
            responses,
            connections,
        )
    )
    app.include_router(build_order_router(auth, orders))
    app.include_router(
        build_catalogue_router(
            auth,
            products,
        )
    )
    # The signed approval link page (phase 5, P5-14): sign-in, then one click.
    app.include_router(
        build_approval_link_router(
            auth,
            ApprovalLinkService(
                SqlApprovalLinks(engine),
                core.reply_store,
                core.notifications,
                approvals,
            ),
        )
    )
    base_url = (configuration.public_base_url or DEV_PUBLIC_BASE_URL).rstrip("/")
    app.include_router(
        build_connection_router(
            auth,
            ConnectionService(connections, runtime.registry, SqlHostedChatAdmin(engine)),
            base_url,
            lifecycle,
        )
    )
    # Linking a manager's own account to a tool (approvals; ADR 0026 moved it to quotations).
    app.include_router(build_enrollment_router(auth, channels))
    # Customers and conversations in the portal (phase 4).
    app.include_router(
        build_conversation_router(
            auth,
            ContactService(
                SqlContactReader(engine),
                SqlContactMerger(engine),
                SqlCustomerMessageSettings(engine),
            ),
            ResendService(core.outbox),
        )
    )
    # Short-lived public PDF links for tools that fetch attachments by URL (phase 5, C6).
    app.include_router(
        build_media_router(MediaService(SqlMediaLinks(engine), core.deliveries, storage))
    )
    # Hosted customer chat (phase 4): the public chat API and the secure answer links.
    customer_actions = core.customer_actions
    ip_key = configuration.chat_ip_key
    app.include_router(
        build_public_chat_router(
            HostedChatService(
                SqlChatStore(engine),
                SqlIntakeStore(engine),
                core.inbox,
                storage,
                limiter,
                runtime.registry,
                customer_actions,
                default_router(),
                ip_key.get_secret_value().encode() if ip_key else secrets.token_bytes(32),
                base_url,
            ),
            customer_actions,
            base_url,
            None
            if configuration.portal_dist_dir is None
            else configuration.portal_dist_dir / "widget" / "widget.js",
        )
    )
    # Provider callbacks (ADR 0018): one generic route for every provider with webhooks.
    # Phase 5: platform routes (one bot or app for everyone) and customer messages from
    # webhooks, which take the same road as website chat.
    inbox = core.inbox
    app.include_router(
        build_hook_router(
            InboundService(
                SqlHookLookup(engine),
                runtime.registry,
                runtime.secrets,
                inbox,
                customers=CustomerInbox(
                    SqlIntakeStore(engine),
                    inbox,
                    storage,
                    limiter,
                    runtime.registry,
                    runtime.secrets,
                    business_store,
                    default_router(),
                ),
                routes=SqlRouteLookup(engine),
                connections=connections,
            )
        )
    )
    if configuration.portal_dist_dir is not None and configuration.portal_dist_dir.is_dir():
        app.mount(
            "/portal", StaticFiles(directory=configuration.portal_dist_dir, html=True), "portal"
        )
    return app
