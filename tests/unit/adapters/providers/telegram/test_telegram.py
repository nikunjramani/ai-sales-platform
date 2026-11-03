"""The Telegram provider against documented updates and a fake Bot API (no network).

Fixtures: tests/data/providers/telegram/updates.json, written from the Bot API docs (phase 5
had no live accounts). The fake API answers like Telegram (ok/result or error_code/description).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from sales_platform.adapters.providers.telegram.client import TelegramBusy
from sales_platform.adapters.providers.telegram.mapping import link_key
from sales_platform.adapters.providers.telegram.provider import (
    EXPIRED_HINT,
    REPLY_HINT,
    TelegramProvider,
)
from sales_platform.adapters.secrets.memory import InMemorySecretStore
from sales_platform.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.domain.intake import (
    ConnectionUpdate,
    CustomerAction,
    CustomerMessage,
    InboundFile,
    ManagerAction,
    ManagerReply,
)
from sales_platform.ports.providers import (
    Attachment,
    InboundRejected,
    InboundRequest,
    PendingActionKind,
    ProvisionStatus,
    RenderedMessage,
    SendRejected,
    SendUncertain,
)

UPDATES = json.loads(
    (Path(__file__).parents[4] / "data" / "providers" / "telegram" / "updates.json").read_text(
        encoding="utf-8"
    )
)
SECRET = "webhook-secret-token-123"
NOW = datetime(2026, 10, 29, tzinfo=UTC)


class FakeBotApi:
    """Answers Bot API calls; `answers` maps a method to a result or (status, description)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, Any] = {}
        self.files: dict[str, bytes] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/"):
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200, content=self.files.get(name, b""))
        method = path.rsplit("/", 1)[1]
        content_type = request.headers.get("content-type", "")
        params: dict[str, Any] = (
            json.loads(request.content) if "json" in content_type else {"multipart": True}
        )
        self.calls.append((method, params))
        answer = self.answers.get(method, {"message_id": 99, "chat": {"id": 8001}})
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            status, description = answer
            body = {"ok": False, "error_code": status, "description": description}
            if status == 429:
                body["parameters"] = {"retry_after": 3}
            return httpx.Response(status, json=body)
        return httpx.Response(200, json={"ok": True, "result": answer})


@pytest.fixture
def api() -> FakeBotApi:
    return FakeBotApi()


@pytest.fixture
def provider(api: FakeBotApi) -> TelegramProvider:
    return TelegramProvider(httpx.Client(transport=httpx.MockTransport(api)))


@pytest.fixture
def secrets() -> InMemorySecretStore:
    store = InMemorySecretStore()
    store.put_platform(
        "telegram",
        "config",
        json.dumps(
            {"bot_token": "123:abc", "bot_username": "quote_bot", "webhook_secret_token": SECRET}
        ),
    )
    return store


def connection(**config: object) -> Connection:
    return Connection(
        uuid4(), uuid4(), "telegram", "Telegram",
        frozenset({Purpose.CUSTOMER_MESSAGES, Purpose.MANAGER_APPROVALS}),
        {"route": "1a2b3c4d", **config}, {}, None, ConnectionStatus.ACTIVE, None, "test", NOW, NOW,
    )  # fmt: skip


def inbound(name: str, token: str | None = SECRET) -> InboundRequest:
    headers = {} if token is None else {"x-telegram-bot-api-secret-token": token}
    return InboundRequest(headers, json.dumps(UPDATES[name]).encode(), {}, NOW)


def test_only_requests_with_our_secret_token_are_accepted(
    provider: TelegramProvider, secrets: InMemorySecretStore
) -> None:
    for token in (None, "forged-secret-token-000"):
        with pytest.raises(InboundRejected):
            provider.verify_platform_inbound(secrets, inbound("customer_message", token))
    with pytest.raises(InboundRejected):  # the platform is not set up: nothing is accepted
        provider.verify_platform_inbound(InMemorySecretStore(), inbound("customer_message"))
    provider.verify_platform_inbound(secrets, inbound("customer_message"))


