"""The provider registry: every installed tool package, found through entry points (ADR 0018).

At start-up the API and the worker call ProviderRegistry.load. Each entry point in the group
`sales_platform.providers` names a factory; the registry builds the provider and checks that its
key matches the entry point name. Development-only providers (the simulated phone) are skipped
in production. The core then looks providers up by the key stored on each connection.
"""

import logging
from collections.abc import Iterable
from typing import cast

from sales_platform.integrations.domain.connections import Purpose
from sales_platform.integrations.ports.providers import (
    Provider,
    ProviderEnvironment,
    ProviderFactory,
)
from sales_platform.platform.application.plugins import EntryPointLike, group_entries

PROVIDERS = "sales_platform.providers"
logger = logging.getLogger(__name__)


class UnknownProvider(LookupError):
    """No installed provider has this key (or it is development-only in production)."""


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider]) -> None:
        self._providers: dict[str, Provider] = {}
        for provider in providers:
            if provider.key in self._providers:
                raise ValueError(f"Two providers use the key '{provider.key}'")
            self._providers[provider.key] = provider

    @classmethod
    def load(
        cls, environment: ProviderEnvironment, entries: Iterable[EntryPointLike] | None = None
    ) -> "ProviderRegistry":
        providers: list[Provider] = []
        for entry in group_entries(PROVIDERS, entries):
            factory = cast(ProviderFactory, entry.load())
            provider = factory(environment)
            if provider.key != entry.name:
                raise ValueError(
                    f"Entry point '{entry.name}' builds provider '{provider.key}'; they must match"
                )
            if provider.dev_only and environment.environment == "production":
                logger.info("Skipping development-only provider %s in production", provider.key)
                continue
            providers.append(provider)
        if not providers:
            logger.warning("No providers are registered; run `uv sync` to register them")
        return cls(providers)

    def get(self, key: str) -> Provider:
        try:
            return self._providers[key]
        except KeyError:
            raise UnknownProvider(f"Provider '{key}' is not installed") from None

    def find(self, key: str) -> Provider | None:
        return self._providers.get(key)

    def all(self) -> list[Provider]:
        return sorted(self._providers.values(), key=lambda provider: provider.key)

    def for_purpose(self, purpose: Purpose) -> list[Provider]:
        return [provider for provider in self.all() if purpose in provider.purposes]
