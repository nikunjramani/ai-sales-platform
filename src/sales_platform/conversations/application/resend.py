"""Resend a customer message that could not be sent (phase 5, user answer 2026-10-29).

Administrators and sales operators may send a failed message again (for example when a tool
only delivers within 24 hours of the customer's last message, and the customer has written
again). The failed message stays as it was; a copy becomes a new message with its own send
job, conversation row and quotation delivery link. Each failed message is resent at most once
(a failed copy can be resent in turn). A message whose outcome is unknown is never resent:
it may have arrived.
"""

from uuid import UUID

from sales_platform.integrations.ports.messages import MessageOutbox
from sales_platform.platform.domain.tenancy import Permission, TenantContext


class MessageNotResendable(Exception):
    """No failed customer message with that ID in this business."""


class ResendService:
    def __init__(self, outbox: MessageOutbox) -> None:
        self._outbox = outbox

    def resend(self, context: TenantContext, message_id: UUID) -> UUID:
        context.require(Permission.RESEND_MESSAGES)
        created = self._outbox.resend(context.tenant_id, message_id, context.subject)
        if created is None:
            raise MessageNotResendable("Not found")
        return created
