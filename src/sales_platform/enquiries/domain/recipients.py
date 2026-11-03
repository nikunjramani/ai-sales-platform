"""Moved here by ADR 0026 (module order)."""

from sales_platform.integrations.domain.connections import DeliveryMode


class NoDeliveryAddress(Exception):
    """The enquiry carries no address to send the quotation back to."""


def delivery_recipient(
    mode: DeliveryMode, connection_key: str, sender_external_id: str | None
) -> str:
    """Who receives the approved quotation: always whoever sent the enquiry, never an address
    taken from message text. Decided once, at intake (stored as the envelope's reply_to).

    - Pull providers (the API): the connection (API key) that submitted it. The API's
      sender_external_id is the customer's own reference label, not an address.
    - Push providers (email, chat): the provider's verified sender address or chat ID.
    """
    if mode is DeliveryMode.PULL:
        return connection_key
    if not sender_external_id:
        raise NoDeliveryAddress("The enquiry has no sender address to reply to")
    return sender_external_id
