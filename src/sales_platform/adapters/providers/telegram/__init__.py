"""Provider package `telegram` (phase 5): see provider.py."""

from sales_platform.adapters.providers.telegram.provider import TelegramProvider, build

__all__ = ["TelegramProvider", "build"]
