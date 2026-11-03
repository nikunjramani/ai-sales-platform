"""Company-level Telegram settings: our one bot (phase 5, contracts C5).

Stored by an operator with `python -m sales_platform.ops platform-config set telegram` in the
platform secret namespace (name "config", JSON), never in .env or the business portal.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_SECRET_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


class TelegramPlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: SecretStr = Field(description="From BotFather; secret")
    bot_username: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{3,31}$", description="Without @")
    webhook_secret_token: SecretStr = Field(
        description="16-256 characters A-Z a-z 0-9 _ -; Telegram sends it with every update"
    )

    @field_validator("webhook_secret_token")
    @classmethod
    def _token(cls, value: SecretStr) -> SecretStr:
        if not _SECRET_TOKEN.fullmatch(value.get_secret_value()):
            raise ValueError("Use 16-256 characters: letters, digits, _ and -")
        return value
