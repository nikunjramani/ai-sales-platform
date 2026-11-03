"""Customers (contacts), their addresses on each tool (identities) and consent (phase 4).

- A contact is one customer of one business. Website chat visitors give a name and an email or
  phone (D12); these are stored as *claimed* (Q2) and never used to merge or to send.
- An identity is one address of a contact on one connection: a chat visitor ID, an API
  customer_reference, later a phone number on WhatsApp. Messages are tied to identities.
- Consent (D15): a whole message STOP / UNSUBSCRIBE / OPT OUT opts the identity out, START opts
  it back in. An opted-out identity is sent nothing except the opt-out confirmation.
- Viewers see email and phone masked (Q12); other staff roles see them in full.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from sales_platform.accounts.domain.accounts import InvalidEmail, email_hint, normalize_email

MAX_CONTACT_NAME = 100
MAX_PHONE_DIGITS = 15
MIN_PHONE_DIGITS = 8


class ConsentStatus(StrEnum):
    IMPLIED = "implied"  # they wrote to us first; replies are expected
    OPTED_IN = "opted_in"  # they sent START after opting out
    OPTED_OUT = "opted_out"


# The whole message, any case, surrounding spaces ignored (D15). "stop the pumps" is no command.
_CONSENT_WORDS = {
    "stop": ConsentStatus.OPTED_OUT,
    "unsubscribe": ConsentStatus.OPTED_OUT,
    "opt out": ConsentStatus.OPTED_OUT,
    "start": ConsentStatus.OPTED_IN,
}


def consent_command(text: str) -> ConsentStatus | None:
    """The consent change a message asks for, or None if it is an ordinary message."""
    return _CONSENT_WORDS.get(" ".join(text.split()).casefold())


class InvalidContact(ValueError):
    """Name, email or phone not accepted (the message is safe to show to the visitor)."""


def clean_name(raw: str) -> str:
    name = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not name or len(name) > MAX_CONTACT_NAME or not name.isprintable():
        raise InvalidContact(f"Please give a name of 1-{MAX_CONTACT_NAME} characters")
    return name


def normalise_email(raw: str) -> str:
    try:
        return normalize_email(raw)
    except InvalidEmail as error:
        raise InvalidContact(str(error)) from None


def normalise_phone(raw: str) -> str:
    """E.164 shape: "+" and 8-15 digits. Spaces, dashes, dots and brackets are dropped.

    No number lookups and no country guessing: a number without "+" is refused.
    """
    text = re.sub(r"[\s\-().]", "", raw) if isinstance(raw, str) else ""
    if not re.fullmatch(rf"\+\d{{{MIN_PHONE_DIGITS},{MAX_PHONE_DIGITS}}}", text):
        raise InvalidContact("Please give the phone number with its country code, e.g. +91...")
    return text


def mask_email(email: str | None) -> str | None:
    return None if email is None else email_hint(email)


def mask_phone(phone: str | None) -> str | None:
    """'+91******210': the country code and the last three digits stay visible."""
    if phone is None:
        return None
    return phone[:3] + "*" * max(len(phone) - 6, 0) + phone[-3:]


@dataclass(frozen=True)
class ClaimedDetails:
    """What a website chat visitor typed when starting a chat (not verified, Q2)."""

    name: str
    email: str | None
    phone: str | None

    @classmethod
    def parse(cls, name: str, email: str | None, phone: str | None) -> "ClaimedDetails":
        email = email.strip() if email else None
        phone = phone.strip() if phone else None
        if not email and not phone:
            raise InvalidContact("Please give an email address or a phone number")
        return cls(
            clean_name(name),
            normalise_email(email) if email else None,
            normalise_phone(phone) if phone else None,
        )


def may_receive(consent: ConsentStatus, is_opt_out_confirmation: bool) -> bool:
    """Opted-out identities get nothing except the confirmation that they opted out."""
    return consent is not ConsentStatus.OPTED_OUT or is_opt_out_confirmation
