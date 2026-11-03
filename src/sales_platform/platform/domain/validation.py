"""Small guards shared by domain value objects. They fail loudly instead of guessing."""

from datetime import datetime
from decimal import Decimal


def require_decimal(value: object, name: str) -> Decimal:
    """Accept only finite Decimals; floats cannot represent most money values exactly."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{name} must be a finite Decimal")
    return value


def require_aware(value: datetime, name: str) -> datetime:
    """Reject naive datetimes so freshness and effective-date checks are unambiguous."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def require_text(value: str, name: str, max_length: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{name} must be non-empty text of at most {max_length} characters")
    return value
