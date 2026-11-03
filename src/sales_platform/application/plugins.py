"""Find pluggable parts (providers, secret stores) by name through Python entry points.

An entry point is a line in a package's metadata ("name = module:function") that installed
packages publish under a group name. Loading by group means a new provider is installed, not
coded into the core. The metadata comes from the installed package, so after changing
pyproject.toml run `uv sync` once.
"""

from collections.abc import Iterable
from importlib.metadata import entry_points
from typing import Protocol


class EntryPointLike(Protocol):
    name: str

    def load(self) -> object: ...


class PluginNotFound(LookupError):
    pass


def group_entries(
    group: str, entries: Iterable[EntryPointLike] | None = None
) -> list[EntryPointLike]:
    """The group's entry points, or the given ones (tests pass fakes)."""
    return list(entries) if entries is not None else list(entry_points(group=group))


def load_named(group: str, name: str, entries: Iterable[EntryPointLike] | None = None) -> object:
    matches = [entry for entry in group_entries(group, entries) if entry.name == name]
    if not matches:
        raise PluginNotFound(
            f"No '{name}' is installed in entry point group {group}; "
            "check the setting, or run `uv sync` to register the packages"
        )
    if len(matches) > 1:
        raise PluginNotFound(f"Two packages register '{name}' in {group}")
    return matches[0].load()
