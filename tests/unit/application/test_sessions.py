"""Portal sessions: hashed tokens, expiry, revocation and CSRF tokens."""

from datetime import datetime, timedelta
from typing import Any

import pytest

from sales_platform.application.api_keys import AuthenticationFailed, hash_secret
from sales_platform.application.sessions import SessionService, csrf_token_for
from sales_platform.ports.identity import VerifiedIdentity


@pytest.fixture
def clock(now: datetime) -> list[datetime]:
    return [now]


@pytest.fixture
def service(memory_sessions: Any, clock: list[datetime]) -> SessionService:
    return SessionService(memory_sessions, timedelta(hours=1), lambda: clock[0])


IDENTITY = VerifiedIdentity("manager@industrial.example", "dev")


def test_only_the_hash_is_stored_and_the_token_authenticates(
    service: SessionService, quotation_db: Any
) -> None:
    new = service.start(IDENTITY)
    assert new.token not in str(quotation_db.sessions)
    assert hash_secret(new.token) in quotation_db.sessions
    assert service.authenticate(new.token).subject == IDENTITY.subject
    assert new.csrf_token == csrf_token_for(new.token) != new.token


@pytest.mark.parametrize("token", [None, "", "unknown-token", "x" * 201])
def test_missing_or_unknown_tokens_fail(service: SessionService, token: str | None) -> None:
    with pytest.raises(AuthenticationFailed):
        service.authenticate(token)


def test_expired_and_ended_sessions_fail(service: SessionService, clock: list[datetime]) -> None:
    expiring = service.start(IDENTITY)
    ended = service.start(IDENTITY)
    service.end(ended.token)
    with pytest.raises(AuthenticationFailed):
        service.authenticate(ended.token)
    clock[0] += timedelta(hours=1)
    with pytest.raises(AuthenticationFailed):
        service.authenticate(expiring.token)


def test_csrf_header_must_match_this_session(service: SessionService) -> None:
    first, second = service.start(IDENTITY), service.start(IDENTITY)
    service.check_csrf(first.token, first.csrf_token)
    for supplied in (None, "", second.csrf_token):
        with pytest.raises(AuthenticationFailed):
            service.check_csrf(first.token, supplied)
