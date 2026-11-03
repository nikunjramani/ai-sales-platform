"""Text of the quotation messages managers and customers get (moved from the outbox's
templates, ADR 0026). Callers pass the Rendered result to the outbox, which knows no
quotation types."""

from uuid import UUID

from sales_platform.integrations.domain.messages import Rendered
from sales_platform.quotations.domain.acceptance import Offer, RecordedResponse, ResponseOutcome
from sales_platform.quotations.domain.quotations import ReviewPacket, quotation_number


def render_approval_request(packet: ReviewPacket) -> Rendered:
    """The review packet for linked managers (TemplateKey.APPROVAL_REQUEST)."""
    return Rendered(
        _review_text(packet),
        {
            "task_id": str(packet.task_id),
            "quotation": packet.number,
            "version": str(packet.version),
        },
    )


def render_response_notice(offer: Offer, response: RecordedResponse) -> Rendered:
    """A customer's accept/decline, for managers (TemplateKey.RESPONSE_NOTICE)."""
    return Rendered(_notice_text(offer, response), {"notice_id": str(response.response_id)})


def render_quotation_document(quotation_id: UUID, version: int) -> Rendered:
    """TemplateKey.QUOTATION_DOCUMENT: the document itself is the message. Customer-facing
    covering text for push providers is an open decision (D22), so none is invented here."""
    return Rendered("", {"quotation": quotation_number(quotation_id), "version": str(version)})


def _review_text(packet: ReviewPacket) -> str:
    """Plain-text review summary for messaging channels. Amounts are shown exactly as stored."""
    content = packet.content
    currency = str(content["currency"])
    lines = content["lines"]
    assert isinstance(lines, list)
    text = [
        f"{packet.tenant_name}: approval needed for quotation {packet.number} "
        f"version {packet.version}",
        f"Customer reference: {packet.customer_reference or 'not given'}",
        "",
    ]
    for line in lines:
        text.append(
            f"- {line['sku']} {line['description']}: {line['quantity']} {line['unit']} x "
            f"{line['unit_price']} = {line['line_total']} {currency} "
            f"(stock: {line['stock_status']})"
        )
    text += [
        "",
        f"Subtotal {content['subtotal']} {currency}; tax {content['tax']} {currency}; "
        f"total {content['total']} {currency}",
    ]
    if content.get("stock_review_required"):
        text.append("Attention: at least one line is not confirmed as in stock.")
    text.append(f"Content hash: {packet.content_sha256[:12]}")
    return "\n".join(text)


def _notice_text(offer: Offer, response: RecordedResponse) -> str:
    total = f"{offer.content['total']} {offer.content['currency']}"
    what = {
        ResponseOutcome.ACCEPTED: "accepted",
        ResponseOutcome.DECLINED: "declined",
    }[response.outcome]
    text = (
        f"{offer.tenant_name}: the customer {what} quotation "
        f"{quotation_number(response.quotation_id)} version {response.version} (total {total})."
    )
    if response.reason:
        text += f" Reason: {response.reason}"
    return text
