"""Provider `telegram`: our one bot, connected to each business's own Telegram account.

- Customers write to the business's own account; our bot runs behind it as a connected
  business bot (Telegram Business). Replies go out through that business connection, which
  Telegram allows only within 24 hours of the customer's last message: outside it the send
  is refused and shown as "the customer must message first" (user answer 2026-10-29).
- Managers approve through our bot's own chat (P5-2): they link with a deep link that sends
  "/start <route>_<code>", get reviews with Approve / Reject / Change buttons and the signed
  link, and may reply in words (read only when a model is configured).
- Enable: a link code (10 minutes) the business sends to our bot from its own account
  ("/link CODE"), then it adds our bot in Telegram Business -> Chatbots; the
  business_connection update finishes it. The steps say Telegram Premium may be needed (P5-3)
  and recommend new chats only.
- Every update comes to one platform URL with our secret token header; routing keys name the
  business (mapping.py). Not live-verified: there were no accounts in phase 5.
"""

import hmac
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any

import httpx
from pydantic import ValidationError

from sales_platform.adapters.providers.telegram.client import (
    TelegramBusy,
    TelegramClient,
    TelegramRefused,
    TelegramUncertain,
)
from sales_platform.adapters.providers.telegram.config import TelegramPlatformConfig
from sales_platform.adapters.providers.telegram.mapping import (
    BUTTON_TITLES,
    button_data,
    events,
    link_key,
    message_id,
    routing_keys,
)
from sales_platform.domain.chat import MAX_ATTACHMENT_BYTES
from sales_platform.domain.connections import Capabilities, Connection, Purpose
from sales_platform.domain.intake import InboundEvent
from sales_platform.ports.providers import (
    EnrollmentHint,
    InboundRejected,
    InboundRequest,
    InvalidInbound,
    PendingAction,
    PendingActionKind,
    ProviderDefaults,
    ProviderEnvironment,
    ProvisionResult,
    ProvisionStatus,
    ReceiptKind,
    RenderedMessage,
    SendReceipt,
    SendRejected,
    SendUncertain,
    TestResult,
)
from sales_platform.ports.secrets import SecretNotFound, SecretStore, SecretStoreUnavailable

PROVIDER_KEY = "telegram"
SECRET_HEADER = "x-telegram-bot-api-secret-token"
LINK_LIFETIME = timedelta(minutes=10)  # user answer 2026-10-29
MAX_TEXT = 4096
MAX_CAPTION = 1024
ALLOWED_UPDATES = (
    "message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
)
REPLY_HINT = (
    "To answer an approval request, reply to its message (tap and hold it, then Reply) "
    "or use its buttons."
)
EXPIRED_HINT = "This link has expired. Please ask for a new one in the portal."
OUTSIDE_WINDOW = (
    "The customer must message first: Telegram allows replies only within 24 hours of the "
    "customer's last message"
)
logger = logging.getLogger(__name__)


