"""The installed package metadata registers the in-repo pluggable parts (run `uv sync`)."""

from importlib.metadata import entry_points

from sales_platform.composition import build_secret_store
from sales_platform.config import Settings


def test_providers_are_registered() -> None:
    names = {entry.name for entry in entry_points(group="sales_platform.providers")}
    assert {"api", "simulated"} <= names


def test_secret_stores_are_registered() -> None:
    names = {entry.name for entry in entry_points(group="sales_platform.secret_stores")}
    assert {"local", "none"} <= names


def test_production_gets_the_none_store() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://app:x@localhost/db",
        environment="production",
        mailer="real_mailer",
        mail_from="no-reply@quotes.example",
        public_portal_url="https://quotes.example/portal/",
        public_base_url="https://quotes.example",
        chat_ip_key="0123456789abcdef0123456789abcdef",
    )
    assert type(build_secret_store(settings)).__name__ == "NoSecretStore"
