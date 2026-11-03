"""Moved here by ADR 0026 (module order)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class InboundEventType(StrEnum):
    """Kinds of normalised events a provider callback can carry (ADR 0018)."""

    CUSTOMER_MESSAGE = "customer_message"
    MANAGER_ACTION = "manager_action"
    DELIVERY_STATUS = "delivery_status"
    CONSENT_CHANGE = "consent_change"
    # Phase 5:
    MANAGER_REPLY = "manager_reply"
    CUSTOMER_ACTION = "customer_action"
    CONNECTION_UPDATE = "connection_update"
    # Phase 7: "where is my order?" as the whole message (fixed words, like STOP/START).
    ORDER_STATUS_REQUEST = "order_status_request"


@dataclass(frozen=True)
class InboundFile:
    """A file a customer sent that is not downloaded yet: the provider fetches it by file_ref
    (Provider.fetch_file). Names and types are the sender's claims, checked in quarantine."""

    file_ref: str
    filename: str
    content_type: str
    size_bytes: int | None = None


@dataclass(frozen=True)
class CustomerMessage:
    provider_event_id: str
    sender_external_id: str
    text: str
    occurred_at: datetime | None = None
    thread_id: str | None = None
    reply_to_external_id: str | None = None
    files: tuple[InboundFile, ...] = ()
    skipped: tuple[str, ...] = ()  # kinds we do not read (voice, video...), noted for staff
    # The business typed it itself on its own account (e.g. a Telegram business chat): kept
    # in the conversation as a staff message, never read as the customer's words.
    from_business: bool = False


@dataclass(frozen=True)
class ManagerAction:
    """A linked manager's enroll code, button decision or answer to a confirmation question.

    kind "enroll": code. kind "decision": reference (the review's action token) and action.
    kind "confirm": reference (the confirmation's token) and action "yes" or "no" (P5-13).
    """

    provider_event_id: str
    external_user_id: str
    kind: str  # "enroll" | "decision" | "confirm"
    code: str | None = None
    reference: str | None = None
    action: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ManagerReply:
    """Free text from a linked manager, read later (D10). It names the message it answers by
    the provider's ID for that message; without it nothing can be decided (safety rule 2)."""

    provider_event_id: str
    external_user_id: str
    text: str
    reply_to_provider_message_id: str | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class CustomerAction:
    """A customer pressed Accept or Decline on a quotation (phase 4 references, D11)."""

    provider_event_id: str
    sender_external_id: str
    reference: str  # the customer action reference ID the button carried
    action: str  # "accept" | "decline"


@dataclass(frozen=True)
class ConnectionUpdate:
    """The provider reports progress of a business's setup (the business linked its account,
    switched our bot off...). Handed to Provider.resume with the connection's pending step."""

    provider_event_id: str
    action_id: str
    data: Mapping[str, str]


@dataclass(frozen=True)
class DeliveryStatusUpdate:
    """A provider's later report on a message we sent (named to avoid DeliveryStatus)."""

    provider_event_id: str
    provider_message_id: str
    status: str  # "delivered" | "failed"
    detail: str | None = None


@dataclass(frozen=True)
class ConsentChange:
    provider_event_id: str
    sender_external_id: str
    opted_in: bool


@dataclass(frozen=True)
class OrderStatusRequest:
    provider_event_id: str
    sender_external_id: str
    order_number: str | None = None  # None: all of the customer's orders


type InboundEvent = (
    CustomerMessage
    | ManagerAction
    | ManagerReply
    | CustomerAction
    | ConnectionUpdate
    | DeliveryStatusUpdate
    | ConsentChange
    | OrderStatusRequest
)


_EVENT_TYPES: dict[type, InboundEventType] = {
    CustomerMessage: InboundEventType.CUSTOMER_MESSAGE,
    ManagerAction: InboundEventType.MANAGER_ACTION,
    ManagerReply: InboundEventType.MANAGER_REPLY,
    CustomerAction: InboundEventType.CUSTOMER_ACTION,
    ConnectionUpdate: InboundEventType.CONNECTION_UPDATE,
    DeliveryStatusUpdate: InboundEventType.DELIVERY_STATUS,
    ConsentChange: InboundEventType.CONSENT_CHANGE,
    OrderStatusRequest: InboundEventType.ORDER_STATUS_REQUEST,
}


_EVENT_CLASSES = {kind: cls for cls, kind in _EVENT_TYPES.items()}


def event_type(event: InboundEvent) -> InboundEventType:
    return _EVENT_TYPES[type(event)]


MAX_PROVIDER_EVENT_ID = 200


def validate_event(event: InboundEvent) -> InboundEvent:
    """Provider event IDs are the dedupe key: 1-200 printable characters."""
    key = event.provider_event_id
    if not isinstance(key, str) or not 0 < len(key) <= MAX_PROVIDER_EVENT_ID:
        raise ValueError("Provider event IDs are 1-200 characters")
    if not key.isprintable():
        raise ValueError("Provider event IDs must be printable")
    return event


def _plain(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, InboundFile):
        return dict(vars(value))
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return dict(value)
    return value


def event_to_json(event: InboundEvent) -> dict[str, object]:
    """Stored form of a non-enquiry event in inbox_events.envelope."""
    data: dict[str, object] = {"type": event_type(event).value}
    for name, value in vars(event).items():
        data[name] = _plain(value)
    return data


def event_from_json(data: dict[str, object]) -> InboundEvent:
    kind = InboundEventType(str(data["type"]))
    values = {name: value for name, value in data.items() if name != "type"}
    if "occurred_at" in values:
        occurred = values["occurred_at"]
        values["occurred_at"] = datetime.fromisoformat(str(occurred)) if occurred else None
    if kind is InboundEventType.CUSTOMER_MESSAGE:
        files = values.get("files") or []
        assert isinstance(files, list)
        values["files"] = tuple(InboundFile(**item) for item in files)
        values["skipped"] = tuple(values.get("skipped") or ())  # type: ignore[arg-type]
    event: InboundEvent = _EVENT_CLASSES[kind](**values)
    return event
