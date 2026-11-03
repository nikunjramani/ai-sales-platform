"""Customers answering a quotation from their own tool (phase 4, D11, Q1).

- With "quotation ready", QuotationReadyHook issues one reference for that version and that
  customer identity (customer_action_refs). Tools with buttons get Accept/Decline buttons that
  carry the reference ID; the tool proves who pressed them, and the presser must be that
  identity. Tools without buttons get a secure link instead: a random token whose SHA-256 is
  stored (single use, expires at "valid until"; opening the page changes nothing).
- CustomerActionService turns a press or a confirmed link into the same answer core as the API
  (QuotationResponseService.respond_by_reference): version and hash check, open check,
  re-pricing on accept, append-only record.
- Every refusal about the reference itself (unknown, other identity, expired) is one generic
  CustomerActionRejected, so nothing is learnt by guessing.
"""

import hashlib
import secrets
from dataclasses import replace
from uuid import UUID

from sales_platform.application.acceptance import QuotationResponseService
from sales_platform.application.providers import ProviderRegistry
from sales_platform.domain.acceptance import RecordedResponse, ResponseAction
from sales_platform.domain.messages import OutboundMessage
from sales_platform.ports.conversations import CustomerTargets
from sales_platform.ports.customer_actions import (
    CustomerActionRef,
    CustomerActionStore,
    NewActionRef,
)
from sales_platform.ports.documents import DeliveryStore
from sales_platform.ports.messages import SendHook, SendPreparation
from sales_platform.ports.providers import SendReceipt

INVALID = "This link or button is no longer valid"
ANSWER_ACTIONS = (ResponseAction.ACCEPT.value, ResponseAction.DECLINE.value)


class CustomerActionRejected(Exception):
    """Unknown, expired or someone else's reference (one message for all)."""


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CustomerActionService:
    def __init__(self, refs: CustomerActionStore, responses: QuotationResponseService) -> None:
        self._refs = refs
        self._responses = responses

    def by_button(self, tenant_id: UUID, ref_id: UUID, sender_identity: UUID) -> CustomerActionRef:
        """The reference a button carried, if the presser is the customer it was sent to."""
        ref = self._refs.get(tenant_id, ref_id)
        if ref is None or ref.identity_id != sender_identity:
            raise CustomerActionRejected(INVALID)
        return ref

    def by_link(self, token: str) -> CustomerActionRef:
        if not token or len(token) > 200:
            raise CustomerActionRejected(INVALID)
        ref = self._refs.find_by_token(token_sha256(token))
        if ref is None:
            raise CustomerActionRejected(INVALID)
        return ref

    def act(
        self, ref: CustomerActionRef, action: ResponseAction, reason: str | None
    ) -> tuple[RecordedResponse, bool]:
        """Raises the answer core's errors: NotCurrentOffer, OfferNotOpen, PricesChanged,
        ErpError (nothing recorded; try again)."""
        return self._responses.respond_by_reference(ref, action, reason)


class QuotationReadyHook:
    """Send hook for quotation_ready: the PDF (inner hook) plus Accept/Decline."""

    def __init__(
        self,
        inner: SendHook,
        deliveries: DeliveryStore,
        targets: CustomerTargets,
        refs: CustomerActionStore,
        registry: ProviderRegistry,
        public_base_url: str,
    ) -> None:
        self._inner = inner
        self._deliveries = deliveries
        self._targets = targets
        self._refs = refs
        self._registry = registry
        self._base = public_base_url.rstrip("/")

    def before_send(self, message: OutboundMessage) -> SendPreparation:
        prepared = self._inner.before_send(message)
        if prepared.cancel_reason is not None:
            return prepared
        document = (
            None
            if message.document_id is None
            else self._deliveries.document(message.tenant_id, message.document_id)
        )
        target = (
            None
            if message.enquiry_id is None
            else self._targets.for_enquiry(message.tenant_id, message.enquiry_id)
        )
        if document is None or document.valid_until is None or target is None:
            return SendPreparation(cancel_reason="The quotation is not available to answer")
        provider = self._registry.find(message.provider_key)
        buttons = provider is not None and provider.capabilities.buttons
        token = None if buttons else secrets.token_urlsafe(32)
        ref_id = self._refs.issue(
            NewActionRef(
                tenant_id=message.tenant_id,
                quotation_id=document.quotation_id,
                version=document.version,
                content_sha256=document.content_sha256,
                identity_id=target.identity_id,
                conversation_id=target.conversation_id,
                outbound_message_id=message.message_id,
                expires_at=document.valid_until,
                token_sha256=None if token is None else token_sha256(token),
            )
        )
        # Never stored in the outbox: the link token exists only in what the provider sends.
        reference = str(ref_id) if token is None else f"{self._base}/q/{token}"
        return replace(
            prepared,
            actions=ANSWER_ACTIONS,
            action_reference=reference,
            expires_at=document.valid_until,
        )

    def after_send(
        self, message: OutboundMessage, preparation: SendPreparation, receipt: SendReceipt
    ) -> None:
        self._inner.after_send(message, preparation, receipt)
