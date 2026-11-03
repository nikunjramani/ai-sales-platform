from datetime import timedelta

import pytest

from sales_platform.platform.domain.jobs import retry_delay


@pytest.mark.parametrize(
    ("attempt", "expected_ceiling"),
    [(1, 5), (2, 10), (3, 20), (8, 600), (40, 600)],  # 5s doubling, capped at 10 minutes
)
def test_backoff_doubles_up_to_cap(attempt: int, expected_ceiling: int) -> None:
    assert retry_delay(attempt, jitter=lambda: 0.999999) < timedelta(seconds=expected_ceiling)
    assert retry_delay(attempt, jitter=lambda: 0.5) == timedelta(seconds=expected_ceiling / 2)
    assert retry_delay(attempt, jitter=lambda: 0.0) == timedelta(0)


def test_attempt_numbers_start_at_one() -> None:
    with pytest.raises(ValueError):
        retry_delay(0, jitter=lambda: 0.5)
