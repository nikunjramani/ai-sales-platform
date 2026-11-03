"""Provider registry: loading by entry point, key checks and development-only providers."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from sales_platform.adapters.providers.api import ApiProvider
from sales_platform.adapters.providers.simulated import SimulatedProvider
from sales_platform.application.providers import ProviderRegistry, UnknownProvider
from sales_platform.domain.connections import Purpose
from sales_platform.ports.providers import Provider, ProviderEnvironment


@dataclass
class FakeEntry:
    name: str
    provider: Provider

    def load(self) -> object:
        return lambda environment: self.provider


def environment(name: str = "development") -> ProviderEnvironment:
    return ProviderEnvironment(name, Path("var/storage"))


def test_loads_providers_and_finds_them_by_key_and_purpose() -> None:
    entries = [
        FakeEntry("api", ApiProvider()),
        FakeEntry("simulated", SimulatedProvider(Path("."))),
    ]
    registry = ProviderRegistry.load(environment(), entries)
    assert [provider.key for provider in registry.all()] == ["api", "simulated"]
    assert registry.get("api").key == "api"
    assert [p.key for p in registry.for_purpose(Purpose.CATALOGUE)] == ["simulated"]
    with pytest.raises(UnknownProvider):
        registry.get("telegram")
    assert registry.find("telegram") is None


def test_development_only_providers_are_skipped_in_production() -> None:
    entries = [
        FakeEntry("api", ApiProvider()),
        FakeEntry("simulated", SimulatedProvider(Path("."))),
    ]
    registry = ProviderRegistry.load(environment("production"), entries)
    assert [provider.key for provider in registry.all()] == ["api"]


def test_key_must_match_the_entry_point_and_be_unique() -> None:
    with pytest.raises(ValueError, match="must match"):
        ProviderRegistry.load(environment(), [FakeEntry("email", ApiProvider())])
    with pytest.raises(ValueError, match="Two providers"):
        ProviderRegistry([ApiProvider(), ApiProvider()])


def test_the_installed_metadata_registers_both_phase_1_providers() -> None:
    registry = ProviderRegistry.load(environment())
    assert {"api", "simulated"} <= {provider.key for provider in registry.all()}
