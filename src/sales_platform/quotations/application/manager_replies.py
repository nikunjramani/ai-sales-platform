"""Approval by message (D10, phase 5): buttons, written replies, questions and confirmations.

Three ways in, one decision path (ChannelActionService -> ApprovalService, the same checks as
the portal: approver role, no self-approval, one decision, current version):
1. A button on the review message. Approve decides at once. Reject and Change first ask
   "reply with the reason" (a prompt of kind reason); the reply to that question is the
   reason, and the action is the button's (no AI involved, so it also works without a model).
2. A written reply to the review (or to any later message about the same request):
   - it must come from the linked account of that request (rule 1) and reply to one of our
     messages about it (rule 2), otherwise the manager is told how to answer;
   - without a model it is not read (P5-5): the manager is told to use buttons or the link;
   - otherwise the classify_manager_reply job asks the reader, applies the deterministic
     guards and asks "You want to <action> Q-... version N? YES / NO" (P5-13), or asks
     "approve, reject or revise?" when unclear (rule 3). Only an exact YES (a reply or the
     Yes button) records it; NO cancels; anything else is read as a new reply.
3. The signed link (api/approval_links.py), which needs sign-in.
After every decision the manager gets what was recorded (rule 4); a request that was decided,
changed or expired meanwhile gets that state instead. Every written reply is audited.
"""

import logging
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sales_platform.enquiries.ports.interpretation import InterpretationRejected
from sales_platform.integrations.application.messaging import OutboundService
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.domain.events import ManagerAction, ManagerReply
from sales_platform.integrations.domain.messages import OutboundMessage, TemplateKey
from sales_platform.integrations.ports.connections import ConnectionStore
from sales_platform.integrations.ports.inbound import InboundStore
from sales_platform.integrations.ports.messages import MessageLinks, SendPreparation
from sales_platform.integrations.ports.providers import SendReceipt
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.domain.security import hash_secret
from sales_platform.platform.domain.tenancy import AuthorizationError
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.quotations.application.approvals import DecisionResult
from sales_platform.quotations.application.channel_actions import (
    ChannelActionRejected,
    ChannelActionService,
    ChannelEvent,
    ChannelEventKind,
)
from sales_platform.quotations.domain.manager_replies import (
    ACTION_OF,
    ReplyClassification,
    ReplyIntent,
    clean_reply,
    confirm_word,
    guard,
)
from sales_platform.quotations.domain.quotations import (
    ApprovalAction,
    DecisionConflict,
    StaleVersion,
    TaskNotFound,
    TaskStatus,
)
from sales_platform.quotations.ports.channels import ChannelBinding
from sales_platform.quotations.ports.manager_replies import (
    ApprovalRequestState,
    PromptKind,
    ReplyPrompt,
    ReplyStore,
)
from sales_platform.quotations.ports.reply_classification import ReplyClassifier