class TelegramProvider(ProviderDefaults):
    key = PROVIDER_KEY
    display_name = "Telegram"
    purposes = frozenset({Purpose.CUSTOMER_MESSAGES, Purpose.MANAGER_APPROVALS})
    capabilities = Capabilities(
        webhooks=False,
        platform_webhooks=True,
        self_service=True,
        attachments=True,
        max_attachment_bytes=MAX_ATTACHMENT_BYTES,
        buttons=True,
        threads=True,
        outbound_initiation=True,  # managers yes; customers within 24 hours (see send)
        reply_window=timedelta(hours=24),
        delivery_receipts=False,
        manager_enrollment="deep_link",
        max_inbound_bytes=256 * 1024,
    )
    platform_config_model: type | None = TelegramPlatformConfig

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http

    # --- company settings -------------------------------------------------------------------
    def _settings(self, secrets: SecretStore) -> TelegramPlatformConfig:
        try:
            raw = secrets.get_platform(self.key, "config")
            return TelegramPlatformConfig.model_validate_json(raw)
        except (SecretNotFound, SecretStoreUnavailable, ValidationError):
            raise InboundRejected("Telegram is not set up for this platform") from None

    def _client(self, secrets: SecretStore) -> tuple[TelegramClient, TelegramPlatformConfig]:
        settings = self._settings(secrets)
        return TelegramClient(settings.bot_token.get_secret_value(), self._http), settings

    def platform_setup(self, secrets: SecretStore, public_base_url: str) -> str:
        client, settings = self._client(secrets)
        url = f"{public_base_url.rstrip('/')}/api/v1/hooks/p/{self.key}"
        client.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": settings.webhook_secret_token.get_secret_value(),
                "allowed_updates": list(ALLOWED_UPDATES),
            },
        )
        return f"Telegram sends @{settings.bot_username}'s updates to {url}"

    # --- lifecycle ----------------------------------------------------------------------------
    def provision(self, conn: Connection, secrets: SecretStore) -> ProvisionResult:
        _, settings = self._client(secrets)
        route = str(conn.config.get("route") or secrets_token())
        code = token_hex(5).upper()
        old = conn.config.get("link_route")
        expires_at = datetime.now(UTC) + LINK_LIFETIME
        bot = f"@{settings.bot_username}"
        return ProvisionResult(
            ProvisionStatus.NEEDS_ACTION,
            pending=PendingAction(
                "link",
                PendingActionKind.SHOW_STEPS,
                "Connect your Telegram account",
                steps=(
                    "Open Telegram on the account your customers should write to. Connecting "
                    "a business bot may need Telegram Premium.",
                    f"Send this message to {bot} within 10 minutes: /link {{code}}",
                    f"Then open Settings > Telegram Business > Chatbots and add {bot}. We "
                    "recommend new chats only: exclude your existing contacts.",
                    'Allow "Reply to messages". This page updates when Telegram confirms.',
                ),
                once={"code": code},
                expires_at=expires_at,
            ),
            config={"route": route, "link_route": link_key(code)},
            add_routes={link_key(code): expires_at, f"r:{route}": None},
            remove_routes=(str(old),) if old else (),
        )

    def resume(
        self,
        conn: Connection,
        secrets: SecretStore,
        action_id: str,
        submission: Mapping[str, object],
    ) -> ProvisionResult:
        client, settings = self._client(secrets)
        bot = f"@{settings.bot_username}"
        if action_id == "link":
            owner = str(submission["owner_user_id"])
            chat = str(submission["chat_id"])
            self._try(
                client,
                "sendMessage",
                {"chat_id": chat, "text": f"Linked. Now add {bot} in Settings > Telegram "
                 'Business > Chatbots and allow "Reply to messages".'},
            )  # fmt: skip
            return ProvisionResult(
                ProvisionStatus.NEEDS_ACTION,
                pending=PendingAction(
                    "connect",
                    PendingActionKind.SHOW_STEPS,
                    "Add our bot in Telegram Business",
                    steps=(
                        f"Open Settings > Telegram Business > Chatbots and add {bot}. We "
                        "recommend new chats only: exclude your existing contacts.",
                        'Allow "Reply to messages". This page updates when Telegram confirms.',
                    ),
                ),
                config={"owner_user_id": owner},
                add_routes={f"owner:{owner}": None},
                remove_routes=(str(conn.config.get("link_route", "")),),
            )
        if action_id == "business_connection":
            enabled = submission.get("is_enabled") == "true"
            can_reply = submission.get("can_reply") == "true"
            if str(submission.get("user_id")) != str(conn.config.get("owner_user_id")):
                raise ValueError("A business connection of another Telegram account")
            if not enabled or not can_reply:
                return ProvisionResult(
                    ProvisionStatus.FAILED,
                    error=f"Telegram connection switched off, or {bot} may not reply: add it "
                    'again in Telegram Business > Chatbots and allow "Reply to messages"',
                )
            username = str(submission.get("username") or "")
            where = f"@{username}" if username else "your Telegram account"
            return ProvisionResult(
                ProvisionStatus.READY,
                detail=f"Customers can write to {where}",
                config={
                    "business_connection_id": str(submission["business_connection_id"]),
                    "business_username": username,
                },
                add_routes={f"bc:{submission['business_connection_id']}": None},
            )
        raise ValueError("Unknown Telegram step")

    def deprovision(self, conn: Connection, secrets: SecretStore) -> tuple[str, ...]:
        try:
            _, settings = self._client(secrets)
            bot = f"@{settings.bot_username}"
        except InboundRejected:
            bot = "our bot"
        return (f"In Telegram, open Settings > Telegram Business > Chatbots and remove {bot}.",)

    def enrollment_hint(self, conn: Connection, secrets: SecretStore, code: str) -> EnrollmentHint:
        _, settings = self._client(secrets)
        route = str(conn.config.get("route", ""))
        return EnrollmentHint(
            "Open the link on the phone with your own Telegram account and press Start "
            "within 10 minutes.",
            url=f"https://t.me/{settings.bot_username}?start={route}_{code}",
        )

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult:
        try:
            client, settings = self._client(secrets)
            client.call("getMe")
            connection_id = conn.config.get("business_connection_id")
            if not connection_id:
                return TestResult(ok=False, detail="Telegram account not connected yet")
            found = client.call("getBusinessConnection", {"business_connection_id": connection_id})
        except (InboundRejected, TelegramRefused, TelegramBusy, TelegramUncertain) as error:
            return TestResult(ok=False, detail=str(error)[:200])
        rights = found.get("rights") or {}
        can_reply = bool(rights.get("can_reply", found.get("can_reply", False)))
        if not found.get("is_enabled") or not can_reply:
            return TestResult(ok=False, detail=f"@{settings.bot_username} may not reply")
        return TestResult(ok=True, detail="Connected")

    # --- inbound ------------------------------------------------------------------------------
    def verify_platform_inbound(self, secrets: SecretStore, request: InboundRequest) -> None:
        settings = self._settings(secrets)
        expected = settings.webhook_secret_token.get_secret_value()
        if not hmac.compare_digest(request.headers.get(SECRET_HEADER, ""), expected):
            raise InboundRejected("Invalid secret token")
        update = _update(request)
        query = update.get("callback_query")
        if isinstance(query, Mapping) and "id" in query:
            # Stop the button's spinner; the press itself is handled by the worker.
            client = TelegramClient(settings.bot_token.get_secret_value(), self._http)
            self._try(client, "answerCallbackQuery", {"callback_query_id": query["id"]})

    def routing_keys(self, request: InboundRequest) -> Sequence[str]:
        return routing_keys(_update(request))

    def unrouted(self, secrets: SecretStore, request: InboundRequest) -> None:
        """A private message to our bot that names no business: a generic hint."""
        update = _update(request)
        message = update.get("message")
        if not isinstance(message, Mapping) or message.get("chat", {}).get("type") != "private":
            return
        text = str(message.get("text", ""))
        hint = EXPIRED_HINT if text.startswith(("/start ", "/link ")) else REPLY_HINT
        client, _ = self._client(secrets)
        self._try(client, "sendMessage", {"chat_id": message["chat"]["id"], "text": hint})

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        owner = conn.config.get("owner_user_id")
        try:
            return events(_update(request), None if owner is None else str(owner))
        except (KeyError, TypeError, ValueError):
            raise InvalidInbound("Unexpected Telegram update") from None

    def fetch_file(
        self, conn: Connection, secrets: SecretStore, file_ref: str, max_bytes: int
    ) -> bytes:
        client, _ = self._client(secrets)
        try:
            found = client.call("getFile", {"file_id": file_ref})
        except (TelegramRefused, TelegramBusy, TelegramUncertain):
            raise ValueError("The file could not be found") from None
        size = found.get("file_size")
        if not found.get("file_path") or (isinstance(size, int) and size > max_bytes):
            raise ValueError("The file is too large or unavailable")
        return client.download(str(found["file_path"]), max_bytes)

    # --- outbound -----------------------------------------------------------------------------
    def send(self, conn: Connection, secrets: SecretStore, message: RenderedMessage) -> SendReceipt:
        client, _ = self._client(secrets)
        route = str(conn.config.get("route", ""))
        markup = _keyboard(route, message)
        text = message.text
        if message.purpose is Purpose.MANAGER_APPROVALS:
            if message.link:
                text += f"\n\nDecide in the portal: {message.link}"
            params: dict[str, Any] = {"chat_id": message.recipient}
        else:
            business = conn.config.get("business_connection_id")
            if not business:
                raise SendRejected("The Telegram account is not connected")
            params = {"chat_id": message.recipient, "business_connection_id": business}
        try:
            sent = self._deliver(client, params, text, markup, message)
        except TelegramRefused as error:
            if message.purpose is Purpose.CUSTOMER_MESSAGES and error.code in (400, 403):
                raise SendRejected(f"{OUTSIDE_WINDOW} ({error.description})") from None
            raise SendRejected(error.description) from None
        except TelegramUncertain as error:
            raise SendUncertain(str(error)) from None
        # TelegramBusy and connection errors: nothing was sent; the job retries.
        chat = sent["chat"]["id"]
        provider_id = message_id(chat, sent["message_id"])
        keys = (f"m:{provider_id}",) if message.purpose is Purpose.MANAGER_APPROVALS else ()
        return SendReceipt(ReceiptKind.SENT, provider_id, routing_keys=keys)

    def _deliver(
        self,
        client: TelegramClient,
        params: dict[str, Any],
        text: str,
        markup: dict[str, Any] | None,
        message: RenderedMessage,
    ) -> Any:
        extra = {} if markup is None else {"reply_markup": markup}
        attachment = message.attachment
        if attachment is None:
            return client.call("sendMessage", {**params, "text": _cut(text, MAX_TEXT), **extra})
        if len(text) > MAX_CAPTION:
            client.call("sendMessage", {**params, "text": _cut(text, MAX_TEXT)})
            text = ""
        files = {"document": (attachment.filename, attachment.data, attachment.content_type)}
        return client.call("sendDocument", {**params, "caption": text, **extra}, files)

    @staticmethod
    def _try(client: TelegramClient, method: str, params: Mapping[str, Any]) -> None:
        """Best-effort extras (hints, the button spinner): a failure changes nothing."""
        try:
            client.call(method, params)
        except Exception as error:  # noqa: BLE001
            logger.info("Telegram %s failed: %s", method, type(error).__name__)


def secrets_token() -> str:
    """A connection's short route (8 hex characters) for deep links and buttons."""
    return token_hex(4)


def _update(request: InboundRequest) -> dict[str, Any]:
    try:
        data = json.loads(request.body)
    except ValueError:
        raise InvalidInbound("Not JSON") from None
    if not isinstance(data, dict) or "update_id" not in data:
        raise InvalidInbound("Not a Telegram update")
    return data


def _keyboard(route: str, message: RenderedMessage) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    if message.actions and message.action_reference:
        reference = message.action_reference
        if message.purpose is Purpose.CUSTOMER_MESSAGES:
            reference = reference.replace("-", "")  # a customer action reference ID
        rows.append(
            [
                {
                    "text": BUTTON_TITLES[action],
                    "callback_data": button_data(route, action, reference),
                }
                for action in message.actions
                if action in BUTTON_TITLES
            ]
        )
    if message.link and message.link.startswith("https://"):
        rows.append([{"text": "Open in the portal", "url": message.link}])
    return {"inline_keyboard": rows} if rows else None


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 30].rstrip() + "\n... (see the portal)"


def build(environment: ProviderEnvironment) -> TelegramProvider:
    """Entry point `telegram` in group sales_platform.providers."""
    return TelegramProvider()
