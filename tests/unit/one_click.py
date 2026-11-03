"""Test doubles for one-click tools (phase 5): a generic provider and in-memory stores.

OneClickTool is not any real tool: it exercises the provider contract the way a package does
(provision with a link code, a setup event that finishes it, a platform webhook with a
company-level secret and routing keys), so the core is tested without naming a provider.
"""

import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sales_platform.domain.connections import Capabilities, Connection, ConnectionStatus, Purpose
from sales_platform.domain.intake import ConnectionUpdate, CustomerMessage, InboundEvent
from sales_platform.ports.connections import ConnectionChange, RouteTaken
from sales_platform.ports.providers import (
    FREE,
    CostNote,
    InboundRejected,
    InboundRequest,
    InvalidInbound,
    PendingAction,
    PendingActionKind,
    ProviderDefaults,
    ProvisionResult,
    ProvisionStatus,
    ReceiptKind,
    RenderedMessage,
    SendReceipt,
    TestResult,
)
from sales_platform.ports.secrets import SecretNotFound, SecretStore
from tests.unit.conftest import MemoryConnections

TOOL = "one_click_tool"
PLATFORM_SECRET = "platform-secret"
LINK_LIFETIME = timedelta(minutes=10)


class OneClickTool(ProviderDefaults):
    key = TOOL
    display_name = "One-click tool"
    purposes = frozenset({Purpose.CUSTOMER_MESSAGES, Purpose.MANAGER_APPROVALS})
    capabilities = Capabilities(
        self_service=True, platform_webhooks=True, buttons=True, threads=True
    )

    def __init__(self, now: datetime, cost: CostNote = FREE) -> None:
        self.now = now
        self.cost = cost
        self.codes = 0
        self.unrouted_calls = 0
        self.sent: list[RenderedMessage] = []

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult:
        return TestResult(ok=conn.active, detail="linked" if conn.active else "not linked")

    def provision(self, conn: Connection, secrets: SecretStore) -> ProvisionResult:
        self.codes += 1
        code = f"CODE{self.codes}"
        old = conn.config.get("link_route")
        return ProvisionResult(
            ProvisionStatus.NEEDS_ACTION,
            pending=PendingAction(
                "link",
                PendingActionKind.SHOW_STEPS,
                "Link your account",
                steps=("Open the tool", "Send /link {code} to our bot"),
                once={"code": code},
                expires_at=self.now + LINK_LIFETIME,
            ),
            config={"link_route": f"link:{code}"},
            add_routes={f"link:{code}": self.now + LINK_LIFETIME},
            remove_routes=(str(old),) if old else (),
        )

    def resume(
        self,
        conn: Connection,
        secrets: SecretStore,
        action_id: str,
        submission: Mapping[str, object],
    ) -> ProvisionResult:
        if action_id == "link":
            owner = str(submission["owner"])
            return ProvisionResult(
                ProvisionStatus.READY,
                detail=f"Customers can write to @{owner}",
                config={"owner": owner},
                secrets={"token": f"token-of-{owner}"},
                add_routes={f"owner:{owner}": None},
                remove_routes=(str(conn.config.get("link_route")),),
            )
        if action_id == "off":
            return ProvisionResult(ProvisionStatus.FAILED, error="The tool was switched off")
        if action_id == "poll":
            return ProvisionResult(ProvisionStatus.WORKING, poll_after=timedelta(minutes=5))
        if action_id == "form":
            if not submission.get("name"):
                raise ValueError("A name is required")
            return ProvisionResult(ProvisionStatus.READY, config={"name": submission["name"]})
        raise ValueError("Unknown step")

    def deprovision(self, conn: Connection, secrets: SecretStore) -> tuple[str, ...]:
        return ("Remove our bot in the tool's settings",)

    def verify_platform_inbound(self, secrets: SecretStore, request: InboundRequest) -> None:
        try:
            expected = secrets.get_platform(self.key, "webhook_secret")
        except SecretNotFound:
            raise InboundRejected("Not configured") from None
        if not hmac.compare_digest(request.headers.get("x-secret", ""), expected):
            raise InboundRejected("Invalid signature")

    def routing_keys(self, request: InboundRequest) -> Sequence[str]:
        return [str(key) for key in _body(request).get("routes", [])]

    def unrouted(self, secrets: SecretStore, request: InboundRequest) -> None:
        self.unrouted_calls += 1

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        events: list[InboundEvent] = []
        for item in _body(request).get("events", []):
            if item["type"] == "update":
                events.append(ConnectionUpdate(item["id"], item["action"], item.get("data", {})))
            elif item["type"] == "message":
                events.append(
                    CustomerMessage(
                        item["id"],
                        item["from"],
                        item["text"],
                        from_business=item.get("own", False),
                    )
                )
            else:
                raise InvalidInbound("Unknown event")
        return events

    def send(self, conn: Connection, secrets: SecretStore, message: RenderedMessage) -> SendReceipt:
        self.sent.append(message)
        return SendReceipt(
            ReceiptKind.SENT,
            f"m-{len(self.sent)}",
            routing_keys=(f"msg:{message.recipient}:{len(self.sent)}",),
        )


