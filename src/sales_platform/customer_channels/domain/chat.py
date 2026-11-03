"""Hosted customer chat (website chat, phase 4): public address, limits and allowed websites.

Numbers confirmed by the user on 2026-10-28 (Q9, Q11, form time asked mid-work). They are
settings: ChatLimits() holds the defaults, and the configuration may override them.
"""

from dataclasses import dataclass
from datetime import timedelta

from sales_platform.integrations.domain.hosted_chat import normalise_origin
from sales_platform.platform.domain.rate_limits import RateRule

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