logger = logging.getLogger(__name__)
CONFIRM_ACTIONS = ("confirm", "cancel")
_DONE = {
    ApprovalAction.APPROVE: "approved",
    ApprovalAction.REJECT: "rejected",
    ApprovalAction.REQUEST_REVISION: "sent back for a revision",
}
_STATUS_DONE = {
    TaskStatus.APPROVED: "approved",
    TaskStatus.REJECTED: "rejected",
    TaskStatus.REVISION_REQUESTED: "sent back for a revision",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


class ManagerReplyService:
    def __init__(
        self,
        store: ReplyStore,
        actions: ChannelActionService,
        outbound: OutboundService,
        connections: ConnectionStore,
        inbound: InboundStore,
        classifier: ReplyClassifier | None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._actions = actions
        self._outbound = outbound
        self._connections = connections
        self._inbound = inbound
        self._classifier = classifier
        self._clock = clock

    # --- buttons ---------------------------------------------------------------------------
    def on_button(self, connection: Connection, action: ManagerAction, inbox_event_id: UUID) -> str:
        """A decision button on a review, or Yes/No on a confirmation question."""
        if action.kind == "confirm":
            return self._confirm_button(connection, action)
        try:
            decision = ApprovalAction(action.action or "")
        except ValueError:
            return "rejected"
        event = self._event(connection, action.provider_event_id, action.external_user_id)
        try:
            reference, _ = self._actions.check(
                _with(event, reference=action.reference, action=decision)
            )
        except ChannelActionRejected:
            return "rejected"
        request = self._store.request(connection.tenant_id, reference.reference_id)
        if request is None:
            return "rejected"
        if self._closed(request):
            self._send_state(connection, request, f"state:{inbox_event_id}")
            return "closed"
        if decision is not ApprovalAction.APPROVE and not (action.reason or "").strip():
            prompt = self._store.open_prompt(
                connection.tenant_id,
                request.reference_id,
                PromptKind.REASON,
                decision,
                None,
                request.expires_at,
                inbox_event_id,
            )
            self._send_prompt(connection, request, prompt)
            return "reason asked"
        return self._decide(connection, request, event, decision, action.reason, None)

    def _confirm_button(self, connection: Connection, action: ManagerAction) -> str:
        binding = self._store.binding_on(
            connection.tenant_id, connection.connection_id, action.external_user_id
        )
        prompt = (
            None
            if not action.reference
            else self._store.prompt_by_token(connection.tenant_id, hash_secret(action.reference))
        )
        if binding is None or prompt is None or prompt.kind is not PromptKind.CONFIRM:
            return "rejected"
        request = self._store.request(connection.tenant_id, prompt.reference_id)
        if request is None or request.binding.binding_id != binding.binding_id:
            return "rejected"
        event = self._event(connection, action.provider_event_id, action.external_user_id)
        return self._answer(connection, request, prompt, action.action == "yes", event)

    # --- written replies -------------------------------------------------------------------
    def on_reply(self, connection: Connection, reply: ManagerReply, inbox_event_id: UUID) -> str:
        """Route for manager_reply events (process_inbound, worker)."""
        tenant_id = connection.tenant_id
        binding = self._store.binding_on(
            tenant_id, connection.connection_id, reply.external_user_id
        )
        if binding is None:
            return "not linked"  # an account we do not know: nothing is answered
        replied = (
            None
            if not reply.reply_to_provider_message_id
            else self._store.replied_message(
                tenant_id, connection.connection_id, reply.reply_to_provider_message_id
            )
        )
        if replied is None or replied.reference_id is None:
            self._send(
                connection, binding, None, TemplateKey.APPROVAL_HELP, "reply_to", {},
                f"approval-help:{inbox_event_id}",
            )  # fmt: skip
            return "no reply-to"
        request = self._store.request(tenant_id, replied.reference_id)
        if request is None or request.binding.binding_id != binding.binding_id:
            return "rejected"  # someone else's request (e.g. a forwarded message)
        text = clean_reply(reply.text)
        prompt = self._store.prompt_for_message(tenant_id, replied.outbound_message_id)
        event = self._event(connection, reply.provider_event_id, reply.external_user_id)
        if prompt is not None and not prompt.closed and prompt.expires_at > self._clock():
            if prompt.kind is PromptKind.REASON:
                if not text:
                    return "empty reason"
                if not self._store.close_prompt(tenant_id, prompt.prompt_id, "answered"):
                    return "already answered"
                return self._decide(connection, request, event, prompt.action, text, None)
            answer = confirm_word(text)
            if answer is not None:
                return self._answer(connection, request, prompt, answer, event)
            self._store.close_prompt(tenant_id, prompt.prompt_id, "replaced")
        if self._closed(request):
            self._send_state(connection, request, f"state:{inbox_event_id}")
            self._audit(tenant_id, inbox_event_id, request, text, None, "closed")
            return "closed"
        if self._classifier is None:
            self._send(
                connection, binding, request, TemplateKey.APPROVAL_HELP, "not_read", {},
                f"approval-help:{inbox_event_id}",
            )  # fmt: skip
            self._audit(tenant_id, inbox_event_id, request, text, None, "not read (no model)")
            return "not read"
        self._store.queue_classification(tenant_id, inbox_event_id, request.reference_id, uuid4())
        return "reading"

    def classify(self, job: ClaimedJob) -> None:
        """Job handler for classify_manager_reply, payload {inbox_event_id, reference_id}."""
        tenant_id = job.require_tenant()
        try:
            inbox_event_id = UUID(job.payload["inbox_event_id"])
            reference_id = UUID(job.payload["reference_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload needs inbox_event_id and reference_id") from None
        if self._store.classified(tenant_id, inbox_event_id):
            return  # a retried job after it finished
        stored = self._inbound.load(tenant_id, inbox_event_id)
        request = self._store.request(tenant_id, reference_id)
        if stored is None or request is None or not isinstance(stored.event, ManagerReply):
            raise PermanentJobError("Reply or approval request not found")
        connection = stored.connection
        text = clean_reply(stored.event.text)
        if self._closed(request):
            self._send_state(connection, request, f"state:{inbox_event_id}")
            self._audit(tenant_id, inbox_event_id, request, text, None, "closed")
            return
        suggestion = self._read(job, request, text)
        guarded = guard(text, request.number, suggestion)
        if guarded.intent is ReplyIntent.UNCLEAR:
            self._send(
                connection, request.binding, request, TemplateKey.APPROVAL_CLARIFY, "unclear",
                {}, f"approval-clarify:{inbox_event_id}",
            )  # fmt: skip
            self._audit(
                tenant_id, inbox_event_id, request, text, suggestion, "clarify asked",
                ReplyIntent.UNCLEAR, guarded.guard,
            )  # fmt: skip
            return
        action = ACTION_OF[guarded.intent]
        prompt = self._store.open_prompt(
            tenant_id,
            request.reference_id,
            PromptKind.CONFIRM,
            action,
            guarded.reason,
            request.expires_at,
            inbox_event_id,
        )
        self._send_prompt(connection, request, prompt)
        self._audit(
            tenant_id, inbox_event_id, request, text, suggestion, "confirmation asked",
            guarded.intent, guarded.guard,
        )  # fmt: skip

    # --- internals -------------------------------------------------------------------------
    def _read(
        self, job: ClaimedJob, request: ApprovalRequestState, text: str
    ) -> ReplyClassification:
        assert self._classifier is not None
        try:
            return self._classifier.classify(request.number, request.version, text)
        except InterpretationRejected:
            return ReplyClassification(ReplyIntent.UNCLEAR, None, "unreadable answer")
        except Exception:
            if job.attempt < job.max_attempts:
                raise  # wait and retry (ADR 0017); the manager sees nothing meanwhile
            logger.warning("The reply reader failed on the last attempt; asking the manager")
            return ReplyClassification(ReplyIntent.UNCLEAR, None, "reader unavailable")

    def _answer(
        self,
        connection: Connection,
        request: ApprovalRequestState,
        prompt: ReplyPrompt,
        yes: bool,
        event: ChannelEvent,
    ) -> str:
        if prompt.closed or prompt.expires_at <= self._clock():
            return "already answered"
        outcome = "confirmed" if yes else "cancelled"
        if not self._store.close_prompt(connection.tenant_id, prompt.prompt_id, outcome):
            return "already answered"  # the button and a reply at once: only one counts
        if not yes:
            self._send(
                connection, request.binding, request, TemplateKey.APPROVAL_CANCELLED,
                "cancelled", {}, f"approval-cancelled:{prompt.prompt_id}",
            )  # fmt: skip
            return "cancelled"
        return self._decide(connection, request, event, prompt.action, prompt.reason, "ai")

    def _decide(
        self,
        connection: Connection,
        request: ApprovalRequestState,
        event: ChannelEvent,
        action: ApprovalAction,
        reason: str | None,
        via: str | None,
    ) -> str:
        try:
            result = self._actions.decide_reference(
                _with(event, action=action, reason=reason, via=via), request.reference_id
            )
        except (DecisionConflict, StaleVersion, ChannelActionRejected, TaskNotFound):
            refreshed = self._store.request(connection.tenant_id, request.reference_id)
            self._send_state(connection, refreshed or request, f"state:{event.event_id}")
            return "conflict"
        except (AuthorizationError, ValueError):
            return "rejected"
        self._send_recorded(connection, request, result)
        return "replayed" if result.replayed else "recorded"

    def _send_recorded(
        self, connection: Connection, request: ApprovalRequestState, result: DecisionResult
    ) -> None:
        decision = result.decision
        self._send(
            connection,
            request.binding,
            request,
            TemplateKey.APPROVAL_RECORDED,
            decision.action.value,
            {"reason": decision.reason or "", "total": request.total, "currency": request.currency},
            f"approval-recorded:{decision.decision_id}",
        )

    def _send_state(self, connection: Connection, request: ApprovalRequestState, key: str) -> None:
        if request.task_status is not TaskStatus.PENDING or request.decided is not None:
            done = (
                _DONE[request.decided]
                if request.decided is not None
                else _STATUS_DONE.get(request.task_status, "decided")
            )
            variant, extra = "decided", {"state": done}
        elif request.version != request.current_version:
            variant, extra = "changed", {}
        else:
            variant, extra = "expired", {}
        self._send(
            connection, request.binding, request, TemplateKey.APPROVAL_STATE, variant, extra,
            f"approval-{key}",
        )  # fmt: skip

    def _send_prompt(
        self, connection: Connection, request: ApprovalRequestState, prompt: ReplyPrompt
    ) -> None:
        template = (
            TemplateKey.APPROVAL_CONFIRM
            if prompt.kind is PromptKind.CONFIRM
            else TemplateKey.APPROVAL_REASON
        )
        self._send(
            connection,
            request.binding,
            request,
            template,
            prompt.action.value,
            {"reason": prompt.reason or ""},
            f"approval-prompt:{prompt.prompt_id}",
            {"prompt_id": str(prompt.prompt_id)},
        )

    def _send(
        self,
        connection: Connection,
        binding: ChannelBinding,
        request: ApprovalRequestState | None,
        template: TemplateKey,
        variant: str,
        extra: Mapping[str, str],
        key: str,
        params: Mapping[str, str] | None = None,
    ) -> None:
        render: dict[str, object] = {"variant": variant, **extra}
        links = MessageLinks(binding_id=binding.binding_id)
        stored: dict[str, str] = {"binding_id": str(binding.binding_id), **(params or {})}
        if request is not None:
            render |= {
                "business": request.business_name,
                "number": request.number,
                "version": request.version,
            }
            links = MessageLinks(
                quotation_id=request.quotation_id,
                version=request.version,
                approval_task_id=request.task_id,
                binding_id=binding.binding_id,
                action_ref_id=request.reference_id,
            )
            stored["task_id"] = str(request.task_id)
        self._outbound.queue(
            connection,
            template=template,
            render=render,
            params=stored,
            recipient=binding.external_user_id,
            idempotency_key=key[:200],
            links=links,
            correlation_id=uuid4(),
        )

    def _closed(self, request: ApprovalRequestState) -> bool:
        return (
            request.consumed
            or request.decided is not None
            or request.task_status is not TaskStatus.PENDING
            or request.version != request.current_version
            or request.expires_at <= self._clock()
        )

    def _audit(
        self,
        tenant_id: UUID,
        inbox_event_id: UUID,
        request: ApprovalRequestState,
        text: str,
        suggestion: ReplyClassification | None,
        outcome: str,
        intent: ReplyIntent | None = None,
        guard_name: str | None = None,
    ) -> None:
        self._store.record_classification(
            tenant_id,
            inbox_event_id,
            request.reference_id,
            text,
            (intent or (suggestion.intent if suggestion else ReplyIntent.UNCLEAR)).value,
            suggestion.model if suggestion else "not read",
            guard_name,
            outcome,
        )

    @staticmethod
    def _event(connection: Connection, event_id: str, external_user_id: str) -> ChannelEvent:
        return ChannelEvent(
            channel=connection.provider_key,
            connection_id=connection.legacy_key,
            tenant_id=connection.tenant_id,
            event_id=event_id,
            external_user_id=external_user_id,
            kind=ChannelEventKind.DECISION,
        )


def _with(event: ChannelEvent, **changes: object) -> ChannelEvent:
    values = {**vars(event), **changes}
    return ChannelEvent(**values)


class ApprovalPromptHook:
    """Send hook for approval_confirm / approval_reason: the question's message is linked to
    its prompt at send time, and the Yes/No buttons get a fresh single-use token then."""

    def __init__(
        self,
        store: ReplyStore,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
    ) -> None:
        self._store = store
        self._token_factory = token_factory

    def before_send(self, message: OutboundMessage) -> SendPreparation:
        try:
            prompt_id = UUID(message.params["prompt_id"])
        except (KeyError, ValueError):
            return SendPreparation(cancel_reason="The question has no prompt")
        prompt = self._store.prompt(message.tenant_id, prompt_id)
        if prompt is None or prompt.closed:
            return SendPreparation(cancel_reason="The question was answered or replaced")
        token = self._token_factory() if prompt.kind is PromptKind.CONFIRM else None
        if not self._store.bind_prompt_message(
            message.tenant_id,
            prompt_id,
            message.message_id,
            None if token is None else hash_secret(token),
        ):
            return SendPreparation(cancel_reason="The question was answered or replaced")
        if token is None:
            return SendPreparation(expires_at=prompt.expires_at)
        return SendPreparation(
            action_reference=token, actions=CONFIRM_ACTIONS, expires_at=prompt.expires_at
        )

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None:
        return None
