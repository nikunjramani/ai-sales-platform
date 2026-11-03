"""Outgoing messages: the outbox row, its statuses and the fixed wording (ADR 0018).

Everything we send (a quotation to a customer, an approval request or a notice to a manager)
is first stored as an outbound message, then handed to the connection's provider by the
send_message job. Statuses say what we actually know, never more (same rules as ADR 0015):
- pending:   stored, not yet attempted (or a transient failure that will be retried);
- sending:   being handed to the provider; a crash here means we cannot know if it arrived;
- available: pull providers (the API): the recipient can now fetch it;
- sent:      push providers: the provider accepted it;
- delivered: the provider later confirmed it reached the recipient;
- failed:    it cannot be sent (connection gone, refused, task already decided...);
- unknown:   the provider may or may not have it. Never resent automatically.

Wording is fixed in code (templates), never written by AI. A template turns parameters into the
message text plus metadata a provider may show (quotation number, task ID...). Single-use
approval tokens are never part of a stored message: they are created at send time.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sales_platform.integrations.domain.connections import Purpose


class MessageStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    AVAILABLE = "available"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


# After these the worker does nothing more (a person may act on failed/unknown).
FINAL_MESSAGE_STATUSES = frozenset(
    {
        MessageStatus.AVAILABLE,
        MessageStatus.SENT,
        MessageStatus.DELIVERED,
        MessageStatus.FAILED,
        MessageStatus.UNKNOWN,
    }
)


class TemplateKey(StrEnum):
    APPROVAL_REQUEST = "approval_request"
    RESPONSE_NOTICE = "response_notice"
    QUOTATION_DOCUMENT = "quotation_document"
    # Phase 2 account emails (to people using the portal, not to customers):
    ACCOUNT_VERIFY_EMAIL = "account_verify_email"
    ACCOUNT_PASSWORD_RESET = "account_password_reset"
    ACCOUNT_INVITATION = "account_invitation"
    ACCOUNT_EXISTS = "account_exists"
    ACCOUNT_PASSWORD_CHANGED = "account_password_changed"
    # Phase 4 messages to customers on their own tool (wording confirmed by the user, Q7/Q10).
    ENQUIRY_RECEIVED = "enquiry_received"
    CLARIFICATION_QUESTIONS = "clarification_questions"
    QUOTATION_READY = "quotation_ready"
    ANSWER_ACCEPTED = "answer_accepted"
    ANSWER_DECLINED = "answer_declined"
    PRICES_CHANGED = "prices_changed"
    QUOTATION_WITHDRAWN = "quotation_withdrawn"
    QUOTATION_EXPIRED = "quotation_expired"
    OPTED_OUT = "opted_out"
    OPTED_IN = "opted_in"  # START confirmation (asked mid-work, 2026-10-28)
    # Phase 5: approval by message (D10). To linked managers, about one approval request.
    APPROVAL_CONFIRM = "approval_confirm"  # "You want to approve ...? YES / NO" (P5-13)
    APPROVAL_REASON = "approval_reason"  # after a Reject/Change button: "reply with the reason"
    APPROVAL_CLARIFY = "approval_clarify"  # the reply was not clear (D10 rule 3)
    APPROVAL_RECORDED = "approval_recorded"  # what was recorded (D10 rule 4)
    APPROVAL_CANCELLED = "approval_cancelled"  # the manager answered NO
    APPROVAL_STATE = "approval_state"  # already decided, changed or expired
    APPROVAL_HELP = "approval_help"  # no reply-to, or written replies are not read
    # Phase 7 orders, to customers (fixed wording, ADR 0024): a status change, and the answer
    # to "where is my order?".
    ORDER_CONFIRMED = "order_confirmed"
    ORDER_SHIPPED = "order_shipped"
    ORDER_DELIVERED = "order_delivered"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_STATUS = "order_status"
    ORDER_NONE = "order_none"


# The buttons an approval request offers (a provider without buttons may ignore them).
APPROVAL_ACTIONS = ("approve", "reject", "request_revision")


@dataclass(frozen=True)
class OutboundMessage:
    message_id: UUID
    tenant_id: UUID
    connection_id: UUID
    provider_key: str
    purpose: Purpose
    template_key: TemplateKey
    params: Mapping[str, str]  # IDs only; never tokens or secrets
    recipient: str
    idempotency_key: str
    body_text: str
    metadata: Mapping[str, str]
    status: MessageStatus
    document_id: UUID | None
    quotation_id: UUID | None
    version: int | None
    approval_task_id: UUID | None
    binding_id: UUID | None
    enquiry_id: UUID | None
    response_id: UUID | None
    correlation_id: UUID
    provider_message_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Rendered:
    text: str
    metadata: Mapping[str, str]


def render_template(key: TemplateKey, params: Mapping[str, object]) -> Rendered:
    """Fixed wording for one template. Unknown or missing parameters are a programming error.

    Quotation messages (approval request, customer-answer notice, the PDF) are rendered by
    quotations/domain/message_texts.py and account emails by accounts/domain/emails.py; their
    senders pass the Rendered text (ADR 0026: the outbox knows no business types).
    """
    if key is TemplateKey.CLARIFICATION_QUESTIONS:
        questions = params["questions"]
        assert isinstance(questions, (list, tuple)) and questions
        lines = "\n".join(f"- {single_line(str(question))}" for question in questions)
        return Rendered(f"We need a bit more information:\n{lines}", {})
    if key is TemplateKey.ORDER_STATUS:
        orders = params["orders"]
        assert isinstance(orders, (list, tuple)) and orders
        lines = "\n".join(f"- {single_line(str(order))}" for order in orders)
        business = single_line(str(params["business"]))
        return Rendered(CUSTOMER_WORDING[key].format(business=business, orders=lines), {})
    if key in CUSTOMER_WORDING:
        return _customer_message(key, params)
    if key in MANAGER_WORDING:
        return _manager_message(key, params)
    raise ValueError(f"Unknown template {key}")


# Fixed customer wording (Q7, Q10, D15). {number} is the quotation number, {date} its "valid
# until" (same format as the PDF), {business} the business name. The staff withdrawal reason
# is never shown to the customer (Q10).
CUSTOMER_WORDING: Mapping[TemplateKey, str] = {
    TemplateKey.ENQUIRY_RECEIVED: "Thanks, we are preparing your quotation.",
    TemplateKey.QUOTATION_READY: (
        "Your quotation {number} (version {version}) is ready: total {total} {currency}, "
        "valid until {date}."
    ),
    TemplateKey.ANSWER_ACCEPTED: "Thank you, we received your acceptance of {number}.",
    TemplateKey.ANSWER_DECLINED: "Thank you, we recorded that you declined {number}.",
    TemplateKey.PRICES_CHANGED: (
        "Prices have changed since quotation {number}. "
        "We are preparing an updated quotation for you."
    ),
    TemplateKey.QUOTATION_WITHDRAWN: (
        "Quotation {number} has been withdrawn. Please contact us if you still need it."
    ),
    TemplateKey.QUOTATION_EXPIRED: (
        "Quotation {number} has expired (it was valid until {date}). "
        "Send us a message if you would like an updated quotation."
    ),
    TemplateKey.OPTED_OUT: (
        "You will not receive more messages from {business}. Reply START to receive messages again."
    ),
    TemplateKey.OPTED_IN: "You will receive messages from {business} again.",
    # Orders (phase 7). {order} is the order number, {number} the quotation number.
    TemplateKey.ORDER_CONFIRMED: "Your order {order} (quotation {number}) is confirmed.",
    TemplateKey.ORDER_SHIPPED: "Your order {order} has been shipped.",
    TemplateKey.ORDER_DELIVERED: "Your order {order} has been delivered. Thank you!",
    TemplateKey.ORDER_CANCELLED: (
        "Your order {order} has been cancelled. Please contact us if you have questions."
    ),
    # {orders} is one "O-... (quotation Q-...): status, total 10.00 EUR" per line.
    TemplateKey.ORDER_STATUS: "Your orders with {business}:\n{orders}",
    TemplateKey.ORDER_NONE: "We could not find an order of yours with {business}.",
}
CUSTOMER_TEMPLATES = frozenset(CUSTOMER_WORDING) | {TemplateKey.CLARIFICATION_QUESTIONS}


# Fixed manager wording for approval by message (phase 5, plan section 7.2 as confirmed).
# Each template has variants chosen by params["variant"]: the action (approve, reject,
# request_revision) or the state. {number} and {version} name the one request; {business}
# starts every message because one bot may serve managers of several businesses.
MANAGER_WORDING: Mapping[TemplateKey, Mapping[str, str]] = {
    TemplateKey.APPROVAL_CONFIRM: {
        "approve": "{business}: you want to approve quotation {number} version {version}? "
        "Reply YES to record it or NO to cancel.",
        "reject": "{business}: you want to reject quotation {number} version {version} "
        'with the reason "{reason}"? Reply YES to record it or NO to cancel.',
        "request_revision": "{business}: you want to request a revision of quotation "
        '{number} version {version}: "{reason}"? Reply YES to record it or NO to cancel.',
    },
    TemplateKey.APPROVAL_REASON: {
        "reject": "{business}: reply to this message with the reason for rejecting "
        "quotation {number} version {version}.",
        "request_revision": "{business}: reply to this message with what should change in "
        "quotation {number} version {version}.",
    },
    TemplateKey.APPROVAL_CLARIFY: {
        "unclear": "{business}: do you want to approve, reject or request a revision of "
        "quotation {number} version {version}? Reply to this message with APPROVE, REJECT "
        "and the reason, or CHANGE and what to change.",
    },
    TemplateKey.APPROVAL_RECORDED: {
        "approve": "{business}: approved quotation {number} version {version}, total {total} "
        "{currency}.",
        "reject": "{business}: rejected quotation {number} version {version}. Reason: {reason}",
        "request_revision": "{business}: revision requested for quotation {number} version "
        "{version}: {reason}",
    },
    TemplateKey.APPROVAL_CANCELLED: {
        "cancelled": "{business}: nothing was recorded for quotation {number} version {version}.",
    },
    TemplateKey.APPROVAL_STATE: {
        "decided": "{business}: quotation {number} version {version} was already {state}.",
        "changed": "{business}: quotation {number} has changed; you will get the new version "
        "to review.",
        "expired": "{business}: this approval request for quotation {number} has expired. "
        "Please decide in the portal.",
    },
    TemplateKey.APPROVAL_HELP: {
        "reply_to": "To answer an approval request, reply to its message (tap and hold it, "
        "then Reply) or use its buttons.",
        "not_read": "{business}: written replies are not read here. Please use the buttons "
        "on the approval message or open its link.",
    },
}
MANAGER_TEMPLATES = frozenset(MANAGER_WORDING)


def _manager_message(key: TemplateKey, params: Mapping[str, object]) -> Rendered:
    values = {name: single_line(str(value)) for name, value in params.items()}
    wording = MANAGER_WORDING[key][values["variant"]]
    metadata = {name: values[name] for name in ("number", "version") if name in values}
    return Rendered(wording.format_map(_Strict(values)), metadata)


def format_valid_until(moment: datetime) -> str:
    """The quotation PDF's "valid until" format, with the time zone named."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M") + " UTC"


def _customer_message(key: TemplateKey, params: Mapping[str, object]) -> Rendered:
    """Fill the placeholders; a missing one is a programming error (KeyError)."""
    values = {name: single_line(str(value)) for name, value in params.items()}
    metadata = {name: values[name] for name in ("number", "version") if name in values}
    return Rendered(CUSTOMER_WORDING[key].format_map(_Strict(values)), metadata)


class _Strict(dict[str, str]):
    def __missing__(self, name: str) -> str:
        raise KeyError(f"Template parameter {name!r} is missing")


def single_line(value: str) -> str:
    """No line breaks in anything that goes into an email subject (header injection)."""
    return " ".join(value.split())
