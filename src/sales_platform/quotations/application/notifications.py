"""Approval requests to linked manager accounts, through the outbox (ADR 0013, 0018).

notify_approvers queues one approval_request message per active approver binding (idempotency
key approval-request:<task>:<binding>), with the review text as fixed wording. The single-use
action token is never stored with the message: ApprovalRequestHook creates it at send time,
stores only its hash as an action reference (rotating an unsent one, so only the token actually
sent can act) and marks the reference sent afterwards. Each token is single use, expires, and
only works for the account it was sent to.

A crash while handing a request over leaves the message 'unknown' and it is not resent
automatically (user decision P1-Q6); the task stays visible in the portal.
"""

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sales_platform.integrations.application.messaging import OutboundService
from sales_platform.integrations.domain.messages import (
    APPROVAL_ACTIONS,
    OutboundMessage,
    TemplateKey,
)
from sales_platform.integrations.ports.connections import ConnectionStore
from sales_platform.integrations.ports.messages import MessageLinks, SendPreparation
from sales_platform.integrations.ports.providers import SendReceipt
from sales_platform.platform.domain.jobs import PermanentJobError
from sales_platform.platform.domain.security import hash_secret
from sales_platform.platform.ports.jobs import ClaimedJob
from sales_platform.quotations.domain.message_texts import render_approval_request
from sales_platform.quotations.ports.channels import NotificationStore

REFERENCE_LIFETIME = timedelta(hours=24)

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_action_token() -> str:
    return secrets.token_urlsafe(24)


class ApprovalNotificationHandler:
    """Job handler for notify_approvers, payload {"task_id": "..."}."""

    def __init__(
        self, store: NotificationStore, connections: ConnectionStore, outbound: OutboundService
    ) -> None:
        self._store = store
        self._connections = connections
        self._outbound = outbound

    def __call__(self, job: ClaimedJob) -> None:
        try:
            task_id = UUID(job.payload["task_id"])
        except (KeyError, ValueError):
            raise PermanentJobError("Job payload has no valid task_id") from None
        packet = self._store.review_packet(job.require_tenant(), task_id)
        if packet is None:
            return  # decided or gone: nothing to announce
        for binding in self._store.approver_bindings(job.require_tenant()):
            connection = self._connections.get(job.require_tenant(), binding.connection_ref)
            if connection is None or not connection.active:
                logger.warning("Binding %s has no active connection", binding.binding_id)
                continue
            self._outbound.queue(
                connection,
                template=TemplateKey.APPROVAL_REQUEST,
                render=render_approval_request(packet),
                params={"task_id": str(task_id), "binding_id": str(binding.binding_id)},
                recipient=binding.external_user_id,
                idempotency_key=f"approval-request:{task_id}:{binding.binding_id}",
                links=MessageLinks(
                    quotation_id=packet.quotation_id,
                    version=packet.version,
                    approval_task_id=task_id,
                    binding_id=binding.binding_id,
                ),
                correlation_id=job.correlation_id,
            )


class ApprovalRequestHook:
    """Send hook for approval_request: the action token exists only at send time."""

    def __init__(
        self,
        store: NotificationStore,
        clock: Callable[[], datetime] = utc_now,
        token_factory: Callable[[], str] = new_action_token,
        portal_url: str | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._token_factory = token_factory
        self._portal = None if portal_url is None else portal_url.rstrip("/") + "/"

    def before_send(self, message: OutboundMessage) -> SendPreparation:
        task_id, binding_id = message.approval_task_id, message.binding_id
        if task_id is None or binding_id is None:
            return SendPreparation(cancel_reason="The approval request has no task or binding")
        if self._store.review_packet(message.tenant_id, task_id) is None:
            return SendPreparation(cancel_reason="The approval task was already decided")
        token = self._token_factory()
        expires_at = self._clock() + REFERENCE_LIFETIME
        reference_id = self._store.prepare_reference(
            message.tenant_id, task_id, binding_id, hash_secret(token), expires_at
        )
        if reference_id is None:
            return SendPreparation(already_sent=True)
        self._store.link_message(message.tenant_id, message.message_id, reference_id)
        # The signed link (phase 5): the same single-use token, on a portal page that needs
        # sign-in as this manager (P5-14).
        link = None if self._portal is None else f"{self._portal}#/approve/{token}"
        return SendPreparation(
            action_reference=token,
            expires_at=expires_at,
            actions=APPROVAL_ACTIONS,
            context={"reference_id": str(reference_id)},
            link=link,
        )

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None:
        self._store.mark_sent(message.tenant_id, UUID(preparation.context["reference_id"]))
