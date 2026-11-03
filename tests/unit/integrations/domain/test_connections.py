"""Connections: purposes, statuses, names and the legacy connection key."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from sales_platform.integrations.domain.connections import (
    Capabilities,
    Connection,
    ConnectionStatus,
    DeliveryMode,
    Purpose,
    validate_display_name,
    validate_purposes,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)
ID = UUID("00000000-0000-4000-8000-000000000001")


def connection(account_ref: str | None, status: ConnectionStatus) -> Connection:
    return Connection(
        ID, ID, "simulated", "Demo phone", frozenset({Purpose.MANAGER_APPROVALS}), {}, {},
        account_ref, status, None, "seed", NOW, NOW,
    )  # fmt: skip


def test_legacy_key_is_the_account_ref_or_the_id() -> None:
    assert connection("simulated-local", ConnectionStatus.ACTIVE).legacy_key == "simulated-local"
    assert connection(None, ConnectionStatus.ACTIVE).legacy_key == str(ID)


def test_only_active_connections_are_active() -> None:
    assert connection(None, ConnectionStatus.ACTIVE).active
    for status in (ConnectionStatus.SETTING_UP, ConnectionStatus.ERROR, ConnectionStatus.DISABLED):
        assert not connection(None, status).active
    assert connection(None, ConnectionStatus.ACTIVE).serves(Purpose.MANAGER_APPROVALS)
    assert not connection(None, ConnectionStatus.ACTIVE).serves(Purpose.CATALOGUE)


def test_capability_defaults_are_a_push_tool_without_webhooks() -> None:
    capabilities = Capabilities()
    assert capabilities.delivery_mode is DeliveryMode.PUSH and not capabilities.webhooks
    assert capabilities.max_inbound_bytes == 64 * 1024


def test_display_names() -> None:
    assert validate_display_name("  Shop phone ") == "Shop phone"
    for bad in ("", "   ", "x" * 101, "tab\tname"):
        with pytest.raises(ValueError):
            validate_display_name(bad)


def test_purposes_must_be_offered_and_not_empty() -> None:
    offered = frozenset({Purpose.MANAGER_APPROVALS, Purpose.CATALOGUE})
    assert validate_purposes(["catalogue"], offered) == {Purpose.CATALOGUE}
    for bad in ([], ["customer_messages"], ["nonsense"]):
        with pytest.raises(ValueError):
            validate_purposes(bad, offered)
