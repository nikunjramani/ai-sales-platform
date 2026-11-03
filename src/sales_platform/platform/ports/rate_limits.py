"""Moved here by ADR 0026 (module order)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sales_platform.platform.domain.rate_limits import RateRule


# --- Rate limits ------------------------------------------------------------------------------
@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: timedelta


class RateLimiter(Protocol):
    def hit(self, rule: RateRule, key: str, now: datetime) -> RateDecision:
        """Count one attempt for this key; keys are hashed before they are stored."""
        ...