def test_a_button_press_stops_the_spinner_at_once(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    provider.verify_platform_inbound(secrets, inbound("manager_button"))
    assert api.calls == [("answerCallbackQuery", {"callback_query_id": "cb-1"})]


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        ("business_connection_enabled", ["bc:bc-AAAA", "owner:7001"]),
        ("customer_message", ["bc:bc-AAAA"]),
        ("edited_customer_message", ["bc:bc-AAAA"]),
        ("link_code", [link_key("0A1B2C3D4E")]),
        ("manager_start", ["r:1a2b3c4d"]),
        ("manager_reply", ["m:8001:11"]),
        ("manager_without_reply", []),
        ("manager_button", ["r:1a2b3c4d"]),
        ("customer_accept_button", ["r:1a2b3c4d"]),
    ],
)
def test_each_update_names_its_business_by_a_key(
    provider: TelegramProvider, name: str, keys: list[str]
) -> None:
    assert provider.routing_keys(inbound(name)) == keys


def test_customer_messages_files_and_skipped_kinds(provider: TelegramProvider) -> None:
    conn = connection(owner_user_id="7001")
    [text] = provider.parse_inbound(conn, inbound("customer_message"))
    [photo] = provider.parse_inbound(conn, inbound("customer_photo_and_voice"))
    [own] = provider.parse_inbound(conn, inbound("owner_message"))

    assert text == CustomerMessage(
        "tg:1003", "15005550101", "Please quote 10 metre of 2-core cable",
        occurred_at=datetime.fromtimestamp(1793000200, UTC),
    )  # fmt: skip
    assert isinstance(photo, CustomerMessage)
    assert photo.text == "our parts list"
    assert photo.files == (InboundFile("photo-large", "photo.jpg", "image/jpeg", 98000),)
    assert photo.skipped == ("voice message",)
    assert isinstance(own, CustomerMessage) and own.from_business  # the owner typed it
    assert provider.parse_inbound(conn, inbound("our_own_business_message")) == []
    assert provider.parse_inbound(conn, inbound("edited_customer_message")) == []


def test_manager_updates_become_enrollments_replies_and_buttons(provider: TelegramProvider) -> None:
    conn = connection()
    assert provider.parse_inbound(conn, inbound("manager_start")) == [
        ManagerAction("tg:1009", "8001", "enroll", code="ABCDEF0123")
    ]
    [reply] = provider.parse_inbound(conn, inbound("manager_reply"))
    assert isinstance(reply, ManagerReply)
    assert (reply.text, reply.reply_to_provider_message_id) == ("approved", "8001:11")
    assert provider.parse_inbound(conn, inbound("manager_button")) == [
        ManagerAction(
            "tg:1012", "8001", "decision",
            reference="token-abcdefghijklmnopqrstuvwxyz012", action="approve",
        )
    ]  # fmt: skip
    assert provider.parse_inbound(conn, inbound("manager_yes_button")) == [
        ManagerAction("tg:1013", "8001", "confirm", reference="token-yes", action="yes")
    ]
    assert provider.parse_inbound(conn, inbound("customer_accept_button")) == [
        CustomerAction(
            "tg:1014", "15005550101", str(UUID(hex="6f1c2a3b4c5d4e6f8a9b0c1d2e3f4a5b")), "accept"
        )
    ]


