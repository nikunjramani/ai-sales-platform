from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from sales_platform.platform.config import Settings

RUNTIME_URL = SecretStr("postgresql+psycopg://app:private@localhost/db")
# What production needs besides the database (phase 2: a real mailer and public addresses).
PRODUCTION: dict[str, Any] = {
    "environment": "production",
    "mailer": "real_mailer",
    "mail_from": "no-reply@quotes.example",
    "public_portal_url": "https://quotes.example/portal/",
    "public_base_url": "https://quotes.example",
    "chat_ip_key": "0123456789abcdef0123456789abcdef",
}


def test_settings_require_postgres_and_hide_credentials() -> None:
    settings = Settings(_env_file=None, database_url=RUNTIME_URL)
    assert "private" not in repr(settings)
    with pytest.raises(ValidationError, match="postgresql"):
        Settings(_env_file=None, database_url=SecretStr("sqlite:///example.db"))


def test_missing_migration_url_has_no_runtime_fallback() -> None:
    settings = Settings(_env_file=None, database_url=RUNTIME_URL, migration_database_url=None)
    with pytest.raises(ValueError, match="MIGRATION_DATABASE_URL"):
        settings.migration_url()


def test_ai_model_is_generic_and_its_key_stays_hidden() -> None:
    settings = Settings(
        _env_file=None,
        database_url=RUNTIME_URL,
        model="google_genai:gemini-3.5-flash-lite",
        model_api_key=SecretStr("synthetic-model-key"),
        model_options={"timeout": 30},
    )
    assert "synthetic-model-key" not in repr(settings)
    assert Settings(_env_file=None, database_url=RUNTIME_URL).model is None  # rules, no AI
    with pytest.raises(ValidationError, match="model"):
        Settings(_env_file=None, database_url=RUNTIME_URL, model="gemini-3.5-flash-lite")


def test_secret_store_defaults_and_production_rule() -> None:
    development = Settings(_env_file=None, database_url=RUNTIME_URL)
    assert development.secret_store_name == "local"
    production = Settings(_env_file=None, database_url=RUNTIME_URL, **PRODUCTION)
    assert production.secret_store_name == "none"
    with pytest.raises(ValidationError, match="local secret store"):
        Settings(_env_file=None, database_url=RUNTIME_URL, **PRODUCTION, secret_store="local")
    keyed = Settings(
        _env_file=None, database_url=RUNTIME_URL, secret_store_key=SecretStr("synthetic-key")
    )
    assert "synthetic-key" not in repr(keyed)


def test_account_settings_defaults_in_development() -> None:
    settings = Settings(_env_file=None, database_url=RUNTIME_URL, demo_password=SecretStr("x" * 20))
    assert settings.dev_login_enabled is True
    assert settings.mailer == "console"
    assert settings.mail_sender == "no-reply@localhost.example"
    assert settings.portal_url == "http://localhost:8000/portal/"
    assert "x" * 20 not in repr(settings)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"dev_login": True}, "dev login"),
        ({"demo_password": SecretStr("synthetic-demo-pass")}, "Demo passwords"),
        ({"mailer": "console"}, "console mailer"),
        ({"mail_from": None}, "MAIL_FROM"),
        ({"public_portal_url": "http://quotes.example/portal/"}, "https"),
        ({"chat_ip_key": None}, "CHAT_IP_KEY"),
        ({"public_base_url": "http://quotes.example"}, "PUBLIC_BASE_URL"),
    ],
)
def test_production_refuses_development_account_settings(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, database_url=RUNTIME_URL, **{**PRODUCTION, **change})


def test_production_turns_the_dev_login_off() -> None:
    settings = Settings(_env_file=None, database_url=RUNTIME_URL, **PRODUCTION)
    assert settings.dev_login_enabled is False
