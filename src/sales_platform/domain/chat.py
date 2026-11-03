"""Hosted customer chat (website chat, phase 4): public address, limits and allowed websites.

Numbers confirmed by the user on 2026-10-28 (Q9, Q11, form time asked mid-work). They are
settings: ChatLimits() holds the defaults, and the configuration may override them.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from sales_platform.domain.accounts import RateRule

HANDLE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")
MAX_ALLOWED_ORIGINS = 10
MAX_GREETING = 300
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # Q11, same as API attachments


@dataclass(frozen=True)
class ChatLimits:
    max_message_chars: int = 2000
    messages_per_minute: int = 10
    messages_per_day: int = 100
    sessions_per_ip_hour: int = 20
    session_idle: timedelta = timedelta(days=30)
    readings_per_business_day: int = 200
    files_per_message: int = 5
    files_per_session_day: int = 20
    max_file_bytes: int = MAX_ATTACHMENT_BYTES
    min_form_time: timedelta = timedelta(seconds=2)

    def rules(self) -> "ChatRules":
        return ChatRules(
            RateRule("chat_session_minute", self.messages_per_minute, timedelta(minutes=1)),
            RateRule("chat_session_day", self.messages_per_day, timedelta(days=1)),
            RateRule("chat_start_ip", self.sessions_per_ip_hour, timedelta(hours=1)),
            RateRule("chat_files_day", self.files_per_session_day, timedelta(days=1)),
        )


@dataclass(frozen=True)
class ChatRules:
    per_minute: RateRule
    per_day: RateRule
    start_per_ip: RateRule
    files_per_day: RateRule


def handle_from_name(name: str) -> str:
    """'Acme Industrial Pvt. Ltd.' -> 'acme-industrial-pvt-ltd' (3-64 characters)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:56].strip("-")
    return slug if len(slug) >= 3 else f"chat-{slug}".strip("-")


def validate_handle(handle: str) -> str:
    if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
        raise ValueError(
            "A chat address is 3-64 lower-case letters, digits and hyphens, "
            "starting and ending with a letter or digit"
        )
    return handle


def normalise_origin(raw: str) -> str:
    """'https://www.Example.com/' -> 'https://www.example.com' (scheme, host, port only)."""
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.path not in ("", "/"):
        raise ValueError("A website is written like https://www.example.com")
    if parts.query or parts.fragment or parts.username:
        raise ValueError("A website is written like https://www.example.com")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def validate_origins(raw: Sequence[str]) -> list[str]:
    if len(raw) > MAX_ALLOWED_ORIGINS:
        raise ValueError(f"At most {MAX_ALLOWED_ORIGINS} websites")
    return list(dict.fromkeys(normalise_origin(item) for item in raw))


def origin_allowed(origin: str | None, allowed: list[str], own: str | None) -> bool:
    """Browsers send Origin on cross-site requests. No Origin (same site, or not a browser)
    is allowed here; bots are handled by the rate limits, not by Origin."""
    if origin is None:
        return True
    try:
        clean = normalise_origin(origin)
    except ValueError:
        return False
    return clean in allowed or (own is not None and clean == own)