def test_linking_takes_a_code_then_the_business_connection(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    started = provider.provision(connection(route=None), secrets)
    code = started.pending.once["code"] if started.pending else ""
    assert started.status is ProvisionStatus.NEEDS_ACTION
    assert started.pending is not None and started.pending.kind is PendingActionKind.SHOW_STEPS
    assert "/link {code}" in started.pending.steps[1] and "Premium" in started.pending.steps[0]
    assert link_key(code) in started.add_routes
    route = str(started.config["route"])
    assert f"r:{route}" in started.add_routes

    conn = connection(route=route, link_route=link_key(code))
    [update] = provider.parse_inbound(conn, inbound("link_code"))
    assert isinstance(update, ConnectionUpdate) and update.action_id == "link"
    linked = provider.resume(conn, secrets, "link", update.data)
    assert linked.add_routes == {"owner:7001": None}
    assert linked.remove_routes == (link_key(code),)
    assert api.calls[-1][0] == "sendMessage"  # "Linked. Now add @quote_bot..."

    owned = connection(route=route, owner_user_id="7001")
    [ready_event] = provider.parse_inbound(owned, inbound("business_connection_enabled"))
    assert isinstance(ready_event, ConnectionUpdate)
    ready = provider.resume(owned, secrets, ready_event.action_id, ready_event.data)
    assert ready.status is ProvisionStatus.READY
    assert ready.detail == "Customers can write to @acme_supplies"
    assert ready.add_routes == {"bc:bc-AAAA": None}
    [off] = provider.parse_inbound(owned, inbound("business_connection_without_reply"))
    assert isinstance(off, ConnectionUpdate)
    assert provider.resume(owned, secrets, off.action_id, off.data).status is ProvisionStatus.FAILED
    with pytest.raises(ValueError):
        provider.resume(connection(owner_user_id="other"), secrets, off.action_id, off.data)


def review(**fields: Any) -> RenderedMessage:
    values: dict[str, Any] = {
        "message_id": uuid4(), "idempotency_key": "k", "recipient": "8001",
        "text": "approval needed", "metadata": {}, "purpose": Purpose.MANAGER_APPROVALS,
        "actions": ("approve", "reject", "request_revision"),
        "action_reference": "t" * 32, "link": "https://portal.example.com/approve/" + "t" * 32,
    }  # fmt: skip
    return RenderedMessage(**{**values, **fields})


def test_reviews_go_to_the_managers_bot_chat_with_buttons_and_the_link(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    api.answers["sendMessage"] = {"message_id": 11, "chat": {"id": 8001}}
    receipt = provider.send(connection(), secrets, review())

    method, params = api.calls[-1]
    assert method == "sendMessage" and "business_connection_id" not in params
    assert "Decide in the portal: https://portal.example.com/approve/" in params["text"]
    buttons, link = params["reply_markup"]["inline_keyboard"]
    assert [button["callback_data"] for button in buttons] == [
        f"1a2b3c4d:{code}:{'t' * 32}" for code in ("a", "r", "v")
    ]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)
    assert link == [
        {"text": "Open in the portal", "url": "https://portal.example.com/approve/" + "t" * 32}
    ]
    assert (receipt.provider_message_id, receipt.routing_keys) == ("8001:11", ("m:8001:11",))


def test_a_local_link_is_text_only(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    provider.send(connection(), secrets, review(link="http://localhost:8000/portal/approve/x"))
    [buttons] = api.calls[-1][1]["reply_markup"]["inline_keyboard"]  # no URL button row
    assert len(buttons) == 3


def test_customer_messages_go_through_the_business_account(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    ref = uuid4()
    message = RenderedMessage(
        uuid4(), "k", "15005550101", "Your quotation is ready", {},
        actions=("accept", "decline"), action_reference=str(ref),
        attachment=Attachment("Q-1-v1.pdf", "application/pdf", b"%PDF-", "x"),
    )  # fmt: skip
    api.answers["sendDocument"] = {"message_id": 50, "chat": {"id": 15005550101}}
    receipt = provider.send(connection(business_connection_id="bc-AAAA"), secrets, message)
    assert api.calls[-1] == ("sendDocument", {"multipart": True})
    assert receipt.provider_message_id == "15005550101:50" and receipt.routing_keys == ()
    with pytest.raises(SendRejected, match="not connected"):
        provider.send(connection(), secrets, message)


@pytest.mark.parametrize(
    ("answer", "raised"),
    [
        ((403, "Forbidden: bot can't initiate conversation"), SendRejected),
        ((500, "Internal Server Error"), SendUncertain),
        (httpx.ReadTimeout("slow"), SendUncertain),
        ((429, "Too Many Requests"), TelegramBusy),  # an ordinary error: the job retries
        (httpx.ConnectError("down"), httpx.ConnectError),  # nothing was sent: retried too
    ],
)
def test_send_outcomes_are_mapped_honestly(
    provider: TelegramProvider,
    secrets: InMemorySecretStore,
    api: FakeBotApi,
    answer: Any,
    raised: type[Exception],
) -> None:
    api.answers["sendMessage"] = answer
    message = RenderedMessage(uuid4(), "k", "15005550101", "hello", {})
    with pytest.raises(raised) as error:
        provider.send(connection(business_connection_id="bc-AAAA"), secrets, message)
    if raised is SendRejected:
        assert "customer must message first" in str(error.value)


def test_files_are_downloaded_within_the_limit(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    api.answers["getFile"] = {"file_id": "f", "file_path": "docs/list.pdf", "file_size": 5}
    api.files["list.pdf"] = b"%PDF-"
    assert provider.fetch_file(connection(), secrets, "f", 100) == b"%PDF-"
    with pytest.raises(ValueError):
        provider.fetch_file(connection(), secrets, "f", 3)


def test_unclaimed_private_messages_get_a_hint(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    provider.unrouted(secrets, inbound("manager_without_reply"))
    provider.unrouted(secrets, inbound("manager_start"))
    provider.unrouted(secrets, inbound("customer_message"))  # never answered from here
    assert [params["text"] for _, params in api.calls] == [REPLY_HINT, EXPIRED_HINT]


def test_a_manager_links_with_a_deep_link_and_the_operator_sets_the_webhook(
    provider: TelegramProvider, secrets: InMemorySecretStore, api: FakeBotApi
) -> None:
    hint = provider.enrollment_hint(connection(), secrets, "ABCDEF0123")
    assert hint.url == "https://t.me/quote_bot?start=1a2b3c4d_ABCDEF0123"
    api.answers["setWebhook"] = True
    done = provider.platform_setup(secrets, "https://api.example.com/")
    method, params = api.calls[-1]
    assert method == "setWebhook" and params["secret_token"] == SECRET
    assert params["url"] == "https://api.example.com/api/v1/hooks/p/telegram"
    assert "business_message" in params["allowed_updates"]
    assert "https://api.example.com/api/v1/hooks/p/telegram" in done


def test_enable_link_and_first_customer_message_through_the_core(
    provider: TelegramProvider, secrets: InMemorySecretStore, now: datetime
) -> None:
    """The generic platform route and lifecycle with this package: no Telegram in the core."""
    from sales_platform.application.inbound import InboundService
    from sales_platform.application.lifecycle import ConnectionLifecycle
    from sales_platform.application.providers import ProviderRegistry
    from sales_platform.domain.tenancy import Role, TenantContext
    from tests.unit.conftest import Job, MemoryConnections, MemoryInbox
    from tests.unit.one_click import MemoryLifecycle

    tenant = uuid4()
    connections = MemoryConnections(now)
    store = MemoryLifecycle(connections, now)
    registry = ProviderRegistry([provider])
    lifecycle = ConnectionLifecycle(connections, store, registry, secrets, clock=lambda: now)
    jobs: list[Job] = []
    inbox = MemoryInbox(connections, jobs)
    received: list[CustomerMessage] = []

    class Customers:
        def receive(self, conn: Connection, message: CustomerMessage) -> bool:
            received.append(message)
            return True

    hooks = InboundService(
        connections, registry, secrets, inbox, Customers(), routes=store, connections=connections
    )
    admin = TenantContext(tenant, "user:admin", frozenset({Role.ADMINISTRATOR}))
    started = lifecycle.enable(admin, "telegram", accept_costs=False)
    code = started.pending.once["code"] if started.pending else ""
    store.now = now  # link codes expire 10 minutes after "now"

    link = {**UPDATES["link_code"]}
    link["message"] = {**link["message"], "text": f"/link {code}"}
    request = InboundRequest(
        {"x-telegram-bot-api-secret-token": SECRET}, json.dumps(link).encode(), {}, now
    )
    hooks.receive_platform("telegram", request)
    [row] = inbox.rows.values()
    assert isinstance(row.event, ConnectionUpdate)
    lifecycle.on_update(started.connection, row.event)

    linked = connections.get(tenant, started.connection.connection_id)
    assert linked is not None and linked.config["owner_user_id"] == "7001"
    hooks.receive_platform("telegram", inbound("business_connection_enabled"))
    ready_row = [r for r in inbox.rows.values() if r.event != row.event][0]
    assert isinstance(ready_row.event, ConnectionUpdate)
    assert lifecycle.on_update(linked, ready_row.event) == "active"

    hooks.receive_platform("telegram", inbound("customer_message"))
    assert [message.text for message in received] == ["Please quote 10 metre of 2-core cable"]
