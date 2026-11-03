"""Provider callbacks on the generic hook route: verify, store once, route later (ADR 0018).

POST /api/v1/hooks/{connection_id}/{webhook_token} reaches InboundService.receive:
1. Find the connection by ID and compare the token's SHA-256 in constant time. Unknown,
   wrong token, not active, provider not installed or without webhooks: all the same
   HookNotFound, so a caller learns nothing about which check failed.
2. Refuse bodies larger than the provider allows.
3. The provider verifies the raw bytes (signature, timestamp) BEFORE anything is parsed.
4. The provider parses the body into normalised events. The tenant is always the
   connection's, never something named in the body.
5. Each event is stored once (dedupe on connection, provider event ID and type) together with
   a process_inbound job, in one transaction. The webhook answers at once; the job routes it.

Routing is by event type, never by provider: a manager action goes to the same
ChannelActionService (and ApprovalService) the portal's decisions use.

Phase 5:
- POST|GET /api/v1/hooks/p/{provider_key} reaches receive_platform: tools with one callback
  URL for the whole platform (one bot, one app). The provider checks the request with its
  company-level secrets first, then names the routing keys present in it; each key that names
  a connection (connection_routes, exact match) is parsed in that connection's business. A
  verified request that no connection claims is answered by the provider generically
  (Provider.unrouted) and nothing is stored. Genuine requests are always acknowledged, even
  when their content is refused, so the tool does not retry or switch the webhook off.
- GET on either route is a verification handshake (Provider.handshake).
- Customer messages go to the CustomerInbox at once (the same road as website chat).
- A connection still being set up (or in error) accepts only setup events (ConnectionUpdate).
"""

import hashlib
import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sales_platform.integrations.application.lifecycle import ConnectionLifecycle
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection, ConnectionStatus
from sales_platform.integrations.domain.events import (
    ConnectionUpdate,
    CustomerMessage,
    InboundEvent,
    InboundEventType,
    event_type,
    validate_event,
)
from sales_platform.integrations.ports.connections import (
    ConnectionStore,
    HookLookup,
    RouteLookup,
    RouteTaken,
)
from sales_platform.integrations.ports.inbound import InboundStore
from sales_platform.integrations.ports.providers import (
    HandshakeResponse,
    InboundRequest,
    InvalidInbound,
    OperationUnsupported,
    Provider,
)
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.platform.ports.secrets import SecretStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)
_DUMMY_HASH = "0" * 64


class HookNotFound(Exception):
    """Unknown connection, wrong token, not active or no webhook support (one message)."""


class EventTooLarge(Exception):
    pass


@dataclass(frozen=True)
class InboundReceipt:
    received: int
    duplicates: int


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_SETUP_ONLY = frozenset({ConnectionStatus.SETTING_UP, ConnectionStatus.ERROR})


class CustomerReceiver(Protocol):
    """Takes a customer message at once (application/customer_inbox.py CustomerInbox)."""

    def receive(self, connection: Connection, message: CustomerMessage) -> bool: ...


class InboundService:
    def __init__(
        self,
        hooks: HookLookup,
        registry: ProviderRegistry,
        secrets: SecretStore,
        store: InboundStore,
        customers: CustomerReceiver | None = None,
        routes: RouteLookup | None = None,
        connections: ConnectionStore | None = None,
    ) -> None:
        self._hooks = hooks
        self._registry = registry
        self._secrets = secrets
        self._store = store
        self._customers = customers
        self._routes = routes
        self._connections = connections

    def receive(
        self, connection_id: UUID, webhook_token: str, request: InboundRequest
    ) -> InboundReceipt:
        connection, provider = self._hook(connection_id, webhook_token)
        if len(request.body) > provider.capabilities.max_inbound_bytes:
            raise EventTooLarge("Event too large")
        provider.verify_inbound(connection, self._secrets, request)  # InboundRejected
        events = self._parse(connection, request)
        return self._accept(connection, events, request)

    def handshake(
        self, connection_id: UUID, webhook_token: str, request: InboundRequest
    ) -> HandshakeResponse:
        """A verification GET on a per-connection URL; HookNotFound when it is not one."""
        connection, provider = self._hook(connection_id, webhook_token)
        answer = provider.handshake(connection, self._secrets, request)
        if answer is None:
            raise HookNotFound("Not found")
        return answer

    def receive_platform(self, provider_key: str, request: InboundRequest) -> InboundReceipt:
        provider = self._platform(provider_key)
        if len(request.body) > provider.capabilities.max_inbound_bytes:
            raise EventTooLarge("Event too large")
        provider.verify_platform_inbound(self._secrets, request)  # InboundRejected
        targets = self._route(provider, request)
        if not targets:
            provider.unrouted(self._secrets, request)
            return InboundReceipt(0, 0)
        received = duplicates = 0
        for connection in targets:
            try:
                events = self._parse(connection, request)
            except InvalidInbound:
                # Genuine but not understood: acknowledged so the tool does not retry it.
                logger.warning("Unreadable %s event for %s", provider_key, connection.tenant_id)
                continue
            receipt = self._accept(connection, events, request)
            received += receipt.received
            duplicates += receipt.duplicates
        return InboundReceipt(received, duplicates)

    def platform_handshake(self, provider_key: str, request: InboundRequest) -> HandshakeResponse:
        provider = self._platform(provider_key)
        answer = provider.handshake(None, self._secrets, request)
        if answer is None:
            raise HookNotFound("Not found")
        return answer

    def _hook(self, connection_id: UUID, webhook_token: str) -> tuple[Connection, Provider]:
        found = self._hooks.find_for_hook(connection_id)
        expected = found[1] if found is not None and found[1] else _DUMMY_HASH
        token_matches = hmac.compare_digest(token_sha256(webhook_token), expected)
        if found is None or not found[1] or not token_matches:
            raise HookNotFound("Not found")
        connection = found[0]
        provider = self._registry.find(connection.provider_key)
        if (
            not (connection.active or connection.status in _SETUP_ONLY)
            or provider is None
            or not provider.capabilities.webhooks
        ):
            raise HookNotFound("Not found")
        return connection, provider

    def _platform(self, provider_key: str) -> Provider:
        provider = self._registry.find(provider_key)
        if (
            provider is None
            or not provider.capabilities.platform_webhooks
            or self._routes is None
            or self._connections is None
        ):
            raise HookNotFound("Not found")
        return provider

    def _route(self, provider: Provider, request: InboundRequest) -> list[Connection]:
        """The connections the request's own keys name (at most 10 keys are looked at)."""
        assert self._routes is not None and self._connections is not None
        try:
            keys = list(dict.fromkeys(provider.routing_keys(request)))
        except (InvalidInbound, OperationUnsupported, ValueError):
            return []
        found: dict[UUID, Connection] = {}
        for key in keys[:10]:
            resolved = self._routes.resolve(provider.key, key)
            if resolved is None or resolved[1] in found:
                continue
            connection = self._connections.get(*resolved)
            if connection is not None and connection.provider_key == provider.key:
                found[resolved[1]] = connection
        return [
            connection
            for connection in found.values()
            if connection.active or connection.status in _SETUP_ONLY
        ]

    def _accept(
        self, connection: Connection, events: list[InboundEvent], request: InboundRequest
    ) -> InboundReceipt:
        if not connection.active:
            events = [event for event in events if isinstance(event, ConnectionUpdate)]
        stored: list[InboundEvent] = []
        received = duplicates = 0
        for event in events:
            if isinstance(event, CustomerMessage) and self._customers is not None:
                received += 1
                if not self._customers.receive(connection, event):
                    duplicates += 1
            else:
                stored.append(event)
        recorded = self._store.record(connection, stored, request.received_at, queue_jobs=True)
        received += len(recorded)
        duplicates += sum(1 for item in recorded if not item.new)
        return InboundReceipt(received=received, duplicates=duplicates)

    def _parse(self, connection: Connection, request: InboundRequest) -> list[InboundEvent]:
        provider = self._registry.get(connection.provider_key)
        try:
            events = [
                validate_event(event) for event in provider.parse_inbound(connection, request)
            ]
        except ValueError:
            raise InvalidInbound("Invalid event") from None
        return events


