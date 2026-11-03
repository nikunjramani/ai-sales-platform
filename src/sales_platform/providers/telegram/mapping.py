"""Telegram updates to routing keys and normalised events (pure functions, no HTTP).

One bot serves every business, so each update names its business by a routing key:
- bc:<business_connection_id>  a customer chat of a business's own Telegram account;
- owner:<telegram user id>     the business owner while connecting (the business_connection
                               update arrives before its bc route exists);
- link:<sha256 of the code>    "/link CODE" sent to our bot by the business (10 minutes);
- r:<connection route>         a manager's "/start <route>_<code>" deep link, and buttons;
- m:<chat id>:<message id>     a manager's reply to one of our messages in the bot chat.
Button data is "<route>:<action>:<reference>" (at most 64 bytes, Telegram's limit).
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sales_platform.integrations.domain.events import (
    ConnectionUpdate,
    CustomerAction,
    CustomerMessage,
    InboundEvent,
    InboundFile,
    ManagerAction,
    ManagerReply,
)

ROUTE = re.compile(r"^[0-9a-f]{8}$")
_START = re.compile(r"^/start(?:@\w+)?\s+([0-9a-f]{8})_([0-9A-F]{10})\s*$")
_LINK = re.compile(r"^/link(?:@\w+)?\s+([0-9A-Fa-f]{10})\s*$")
ACTION_CODES = {
    "approve": "a",
    "reject": "r",
    "request_revision": "v",
    "confirm": "y",
    "cancel": "n",
    "accept": "ca",
    "decline": "cd",
}
BUTTON_TITLES = {
    "approve": "Approve",
    "reject": "Reject",
    "request_revision": "Change",
    "confirm": "Yes",
    "cancel": "No",
    "accept": "Accept",
    "decline": "Decline",
}
_ACTIONS = {code: action for action, code in ACTION_CODES.items()}
_DECISIONS = {"approve", "reject", "request_revision"}
# Kinds a customer may send that we do not read (noted for staff, user answer 2026-10-29).
_SKIPPED_KINDS = {
    "voice": "voice message",
    "audio": "audio",
    "video": "video",
    "video_note": "video message",
    "sticker": "sticker",
    "animation": "animation",
    "location": "location",
    "contact": "contact",
    "poll": "poll",
}


def link_key(code: str) -> str:
    return "link:" + hashlib.sha256(code.strip().upper().encode("ascii")).hexdigest()


def message_id(chat_id: object, message: object) -> str:
    """Our provider message ID for a Telegram message: "<chat id>:<message id>"."""
    return f"{chat_id}:{message}"


def button_data(route: str, action: str, reference: str) -> str:
    data = f"{route}:{ACTION_CODES[action]}:{reference}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Button data is limited to 64 bytes")
    return data


def routing_keys(update: Mapping[str, Any]) -> list[str]:
    if "business_connection" in update:
        connection = update["business_connection"]
        return [f"bc:{connection['id']}", f"owner:{connection['user']['id']}"]
    for kind in ("business_message", "edited_business_message"):
        if kind in update:
            return [f"bc:{update[kind]['business_connection_id']}"]
    if "deleted_business_messages" in update:
        return [f"bc:{update['deleted_business_messages']['business_connection_id']}"]
    if "callback_query" in update:
        route = str(update["callback_query"].get("data", "")).split(":", 1)[0]
        return [f"r:{route}"] if ROUTE.fullmatch(route) else []
    message = update.get("message")
    if not isinstance(message, Mapping) or message.get("chat", {}).get("type") != "private":
        return []
    text = str(message.get("text", ""))
    if started := _START.match(text):
        return [f"r:{started.group(1)}"]
    if linked := _LINK.match(text):
        return [link_key(linked.group(1))]
    replied = message.get("reply_to_message")
    if isinstance(replied, Mapping):
        return ["m:" + message_id(message["chat"]["id"], replied["message_id"])]
    return []


def events(update: Mapping[str, Any], owner_user_id: str | None) -> Sequence[InboundEvent]:
    """The events of one update for the connection its key named. owner_user_id: the
    business account's Telegram user (its own messages are staff messages)."""
    event_id = f"tg:{update['update_id']}"
    if "business_connection" in update:
        connection = update["business_connection"]
        rights = connection.get("rights") or {}
        can_reply = bool(rights.get("can_reply", connection.get("can_reply", False)))
        return [
            ConnectionUpdate(
                event_id,
                "business_connection",
                {
                    "business_connection_id": str(connection["id"]),
                    "user_id": str(connection["user"]["id"]),
                    "username": str(connection["user"].get("username") or ""),
                    "is_enabled": "true" if connection.get("is_enabled") else "false",
                    "can_reply": "true" if can_reply else "false",
                },
            )
        ]
    if "business_message" in update:
        return _business_message(event_id, update["business_message"], owner_user_id)
    if "callback_query" in update:
        return _button(event_id, update["callback_query"])
    message = update.get("message")
    if not isinstance(message, Mapping):
        return []  # edited or deleted business messages: ignored (logged by the core)
    sender = str(message.get("from", {}).get("id", ""))
    text = str(message.get("text", ""))
    if started := _START.match(text):
        return [ManagerAction(event_id, sender, "enroll", code=started.group(2))]
    if _LINK.match(text):
        return [
            ConnectionUpdate(
                event_id, "link", {"owner_user_id": sender, "chat_id": str(message["chat"]["id"])}
            )
        ]
    replied = message.get("reply_to_message")
    if isinstance(replied, Mapping) and text:
        return [
            ManagerReply(
                event_id,
                sender,
                text,
                message_id(message["chat"]["id"], replied["message_id"]),
                _at(message),
            )
        ]
    return []


