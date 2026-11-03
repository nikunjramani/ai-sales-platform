"""Composition root: builds the pluggable runtime parts, the shared core and the worker's job
handlers (ADR 0018, ADR 0026 step 1).

The API, the worker, scripts and tests all wire providers, stores and handlers through these
functions, so adding a provider never means editing the API or the worker. `build_core` builds
the services both processes use, once per process, so the API and the worker cannot wire them
differently.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import Engine

from sales_platform.adapters.documents.pdf import ReportLabQuotationRenderer
from sales_platform.adapters.documents.text import PdfAndTextReader
from sales_platform.adapters.storage.local import LocalObjectStorage
from sales_platform.application.acceptance import (
    QuotationExpiryHandler,
    QuotationResponseService,
    ResponseNotificationHandler,
)
from sales_platform.application.accounts import AccountEmailHandler
from sales_platform.application.approvals import ApprovalService
from sales_platform.application.catalogue_sources import CatalogueSourceResolver, TenantCatalogue
from sales_platform.application.catalogue_sync import CatalogueSyncHandler
from sales_platform.application.channel_actions import ChannelActionService
from sales_platform.application.customer_actions import CustomerActionService, QuotationReadyHook
from sales_platform.application.customer_messages import CustomerConsentHook, CustomerMessenger
from sales_platform.application.delivery import QuotationDeliveryHandler, QuotationDocumentHook
from sales_platform.application.inbound import (
    ConnectionUpdateRoute,
    ConsentRoute,
    CustomerActionRoute,
    ProcessInboundHandler,
    StaffMessageRoute,
    routes_for,
)
from sales_platform.application.jobs import JobHandler, PauseWhileSuspended
from sales_platform.application.lifecycle import ConnectionLifecycle
from sales_platform.application.manager_replies import (
    ApprovalPromptHook,
    ManagerReplyService,
)
from sales_platform.application.media import MediaLinkHook
from sales_platform.application.messaging import OutboundService, SendMessageHandler
from sales_platform.application.notifications import (
    ApprovalNotificationHandler,
    ApprovalRequestHook,
)
from sales_platform.application.orders import (
    CreateOrderHandler,
    ErpOrderOperationHandler,
    OrderDestinations,
    OrderNotificationHandler,
    OrderService,
    OrderStatusRoute,
    ReconcileOrderOperationHandler,
    RefreshOrderStatusHandler,
)
from sales_platform.application.plugins import load_named
from sales_platform.application.preparation import EnquiryPreparationHandler
from sales_platform.application.pricing import QuotationPricingService
from sales_platform.application.providers import ProviderRegistry
from sales_platform.application.tenancy import TenantContextResolver
from sales_platform.config import Settings
from sales_platform.domain.chat import ChatLimits
from sales_platform.domain.connections import DeliveryMode, Purpose
from sales_platform.domain.jobs import JobKind
from sales_platform.domain.messages import CUSTOMER_TEMPLATES, TemplateKey
from sales_platform.persistence.acceptance import SqlResponseStore
from sales_platform.persistence.accounts import SqlAccountEmailStore, SqlBusinessStore
from sales_platform.persistence.catalogue import SqlCatalogueStore
from sales_platform.persistence.catalogue_sync import SqlCatalogueSync
from sales_platform.persistence.channels import SqlChannelStore, SqlNotificationStore
from sales_platform.persistence.connections import SqlConnectionStore
from sales_platform.persistence.conversations import SqlCustomerTargets, SqlStaffMessages
from sales_platform.persistence.customer_actions import SqlCustomerActionStore
from sales_platform.persistence.documents import SqlDeliveryStore
from sales_platform.persistence.inbound import SqlInboundStore
from sales_platform.persistence.intake import SqlPreparationStore
from sales_platform.persistence.manager_replies import SqlReplyStore
from sales_platform.persistence.media import SqlMediaLinks
from sales_platform.persistence.messages import SqlMessageOutbox
from sales_platform.persistence.orders import SqlOrderStore
from sales_platform.persistence.provider_state import SqlConnectionLifecycle, SqlRouteWriter
from sales_platform.persistence.quotations import SqlApprovalStore, SqlQuotationWorkflowStore
from sales_platform.persistence.tenancy import SqlMembershipReader, SqlPricingPolicyReader
from sales_platform.persistence.workflow import SqlWorkflowStore
from sales_platform.ports.accounts import DefaultSource, Mailer, MailerEnvironment
from sales_platform.ports.erp import ErpReader
from sales_platform.ports.interpretation import EnquiryInterpreter
from sales_platform.ports.messages import SendHook
from sales_platform.ports.providers import ProviderEnvironment
from sales_platform.ports.reply_classification import ReplyClassifier
from sales_platform.ports.secrets import SecretStore, SecretStoreEnvironment
from sales_platform.ports.workflow import ReadingCap
from sales_platform.workflows.enquiry import (
    EnquiryWorkflowDeps,
    ExtractionJobHandler,
    build_enquiry_graph,
)
from sales_platform.workflows.quotation import (
    QuotationJobHandler,
    QuotationWorkflowDeps,
    build_quotation_graph,
)

SECRET_STORES = "sales_platform.secret_stores"
MAILERS = "sales_platform.mailers"


# Where links in customer messages point when SALES_PUBLIC_BASE_URL is not set (dev).
DEV_PUBLIC_BASE_URL = "http://localhost:8000"


def build_secret_store(settings: Settings) -> SecretStore:
    factory = cast(
        Callable[[SecretStoreEnvironment], SecretStore],
        load_named(SECRET_STORES, settings.secret_store_name),
    )
    key = settings.secret_store_key
    return factory(
        SecretStoreEnvironment(
            environment=settings.environment,
            storage_dir=settings.storage_dir,
            key=key.get_secret_value() if key is not None else None,
            options=settings.secret_store_options,
        )
    )


class LazySecretStore:
    """Builds the configured store on first use.

    The API and worker start even when no store is configured (e.g. no local key); only an
    operation that really needs a secret fails, with the store's own clear error.
    """

    def __init__(self, build: Callable[[], SecretStore]) -> None:
        self._build = build
        self._store: SecretStore | None = None
        self._lock = threading.Lock()

    def _get(self) -> SecretStore:
        with self._lock:
            if self._store is None:
                self._store = self._build()
            return self._store

    def put(self, tenant_id: UUID, name: str, value: str) -> str:
        return self._get().put(tenant_id, name, value)

    def get(self, tenant_id: UUID, reference: str) -> str:
        return self._get().get(tenant_id, reference)

    def delete(self, tenant_id: UUID, reference: str) -> None:
        self._get().delete(tenant_id, reference)

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        self._get().put_platform(provider_key, name, value)

    def get_platform(self, provider_key: str, name: str) -> str:
        return self._get().get_platform(provider_key, name)


def build_mailer(settings: Settings) -> Mailer:
    """The account email sender named by SALES_MAILER (an installed entry point)."""
    factory = cast(Callable[[MailerEnvironment], Mailer], load_named(MAILERS, settings.mailer))
    return factory(
        MailerEnvironment(
            environment=settings.environment,
            storage_dir=settings.storage_dir,
            options=settings.mailer_options,
        )
    )


def lazy_secret_store(settings: Settings) -> SecretStore:
    return LazySecretStore(lambda: build_secret_store(settings))


def build_provider_registry(settings: Settings, engine: Engine | None = None) -> ProviderRegistry:
    """Every installed provider. `engine` (the process's own database role) lets providers
    that read our own tables, such as our product list, use them."""
    return ProviderRegistry.load(
        ProviderEnvironment(
            environment=settings.environment,
            storage_dir=settings.storage_dir,
            catalogue_store=SqlCatalogueStore(engine) if engine is not None else None,
        )
    )


@dataclass(frozen=True)
class Runtime:
    """The pluggable parts shared by the API and the worker."""

    registry: ProviderRegistry
    secrets: SecretStore


def build_runtime(settings: Settings, engine: Engine | None = None) -> Runtime:
    return Runtime(build_provider_registry(settings, engine), lazy_secret_store(settings))


def default_catalogue_source(
    settings: Settings, registry: ProviderRegistry
) -> DefaultSource | None:
    """The product source new businesses start with (SALES_DEFAULT_CATALOGUE_PROVIDER).

    A configured key that is not an installed catalogue provider stops start-up: a silent
    fallback would leave every new business without products.
    """
    key = settings.default_catalogue_provider
    if not key:
        return None
    provider = registry.find(key)
    if provider is None or Purpose.CATALOGUE not in provider.purposes:
        raise ValueError(f"SALES_DEFAULT_CATALOGUE_PROVIDER '{key}' is not a product source")
    return DefaultSource(provider.key, provider.display_name)


def capped_providers(registry: ProviderRegistry) -> frozenset[str]:
    """Tools anyone may write to (hosted chat, pushed customer tools such as Telegram): their
    enquiries share one daily reading cap per business (phase 4 Q9, phase 5 limits answer).
    The API (pull, a business's own system) is not capped."""
    return frozenset(
        provider.key
        for provider in registry.all()
        if provider.capabilities.hosted_chat
        or (
            Purpose.CUSTOMER_MESSAGES in provider.purposes
            and provider.capabilities.delivery_mode is DeliveryMode.PUSH
        )
    )


def build_catalogue(engine: Engine, runtime: Runtime) -> ErpReader:
    """Every business's own product source, through its catalogue connection."""
    return TenantCatalogue(
        CatalogueSourceResolver(SqlConnectionStore(engine), runtime.registry, runtime.secrets)
    )


@dataclass(frozen=True)
class CoreServices:
    """The stores and services both the API and the worker use, built once per process with
    that process's own database role (`engine`). Nothing connects until it is used."""

    connections: SqlConnectionStore
    businesses: SqlBusinessStore
    memberships: SqlMembershipReader
    resolver: TenantContextResolver
    catalogue: ErpReader
    pricing: QuotationPricingService
    response_store: SqlResponseStore
    responses: QuotationResponseService
    approvals: ApprovalService
    channel_actions: ChannelActionService
    lifecycle: ConnectionLifecycle
    order_store: SqlOrderStore
    destinations: OrderDestinations
    orders: OrderService
    notifications: SqlNotificationStore
    deliveries: SqlDeliveryStore
    inbox: SqlInboundStore
    reply_store: SqlReplyStore
    customer_actions: CustomerActionService
    outbox: SqlMessageOutbox


def build_core(
    settings: Settings,
    engine: Engine,
    runtime: Runtime,
    catalogue: ErpReader | None = None,
) -> CoreServices:
    """`catalogue` defaults to each business's own product source; tests may pass a fixed one."""
    connections = SqlConnectionStore(engine)
    reader = catalogue if catalogue is not None else build_catalogue(engine, runtime)
    pricing = QuotationPricingService(reader, SqlPricingPolicyReader(engine))
    response_store = SqlResponseStore(engine)
    responses = QuotationResponseService(response_store, pricing)
    memberships = SqlMembershipReader(engine)
    resolver = TenantContextResolver(memberships)
    approvals = ApprovalService(SqlApprovalStore(engine), resolver)
    order_store = SqlOrderStore(engine)
    destinations = OrderDestinations(connections, runtime.registry, runtime.secrets)
    return CoreServices(
        connections=connections,
        businesses=SqlBusinessStore(engine, default_catalogue_source(settings, runtime.registry)),
        memberships=memberships,
        resolver=resolver,
        catalogue=reader,
        pricing=pricing,
        response_store=response_store,
        responses=responses,
        approvals=approvals,
        channel_actions=ChannelActionService(
            SqlChannelStore(engine),
            approvals,
            connections,
            runtime.registry,
            secrets=runtime.secrets,
        ),
        lifecycle=ConnectionLifecycle(
            connections, SqlConnectionLifecycle(engine), runtime.registry, runtime.secrets
        ),
        order_store=order_store,
        destinations=destinations,
        orders=OrderService(order_store, reader, destinations),
        notifications=SqlNotificationStore(engine),
        deliveries=SqlDeliveryStore(engine),
        inbox=SqlInboundStore(engine),
        reply_store=SqlReplyStore(engine),
        customer_actions=CustomerActionService(SqlCustomerActionStore(engine), responses),
        outbox=SqlMessageOutbox(engine),
    )


def build_job_handlers(
    settings: Settings,
    engine: Engine,
    runtime: Runtime,
    checkpointer: BaseCheckpointSaver[str],
    interpreter: EnquiryInterpreter,
    catalogue: ErpReader | None = None,
    reply_classifier: ReplyClassifier | None = None,
) -> dict[JobKind, JobHandler]:
    """Every job the worker runs, wired to the worker-role engine.

    `catalogue` defaults to each business's own source; tests may pass a fixed reader.
    `reply_classifier` reads managers' written replies (None: they are not read, P5-5).
    """
    core = build_core(settings, engine, runtime, catalogue)
    storage = LocalObjectStorage(settings.storage_dir)
    connections = core.connections
    notifications = core.notifications
    deliveries = core.deliveries
    outbox = core.outbox
    outbound = OutboundService(outbox)
    pricing = core.pricing
    responses = core.response_store
    workflow_store = SqlWorkflowStore(engine)
    quotation_store = SqlQuotationWorkflowStore(engine)
    targets = SqlCustomerTargets(engine)
    customers = CustomerMessenger(targets, connections, runtime.registry, outbound)
    enquiry_graph = build_enquiry_graph(
        EnquiryWorkflowDeps(
            workflow_store, storage, PdfAndTextReader(), interpreter, core.catalogue, customers
        ),
        checkpointer,
    )
    # Tools that download attachments by URL get a short-lived public link (phase 5, C6).
    document_hook = MediaLinkHook(
        QuotationDocumentHook(deliveries, storage),
        SqlMediaLinks(engine),
        deliveries,
        runtime.registry,
        settings.public_base_url or DEV_PUBLIC_BASE_URL,
        timedelta(minutes=settings.media_link_minutes),
    )
    send_hooks: dict[TemplateKey, SendHook] = {
        template: CustomerConsentHook(targets) for template in CUSTOMER_TEMPLATES
    }
    send_hooks[TemplateKey.QUOTATION_READY] = CustomerConsentHook(
        targets,
        QuotationReadyHook(
            document_hook,
            deliveries,
            targets,
            SqlCustomerActionStore(engine),
            runtime.registry,
            settings.public_base_url or DEV_PUBLIC_BASE_URL,
        ),
    )
    send_hooks[TemplateKey.APPROVAL_REQUEST] = ApprovalRequestHook(
        notifications, portal_url=settings.portal_url
    )
    send_hooks[TemplateKey.QUOTATION_DOCUMENT] = document_hook
    quotations = QuotationJobHandler(
        build_quotation_graph(
            QuotationWorkflowDeps(quotation_store, pricing, responses), checkpointer
        ),
        quotation_store,
    )
    statuses = core.businesses
    channel_actions = core.channel_actions
    # Approval by message (D10, phase 5): buttons, written replies and confirmations.
    reply_store = core.reply_store
    replies = ManagerReplyService(
        reply_store,
        channel_actions,
        outbound,
        connections,
        core.inbox,
        reply_classifier,
    )
    prompt_hook = ApprovalPromptHook(reply_store)
    send_hooks[TemplateKey.APPROVAL_CONFIRM] = prompt_hook
    send_hooks[TemplateKey.APPROVAL_REASON] = prompt_hook
    lifecycle = core.lifecycle
    # Orders (phase 7): created after acceptance, written to the ERP, followed and announced.
    orders = core.order_store
    destinations = core.destinations
    handlers: dict[JobKind, JobHandler] = {
        JobKind.PREPARE_ENQUIRY: EnquiryPreparationHandler(
            SqlPreparationStore(engine), storage, customers
        ),
        JobKind.EXTRACT_REQUIREMENTS: ExtractionJobHandler(
            enquiry_graph,
            workflow_store,
            ReadingCap(
                capped_providers(runtime.registry),
                ChatLimits().readings_per_business_day,
            ),
        ),
        JobKind.DRAFT_QUOTATION: quotations,
        JobKind.RESUME_QUOTATION: quotations,
        JobKind.NOTIFY_APPROVERS: ApprovalNotificationHandler(notifications, connections, outbound),
        JobKind.DELIVER_QUOTATION: QuotationDeliveryHandler(
            deliveries,
            storage,
            ReportLabQuotationRenderer(),
            connections,
            outbound,
            customers=customers,
        ),
        JobKind.EXPIRE_QUOTATION: QuotationExpiryHandler(core.responses),
        JobKind.NOTIFY_RESPONSE: ResponseNotificationHandler(
            responses, notifications, connections, outbound, customers
        ),
        JobKind.SEND_MESSAGE: SendMessageHandler(
            outbox,
            connections,
            runtime.registry,
            runtime.secrets,
            send_hooks,
            statuses,
            SqlRouteWriter(engine),
        ),
        JobKind.SEND_ACCOUNT_EMAIL: AccountEmailHandler(
            SqlAccountEmailStore(engine),
            build_mailer(settings),
            settings.portal_url,
            settings.mail_sender,
        ),
        JobKind.PROCESS_INBOUND: ProcessInboundHandler(
            core.inbox,
            routes_for(
                channel_actions,
                ConsentRoute(targets, customers),
                staff=StaffMessageRoute(SqlStaffMessages(engine)),
                customer_actions=CustomerActionRoute(targets, core.customer_actions),
                setup=ConnectionUpdateRoute(lifecycle),
                replies=replies,
                orders=OrderStatusRoute(orders, targets, customers),
            ),
        ),
        JobKind.POLL_CONNECTION: lifecycle.poll,
        JobKind.CLASSIFY_MANAGER_REPLY: replies.classify,
        JobKind.CREATE_ORDER: CreateOrderHandler(core.orders),
        JobKind.ERP_ORDER_OPERATION: ErpOrderOperationHandler(orders, destinations),
        JobKind.RECONCILE_ORDER_OPERATION: ReconcileOrderOperationHandler(orders, destinations),
        JobKind.REFRESH_ORDER_STATUS: RefreshOrderStatusHandler(orders, destinations),
        JobKind.NOTIFY_ORDER: OrderNotificationHandler(orders, customers),
        JobKind.SYNC_CATALOGUE: CatalogueSyncHandler(
            connections, runtime.registry, runtime.secrets, SqlCatalogueSync(engine)
        ),
    }
    # A suspended business's jobs wait instead of running (phase 2, Q13).
    return {kind: PauseWhileSuspended(handler, statuses) for kind, handler in handlers.items()}
