"""Rate limits counted in PostgreSQL, shared by every API process (user answer Q5).

Fixed windows: the first attempt for a key opens a window of the rule's length; attempts are
counted until it ends, then the count starts again. One upsert does the counting atomically, so
two processes can never both see "the 10th attempt". The key (an IP address, an email...) is
stored only as a SHA-256 hash. A few expired rows are deleted on each call, so the table stays
small without a separate clean-up job.
"""

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import Engine, text

from sales_platform.platform.domain.rate_limits import RateRule
from sales_platform.platform.ports.rate_limits import RateDecision

_HIT = text(
    "INSERT INTO rate_limit_counters (rule, key_sha256, window_start, hits, expires_at) "
    "VALUES (:rule, :key, :now, 1, :expires) "
    "ON CONFLICT (rule, key_sha256) DO UPDATE SET "
    "window_start = CASE WHEN rate_limit_counters.expires_at <= :now "
    "  THEN :now ELSE rate_limit_counters.window_start END, "
    "hits = CASE WHEN rate_limit_counters.expires_at <= :now "
    "  THEN 1 ELSE rate_limit_counters.hits + 1 END, "
    "expires_at = CASE WHEN rate_limit_counters.expires_at <= :now "
    "  THEN :expires ELSE rate_limit_counters.expires_at END "
    "RETURNING hits, expires_at"
)
_CLEAN = text(
    "DELETE FROM rate_limit_counters WHERE ctid IN ("
    "  SELECT ctid FROM rate_limit_counters WHERE expires_at < :now LIMIT 50)"
)


def rate_key(rule: RateRule, key: str) -> str:
    return hashlib.sha256(f"{rule.name}:{key}".encode()).hexdigest()


class SqlRateLimiter:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def hit(self, rule: RateRule, key: str, now: datetime) -> RateDecision:
        with self._engine.begin() as connection:
            row = connection.execute(
                _HIT,
                {
                    "rule": rule.name,
                    "key": rate_key(rule, key),
                    "now": now,
                    "expires": now + rule.window,
                },
            ).one()
            connection.execute(_CLEAN, {"now": now})
        if row.hits <= rule.limit:
            return RateDecision(True, timedelta(0))
        return RateDecision(False, max(row.expires_at - now, timedelta(seconds=1)))
