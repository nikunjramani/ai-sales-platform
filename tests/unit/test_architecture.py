"""Import-boundary checks: business modules in a fixed order, layers inside each module, and
frameworks and providers kept at the edges (ADR 0001, 0018, 0026)."""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "sales_platform"

# ADR 0026: a module may import only the modules before it in this order (never a later one),
# so no two modules depend on each other.
MODULE_ORDER = (
    "platform",
    "integrations",
    "accounts",
    "catalogue",
    "conversations",
    "enquiries",
    "quotations",
    "orders",
    "customer_channels",
)
# Plug-in packages: they may use any module; only the wiring (by entry point) loads them.
PROVIDERS = "providers"
# The wiring builds the application and may import everything.
WIRING = {"__init__.py", "composition.py", "worker.py", "api_app.py", "seed.py", "ops.py"}

# Inside a module (and towards the same layer of an earlier module): which layers a layer uses.
ALLOWED = {
    "domain": {"domain"},
    "ports": {"domain", "ports"},
    "application": {"domain", "ports", "application"},
    "adapters": {"domain", "ports", "adapters"},
    "workflows": {"domain", "ports", "application", "workflows"},
    "persistence": {"domain", "ports", "persistence"},
    "api": {"domain", "ports", "application", "api"},
}
# Named exceptions, each with its reason: (importing file, imported module).
EXCEPTIONS = {
    ("accounts/persistence/api_keys.py", "sales_platform.providers.api"): "API keys are the "
    "api provider's accounts (see PROVIDER_NAME_EXCEPTIONS)",
    ("customer_channels/api/enquiries.py", "sales_platform.providers.api"): "the API "
    "provider's own routes",
}
# Layer -> third-party packages it may import. Domain, ports and application use only stdlib,
# so business rules never depend on a framework, database or AI provider.
THIRD_PARTY = {
    "adapters": {
        "cryptography",
        "pypdf",
        "reportlab",
        "langchain",
        "langchain_core",
        "pydantic",
        "argon2",  # phase 2: password hashing
        "PIL",  # phase 2: logo checks and re-encoding
    },
    "workflows": {"langgraph", "langchain_core"},
    "persistence": {"sqlalchemy", "psycopg", "langgraph"},  # langgraph: the checkpoint store
    "api": {"fastapi", "pydantic", "starlette", "sqlalchemy"},  # sqlalchemy: error mapping only
}
PROVIDER_THIRD_PARTY = {"httpx", "pydantic", "cryptography"}
# Modules that are not a layer folder but sit directly in a module.
MODULE_FILES = {"platform/config.py": "domain"}  # settings: pydantic, used by every layer


def _imports(path: Path) -> list[tuple[str, int]]:
    names: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append((node.module, node.lineno))
    return names


def _where(parts: list[str]) -> tuple[str, str | None]:
    """("quotations", "domain") for sales_platform.quotations.domain.x; layer None if unknown."""
    module = parts[0] if parts else ""
    layer = parts[1] if len(parts) > 1 else None
    return module, layer


def _files() -> list[Path]:
    return sorted(path for path in PACKAGE.rglob("*.py") if "__pycache__" not in path.parts)


