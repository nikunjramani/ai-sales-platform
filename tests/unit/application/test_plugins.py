"""Loading pluggable parts by entry point name."""

from dataclasses import dataclass

import pytest

from sales_platform.application.plugins import PluginNotFound, load_named


@dataclass
class FakeEntry:
    name: str
    target: object

    def load(self) -> object:
        return self.target


def test_loads_by_name_and_explains_missing_or_duplicate() -> None:
    entries = [FakeEntry("local", "L"), FakeEntry("none", "N")]
    assert load_named("group", "none", entries) == "N"
    with pytest.raises(PluginNotFound, match="uv sync"):
        load_named("group", "cloud", entries)
    with pytest.raises(PluginNotFound, match="Two packages"):
        load_named("group", "local", [*entries, FakeEntry("local", "X")])
