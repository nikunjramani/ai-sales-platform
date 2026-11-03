"""The signed approval link (phase 5, P5-14): one click after sign-in, as the right manager.

Every approval request sent to a linked account also carries a portal link with the same
single-use token as its buttons (only its SHA-256 is stored). Opening it needs sign-in
(the portal session; P5-14), and only the manager the request was sent to may use it; anyone
else gets "this link is not for you". The page shows the exact version; a deliberate click
(POST with CSRF, never the GET) decides through the same ApprovalService as everything else,
and consumes the reference. Unknown tokens are "not found"; decided, changed or expired
requests show their state instead of buttons.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sales_platform.application.approvals import ApprovalService, DecisionResult
from sales_platform.domain.quotations import (
    PORTAL_TRANSPORT,
    ApprovalAction,
    DecisionCommand,
    ReviewPacket,
    TaskStatus,
)
from sales_platform.ports.channels import NotificationStore
from sales_platform.ports.manager_replies import (
    ApprovalLinkLookup,
    ApprovalRequestState,
    ReplyStore,
)

MAX_TOKEN = 100


def utc_now() -> datetime:
    return datetime.now(UTC)


class LinkNotFound(Exception):
    """No such link (one answer for every unknown token)."""


class LinkNotForYou(Exception):
    """The link was sent to another manager."""


class LinkState(StrEnum):
    OPEN = "open"
    DECIDED = "decided"
    CHANGED = "changed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class LinkView:
    request: ApprovalRequestState
    state: LinkState
    packet: ReviewPacket | None  # only while open


class ApprovalLinkService:
    def __init__(
        self,
        links: ApprovalLinkLookup,
        requests: ReplyStore,
        packets: NotificationStore,
        approvals: ApprovalService,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._links = links
        self._requests = requests
        self._packets = packets
        self._approvals = approvals
        self._clock = clock

    def view(self, subject: str, token: str) -> LinkView:
        request = self._request(subject, token)
        state = self._state(request)
        packet = None
        if state is LinkState.OPEN:
            packet = self._packets.review_packet(request.binding.tenant_id, request.task_id)
            if packet is None:  # decided a moment ago
                state = LinkState.DECIDED
        return LinkView(request, state, packet)

    def decide(
        self,
        subject: str,
        token: str,
        command_id: UUID,
        action: ApprovalAction,
        content_sha256: str,
        reason: str | None,
    ) -> DecisionResult:
        """The approval service's errors pass through (conflict, stale version, permission)."""
        request = self._request(subject, token)
        if self._state(request) is LinkState.EXPIRED and not request.consumed:
            raise LinkNotFound("This link has expired")
        command = DecisionCommand(
            command_id=command_id,
            tenant_id=request.binding.tenant_id,
            task_id=request.task_id,
            content_sha256=content_sha256,
            action=action,
            reason=reason,
            transport=PORTAL_TRANSPORT,
            evidence_ref=f"approval-link:{request.reference_id}",
            received_at=self._clock(),
        )
        return self._approvals.decide(subject, command, request.reference_id)

    def _request(self, subject: str, token: str) -> ApprovalRequestState:
        if not token or len(token) > MAX_TOKEN:
            raise LinkNotFound("Not found")
        found = self._links.find(hashlib.sha256(token.encode("utf-8")).hexdigest())
        request = None if found is None else self._requests.request(*found)
        if request is None:
            raise LinkNotFound("Not found")
        if request.binding.subject != subject:
            raise LinkNotForYou("This link is not for you")
        return request

    def _state(self, request: ApprovalRequestState) -> LinkState:
        if request.decided is not None or request.task_status is not TaskStatus.PENDING:
            return LinkState.DECIDED
        if request.version != request.current_version:
            return LinkState.CHANGED
        if request.consumed or request.expires_at <= self._clock():
            return LinkState.EXPIRED
        return LinkState.OPEN
