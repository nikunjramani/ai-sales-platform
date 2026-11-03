"""Moved here by ADR 0026 (module order)."""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class RateRule:
    """At most `limit` hits per `window` for one key (an IP address, an email, a person...)."""

    name: str
    limit: int
    window: timedelta