class InboundRoute(Protocol):
    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        """Act on one event and return the outcome to record. Raise to retry later."""
        ...


class ProcessInboundHandler:
    """Job handler for process_inbound: route one stored event, record its outcome once."""

    def __init__(
        self, store: InboundStore, routes: Mapping[InboundEventType, InboundRoute]
    ) -> None:
        self._store = store
        self._routes = dict(routes)

    def __call__(self, job: ClaimedJob) -> None:
        try:
            inbox_event_id = UUID(job.payload["inbox_event_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid inbox_event_id") from None
        stored = self._store.load(job.require_tenant(), inbox_event_id)
        if stored is None:
            raise PermanentJobError("Inbox event not found")
        if stored.processed_at is not None:
            return  # a retried job after the outcome was recorded
        route = self._routes.get(event_type(stored.event))
        setup = isinstance(stored.event, ConnectionUpdate)  # also while setting up
        if not stored.connection.active and not setup:
            outcome = "connection not active"
        elif route is None:
            outcome = "unsupported"
        else:
            outcome = route(stored.connection, stored.event, inbox_event_id)
        logger.info("Inbound event %s: %s", inbox_event_id, outcome)
        self._store.mark_processed(job.require_tenant(), inbox_event_id, outcome[:300])


class ConnectionUpdateRoute:
    """A setup event the tool sent (the business linked its account, switched us off...)."""

    def __init__(self, lifecycle: ConnectionLifecycle) -> None:
        self._lifecycle = lifecycle

    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        assert isinstance(event, ConnectionUpdate)
        try:
            return self._lifecycle.on_update(connection, event)
        except RouteTaken:
            return "account linked to another business"


def routes_for(
    manager_actions: InboundRoute,
    consent: InboundRoute | None = None,
    *,
    staff: InboundRoute | None = None,
    customer_actions: InboundRoute | None = None,
    setup: InboundRoute | None = None,
    manager_replies: InboundRoute | None = None,
    orders: InboundRoute | None = None,
) -> Mapping[InboundEventType, InboundRoute]:
    """Which route handles each kind of inbound event. The routes themselves live in the
    module that owns the rule (approvals and replies in quotations, consent and staff messages
    in conversations, "where is my order?" in orders, ADR 0026); the wiring builds them.
    Customer messages themselves are taken at once by the CustomerInbox (and the API and
    website chat)."""
    routes: dict[InboundEventType, InboundRoute] = {
        InboundEventType.MANAGER_ACTION: manager_actions
    }
    optional: dict[InboundEventType, InboundRoute | None] = {
        InboundEventType.CONSENT_CHANGE: consent,
        InboundEventType.CUSTOMER_MESSAGE: staff,
        InboundEventType.CUSTOMER_ACTION: customer_actions,
        InboundEventType.CONNECTION_UPDATE: setup,
        InboundEventType.MANAGER_REPLY: manager_replies,
        InboundEventType.ORDER_STATUS_REQUEST: orders,
    }
    routes.update({kind: route for kind, route in optional.items() if route is not None})
    return routes
