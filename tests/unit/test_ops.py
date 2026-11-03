"""The operator command validates company-level settings and never prints secrets."""

import json
from pathlib import Path

import pytest

from sales_platform import ops
from sales_platform.platform.adapters.secrets.local import new_key


@pytest.fixture(autouse=True)
def local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings needs a database URL even though this command never connects (CI has no .env).
    monkeypatch.setenv(
        "SALES_DATABASE_URL",
        "postgresql+psycopg://sales_app:unused@127.0.0.1/sales_dev",
    )
    monkeypatch.setenv("SALES_ENVIRONMENT", "development")
    monkeypatch.setenv("SALES_SECRET_STORE", "local")
    monkeypatch.setenv("SALES_SECRET_STORE_KEY", new_key())
    monkeypatch.setenv("SALES_STORAGE_DIR", str(tmp_path))


def test_settings_are_checked_stored_and_shown_masked() -> None:
    raw = json.dumps(
        {"bot_token": "123:secret", "bot_username": "quote_bot", "webhook_secret_token": "x" * 20}
    )
    shown = ops.set_config("telegram", raw)
    assert "123:secret" not in shown and "quote_bot" in shown
    again = ops.show_config("telegram")
    assert "123:secret" not in again and json.loads(again)["bot_token"] == "********"


def test_invalid_settings_or_unknown_providers_stop_with_a_message() -> None:
    with pytest.raises(SystemExit, match="Invalid settings"):
        ops.set_config("telegram", json.dumps({"bot_username": "x"}))
    with pytest.raises(SystemExit, match="No installed provider"):
        ops.set_config("nothing", "{}")
    with pytest.raises(SystemExit, match="no company-level settings"):
        ops.set_config("web_chat", "{}")
