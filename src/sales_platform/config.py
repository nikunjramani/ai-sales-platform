"""Validated configuration. Database credentials are never included in object reprs."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SALES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: SecretStr
    migration_database_url: SecretStr | None = None
    worker_database_url: SecretStr | None = None
    # Private storage root: tenant folders with quarantined/clean attachments and quotation PDFs.
    storage_dir: Path = Path("var/storage")
    # Enquiry reader (ADR 0017). Unset = free offline rules, no AI. Otherwise any LangChain chat
    # model as "<provider>:<model>", e.g. "google_genai:gemini-3.5-flash-lite"; the provider's
    # package must be installed. Options are passed to the model as is (e.g. timeout).
    model: str | None = Field(default=None, pattern=r"^[a-z0-9_-]+:\S+$")
    model_api_key: SecretStr | None = None
    model_options: dict[str, Any] = Field(default_factory=dict)
    # Portal sign-in is always email + password (phase 2, D1). The dev login additionally lets
    # you pick a seeded demo person without a password; unset = on in development/test, off in
    # production, where turning it on is refused. Replaces QUOTATION_AUTH_PROVIDER.
    dev_login: bool | None = None
    # Password of the seeded demo people (development/test seed only; user answer Q24).
    # init_local_env.py --add-missing fills it with a random value. Unset = no demo passwords.
    demo_password: SecretStr | None = None
    # Account emails (phase 2): an entry point name in sales_platform.mailers. "console" writes
    # files under storage_dir/dev-mail and is refused in production (a real sender: phase 5).
    mailer: str = Field(default="console", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    mailer_options: dict[str, Any] = Field(default_factory=dict)
    # Sender address and the portal address used in email links. Development defaults below;
    # production must set both (https for the portal).
    mail_from: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+$")
    public_portal_url: str | None = Field(default=None, pattern=r"^https?://\S+/$")
    # Number of reverse proxies in front of the API whose X-Forwarded-For can be trusted for
    # the client IP used by rate limits. 0 = use the direct peer address.
    trusted_proxy_count: int = Field(default=0, ge=0, le=5)
    # Simultaneous Argon2id hashes (64 MiB each).
    password_hash_concurrency: int = Field(default=4, ge=1, le=16)
    session_hours: float = Field(default=8.0, gt=0, le=24)
    # Cookies are always Secure in production; set true locally when serving over HTTPS.
    session_cookie_secure: bool = False
    # Shared secret for signed simulated-channel events (dev/test only; unset = disabled).
    simulated_channel_secret: SecretStr | None = None
    # Where provider secrets live (D8): an entry point name in sales_platform.secret_stores.
    # Unset = "local" (encrypted files, needs secret_store_key) in development/test and "none"
    # in production until a cloud store is chosen (D24). Options go to the store as is.
    secret_store: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    secret_store_key: SecretStr | None = None
    secret_store_options: dict[str, Any] = Field(default_factory=dict)
    # Product source a new business starts with (phase 3): an installed provider key with the
    # catalogue purpose. Empty = new businesses start without one ("No product source").
    default_catalogue_provider: str | None = Field(
        default="local_catalogue", pattern=r"^[a-z][a-z0-9_]{1,31}$"
    )
    # Public address of this API: webhook URLs, website chat links and secure answer links
    # (/q/<token>) in customer messages (phase 4).
    public_base_url: str | None = Field(default=None, pattern=r"^https?://\S+$")
    # Key for hashing website chat visitors' IP addresses (rate limits; the address itself is
    # never stored). Required in production; development without it uses a random key per
    # process (limits then reset on restart).
    chat_ip_key: SecretStr | None = None
    # Phase 5: how long a public link to a quotation PDF works, for tools that download
    # attachments from a URL instead of receiving them (minutes).
    media_link_minutes: int = Field(default=15, ge=1, le=24 * 60)
    # Built React portal (portal/dist) to serve at /portal; unset = not served by the API.
    portal_dist_dir: Path | None = None

    @model_validator(mode="after")
    def no_dev_features_in_production(self) -> "Settings":
        if self.environment != "production":
            return self
        if self.dev_login or self.simulated_channel_secret is not None:
            raise ValueError("The dev login and simulated channel are not allowed in production")
        if self.demo_password is not None:
            raise ValueError("Demo passwords are not allowed in production")
        if self.secret_store == "local":
            raise ValueError("The local secret store is not allowed in production")
        if self.mailer == "console":
            raise ValueError("The console mailer is not allowed in production")
        if self.mail_from is None:
            raise ValueError("SALES_MAIL_FROM is required in production")
        if self.public_portal_url is None or not self.public_portal_url.startswith("https://"):
            raise ValueError("SALES_PUBLIC_PORTAL_URL must be an https address in production")
        if self.chat_ip_key is None:
            raise ValueError("SALES_CHAT_IP_KEY is required in production")
        if self.public_base_url is None or not self.public_base_url.startswith("https://"):
            raise ValueError("SALES_PUBLIC_BASE_URL must be an https address in production")
        return self

    @property
    def secret_store_name(self) -> str:
        if self.secret_store is not None:
            return self.secret_store
        return "none" if self.environment == "production" else "local"

    @property
    def dev_login_enabled(self) -> bool:
        if self.dev_login is not None:
            return self.dev_login
        return self.environment != "production"

    @property
    def mail_sender(self) -> str:
        return self.mail_from or "no-reply@localhost.example"

    @property
    def portal_url(self) -> str:
        return self.public_portal_url or "http://localhost:8000/portal/"

    @property
    def cookie_secure(self) -> bool:
        return self.environment == "production" or self.session_cookie_secure

    @field_validator("database_url", "migration_database_url", "worker_database_url")
    @classmethod
    def postgres_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("A valid PostgreSQL URL is required") from None
        if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
            raise ValueError("Use postgresql+psycopg with an explicit host and database")
        return value

    def migration_url(self) -> str:
        """Explicit owner credentials are required; never fall back to the app role."""
        if self.migration_database_url is None:
            raise ValueError("SALES_MIGRATION_DATABASE_URL is required for migrations/seed")
        return self.migration_database_url.get_secret_value()

    def worker_url(self) -> str:
        """The worker uses its own role; never fall back to the API or owner role."""
        if self.worker_database_url is None:
            raise ValueError("SALES_WORKER_DATABASE_URL is required for the job worker")
        return self.worker_database_url.get_secret_value()