def _business_message(
    event_id: str, message: Mapping[str, Any], owner_user_id: str | None
) -> list[InboundEvent]:
    if message.get("sender_business_bot") or message.get("from", {}).get("is_bot"):
        return []  # what we (or another bot) sent through the business account
    sender = str(message.get("from", {}).get("id", ""))
    files: list[InboundFile] = []
    skipped = [label for kind, label in _SKIPPED_KINDS.items() if kind in message]
    document = message.get("document")
    if isinstance(document, Mapping):
        files.append(
            InboundFile(
                str(document["file_id"]),
                str(document.get("file_name") or "document"),
                str(document.get("mime_type") or "application/octet-stream"),
                document.get("file_size"),
            )
        )
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = photos[-1]  # Telegram lists sizes from small to large
        files.append(
            InboundFile(
                str(largest["file_id"]), "photo.jpg", "image/jpeg", largest.get("file_size")
            )
        )
    return [
        CustomerMessage(
            event_id,
            str(message["chat"]["id"]),
            str(message.get("text") or message.get("caption") or ""),
            occurred_at=_at(message),
            files=tuple(files),
            skipped=tuple(skipped),
            from_business=owner_user_id is not None and sender == owner_user_id,
        )
    ]


def _button(event_id: str, query: Mapping[str, Any]) -> list[InboundEvent]:
    parts = str(query.get("data", "")).split(":")
    sender = str(query.get("from", {}).get("id", ""))
    if len(parts) != 3 or parts[1] not in _ACTIONS:
        return []
    action, reference = _ACTIONS[parts[1]], parts[2]
    if action in _DECISIONS:
        return [ManagerAction(event_id, sender, "decision", reference=reference, action=action)]
    if action in ("confirm", "cancel"):
        answer = "yes" if action == "confirm" else "no"
        return [ManagerAction(event_id, sender, "confirm", reference=reference, action=answer)]
    try:
        reference_id = str(UUID(hex=reference))
    except ValueError:
        return []
    return [CustomerAction(event_id, sender, reference_id, action)]


def _at(message: Mapping[str, Any]) -> datetime | None:
    stamp = message.get("date")
    return None if not isinstance(stamp, int) else datetime.fromtimestamp(stamp, UTC)