def violations() -> list[str]:
    problems: list[str] = []
    for path in _files():
        relative = path.relative_to(PACKAGE).as_posix()
        parts = relative.removesuffix(".py").split("/")
        if relative in WIRING:
            continue
        module = parts[0]
        if module == PROVIDERS:
            for name, line in _imports(path):
                top = name.split(".")[0]
                if top not in {"sales_platform", *sys.stdlib_module_names, *PROVIDER_THIRD_PARTY}:
                    problems.append(f"{relative}:{line} imports {name}")
            continue
        assert module in MODULE_ORDER, f"{relative} is outside every module"
        layer = MODULE_FILES.get(relative) or (parts[1] if len(parts) > 2 else None)
        if layer is None or (layer not in ALLOWED and relative not in MODULE_FILES):
            if path.name != "__init__.py":
                problems.append(f"{relative} is not inside a layer folder")
            continue
        rank = MODULE_ORDER.index(module)
        for name, line in _imports(path):
            if (relative, name) in EXCEPTIONS:
                continue
            dotted = name.split(".")
            if dotted[0] != "sales_platform":
                allowed = dotted[0] in sys.stdlib_module_names or dotted[0] in THIRD_PARTY.get(
                    layer, set()
                ) | (
                    {"pydantic", "pydantic_settings", "sqlalchemy"}  # settings parse DB URLs
                    if relative in MODULE_FILES
                    else set()
                )
                if not allowed:
                    problems.append(f"{relative}:{line} imports {name}")
                continue
            target_layer: str | None
            if f"{'/'.join(dotted[1:])}.py" in MODULE_FILES:
                target_module, target_layer = dotted[1], MODULE_FILES[f"{'/'.join(dotted[1:])}.py"]
            else:
                target_module, target_layer = _where(dotted[1:])
            if target_module == PROVIDERS:
                problems.append(f"{relative}:{line} imports a provider package ({name})")
                continue
            if target_module not in MODULE_ORDER:
                problems.append(f"{relative}:{line} imports the wiring ({name})")
                continue
            if MODULE_ORDER.index(target_module) > rank:
                problems.append(f"{relative}:{line} ({module}) imports later module {name}")
            elif target_layer not in ALLOWED[layer] and not (
                # A store with one implementation and no fake needs no port (user, 2026-11-03).
                layer == "application" and target_layer == "persistence" and target_module == module
            ):
                problems.append(f"{relative}:{line} ({layer}) must not import {name}")
    return problems


def test_modules_and_layers_respect_their_order() -> None:
    assert violations() == []


def test_only_the_wiring_builds_stores() -> None:
    """SQL stores are built only by the wiring (ADR 0026 step 1), so the API and the worker
    share one construction of every service and cannot drift apart."""
    found = []
    for path in _files():
        relative = path.relative_to(PACKAGE).as_posix()
        if relative in WIRING or "/persistence/" in f"/{relative}":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id.startswith("Sql")
            ):
                found.append(f"{relative}:{node.lineno} builds {node.func.id}")
    assert found == []


def test_http_clients_stay_inside_provider_packages() -> None:
    """A tool's API is called only from its own package (phase 5)."""
    outside = [
        path.relative_to(PACKAGE).as_posix()
        for path in _files()
        if any(name.split(".")[0] == "httpx" for name, _ in _imports(path))
        and path.relative_to(PACKAGE).parts[0] != PROVIDERS
    ]
    assert outside == []


# Provider names may appear in core code only here (ADR 0018): everything else decides by
# purposes and capabilities, so adding a provider needs no core change.
PROVIDER_NAME_EXCEPTIONS = {
    "customer_channels/api/enquiries.py": "the API provider's own routes",
    "accounts/persistence/api_keys.py": "API keys are the api provider's accounts",
    "seed.py": "synthetic demo data for development",
    "platform/config.py": "the default product source setting (SALES_DEFAULT_CATALOGUE_PROVIDER)",
}


def test_core_code_never_names_a_provider() -> None:
    from importlib.metadata import entry_points

    keys = {entry.name for entry in entry_points(group="sales_platform.providers")}
    assert {"api", "simulated"} <= keys, "run `uv sync` to register the providers"
    found = []
    for path in _files():
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith(f"{PROVIDERS}/") or relative in PROVIDER_NAME_EXCEPTIONS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in keys:
                found.append(f"{relative}:{node.lineno} names provider {node.value!r}")
    assert found == []


@pytest.mark.parametrize("module", MODULE_ORDER)
def test_every_module_exists(module: str) -> None:
    assert (PACKAGE / module).is_dir()
