"""Moved here by ADR 0026 (module order)."""

from typing import TYPE_CHECKING
from uuid import UUID

from sales_platform.conversations.ports.conversations import CustomerTargets
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.domain.events import (
    CustomerAction,
    InboundEvent,
    ManagerAction,
    ManagerReply,
)
from sales_platform.platform.domain.tenancy import AuthorizationError
from sales_platform.quotations.application.approvals import DecisionResult
from sales_platform.quotations.application.channel_actions import (
    ChannelActionRejected,
    ChannelActionService,
    ChannelEvent,
    ChannelEventKind,
)
from sales_platform.quotations.application.customer_actions import (
    CustomerActionRejected,
    CustomerActionService,
)
from sales_platform.quotations.domain.acceptance import (
    NotCurrentOffer,
    OfferNotOpen,
    PricesChanged,
    ResponseAction,
)
from sales_platform.quotations.domain.quotations import (
    ApprovalAction,
    DecisionConflict,
    StaleVersion,
    TaskNotFound,
)
from sales_platform.quotations.ports.channels import ChannelBinding

if TYPE_CHECKING:
    from sales_platform.quotations.application.manager_replies import ManagerReplyService


class ManagerActionRoute:
    """Enrollment codes and approval decisions from a linked manager account.

    With a ManagerReplyService (the worker, phase 5) decision and Yes/No buttons go through it:
    Reject/Change first ask for the reason, and every decision is confirmed to the manager.
    """

    def __init__(
        self, actions: ChannelActionService, replies: "ManagerReplyService | None" = None
    ) -> None:
        self._actions = actions
        self._replies = replies

    def handle(
        self, connection: Connection, action: ManagerAction
    ) -> ChannelBinding | DecisionResult:
        """Route one action; raises the ChannelActionService's errors (the alias maps them)."""
        try:
            kind = ChannelEventKind(action.kind)
            decision = ApprovalAction(action.action) if action.action else None
        except ValueError:
            raise ChannelActionRejected("Action not accepted") from None
        return self._actions.handle(
            ChannelEvent(
                channel=connection.provider_key,
                connection_id=connection.legacy_key,
                tenant_id=connection.tenant_id,
                event_id=action.provider_event_id,
                external_user_id=action.external_user_id,
                kind=kind,
                code=action.code,
                reference=action.reference,
                action=decision,
                reason=action.reason,
            )
        )

    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        assert isinstance(event, ManagerAction)
        if self._replies is not None and event.kind in ("decision", "confirm"):
            return self._replies.on_button(connection, event, inbox_event_id)
        try:
            result = self.handle(connection, event)
        except (ChannelActionRejected, AuthorizationError, TaskNotFound, ValueError):
            return "rejected"  # final: a retry cannot make it acceptable
        except (DecisionConflict, StaleVersion):
            return "conflict"
        if isinstance(result, DecisionResult):
            return "replayed" if result.replayed else "recorded"
        return "enrolled"


class CustomerActionRoute:
    """Accept/Decline pressed on a quotation in the customer's tool (D11). The button carries
    the reference; the tool proves who pressed it, and it must be the customer it was sent to.
    The confirmation goes out through the same answer core as the API and website chat."""

    def __init__(self, targets: CustomerTargets, actions: CustomerActionService) -> None:
        self._targets = targets
        self._actions = actions

    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        assert isinstance(event, CustomerAction)
        target = self._targets.for_identity(
            connection.tenant_id, connection.connection_id, event.sender_external_id
        )
        try:
            ref_id = UUID(event.reference)
            action = ResponseAction(event.action)
        except ValueError:
            return "rejected"
        if target is None:
            return "rejected"
        try:
            ref = self._actions.by_button(connection.tenant_id, ref_id, target.identity_id)
            if ref.conversation_id != target.conversation_id:
                return "rejected"
            _, replayed = self._actions.act(ref, action, None)
        except CustomerActionRejected:
            return "rejected"
        except (NotCurrentOffer, OfferNotOpen, PricesChanged) as error:
            return type(error).__name__  # the answer core already told the customer
        return "replayed" if replayed else action.value


class ManagerReplyRoute:
    """A linked manager's written reply (D10): see application/manager_replies.py."""

    def __init__(self, replies: "ManagerReplyService") -> None:
        self._replies = replies

    def __call__(self, connection: Connection, event: InboundEvent, inbox_event_id: UUID) -> str:
        assert isinstance(event, ManagerReply)
        return self._replies.on_reply(connection, event, inbox_event_id)