def _body(request: InboundRequest) -> dict[str, Any]:
    try:
        data = json.loads(request.body)
    except ValueError:
        raise InvalidInbound("Not JSON") from None
    if not isinstance(data, dict):
        raise InvalidInbound("Not an object")
    return data


@dataclass
class Route:
    tenant_id: UUID
    connection_id: UUID
    expires_at: datetime | None


@dataclass
class MemoryLifecycle:
    """ConnectionLifecycleStore + RouteLookup + RouteWriter over MemoryConnections."""

    connections: MemoryConnections
    now: datetime
    routes: dict[tuple[str, str], Route] = field(default_factory=dict)
    events: list[tuple[UUID, str, str]] = field(default_factory=list)
    polls: list[tuple[UUID, datetime]] = field(default_factory=list)
    changes: list[ConnectionChange] = field(default_factory=list)

    def create(
        self,
        tenant_id: UUID,
        provider_key: str,
        display_name: str,
        purposes: Sequence[str],
        actor: str,
    ) -> UUID:
        connection = Connection(
            uuid4(),
            tenant_id,
            provider_key,
            display_name,
            frozenset(Purpose(item) for item in purposes),
            {},
            {},
            None,
            ConnectionStatus.SETTING_UP,
            None,
            actor,
            self.now,
            self.now,
        )  # fmt: skip
        self.connections.add(connection)
        self.events.append((connection.connection_id, "created", actor))
        return connection.connection_id

    def apply(
        self, tenant_id: UUID, connection_id: UUID, change: ConnectionChange, actor: str
    ) -> None:
        row = self.connections.get(tenant_id, connection_id)
        assert row is not None
        routes = dict(self.routes)
        mine = {key for key, route in routes.items() if route.connection_id == connection_id}
        for owned in mine:
            if change.remove_all_routes or owned[1] in change.remove_routes:
                del routes[owned]
        for key, expires_at in change.add_routes.items():
            taken = routes.get((row.provider_key, key))
            if taken is not None and taken.tenant_id != tenant_id:
                raise RouteTaken("This account is already connected to another business")
            routes[(row.provider_key, key)] = Route(tenant_id, connection_id, expires_at)
        self.routes = routes
        self.connections.add(
            replace(
                row,
                status=change.status,
                config=dict(change.config),
                secret_refs=dict(change.secret_refs),
                last_error=change.last_error,
                pending_action=change.pending_action,
            )
        )
        if change.poll_at is not None:
            self.polls.append((connection_id, change.poll_at))
        self.changes.append(change)
        self.events.append((connection_id, change.event_kind, actor))

    def record_event(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        kind: str,
        actor: str,
        detail: Mapping[str, object],
    ) -> None:
        self.events.append((connection_id, kind, actor))

    def resolve(self, provider_key: str, routing_key: str) -> tuple[UUID, UUID] | None:
        route = self.routes.get((provider_key, routing_key))
        if route is None or (route.expires_at is not None and route.expires_at <= self.now):
            return None
        return route.tenant_id, route.connection_id

    def add_routes(
        self,
        tenant_id: UUID,
        connection_id: UUID,
        provider_key: str,
        keys: Mapping[str, datetime | None],
    ) -> None:
        for key, expires_at in keys.items():
            if (provider_key, key) not in self.routes:
                self.routes[(provider_key, key)] = Route(tenant_id, connection_id, expires_at)

    def keys_of(self, connection_id: UUID) -> set[str]:
        return {
            key for (_, key), route in self.routes.items() if route.connection_id == connection_id
        }
