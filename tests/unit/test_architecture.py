"""Import-boundary checks so business rules stay independent of frameworks and providers."""

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "sales_platform"

# Layer -> internal layers it may import.
ALLOWED = {
    "domain": {"domain"},
    "ports": {"domain", "ports"},
    "application": {"domain", "ports", "application"},
    "adapters": {"domain", "ports", "adapters"},
    "workflows": {"domain", "ports", "application", "workflows"},
    # ADR 0026 step 1: the database and HTTP layers are checked too.
    "persistence": {"domain", "ports", "persistence"},
    "api": {"domain", "ports", "application", "api"},
}
# The wiring files may import every layer (they build the application). Everything else that
# wires objects together lives in composition.py and worker.py, outside the layers.
COMPOSITION_ROOTS = {
    "api/app.py": "the API's wiring",
    "persistence/seed.py": "development demo data (uses the demo catalogue and provider)",
}
# Named exceptions to the layer rules, each with its reason.
LAYER_EXCEPTIONS = {
    ("persistence/api_keys.py", "sales_platform.adapters.providers.api"): "API keys are the api "
    "provider's accounts (see PROVIDER_NAME_EXCEPTIONS)",
    ("api/enquiries.py", "sales_platform.adapters.providers.api"): "the API provider's own routes",
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
        "httpx",  # phase 5: provider packages only (see the test below)
    },
    "workflows": {"langgraph", "langchain_core"},
    "persistence": {"sqlalchemy", "psycopg", "langgraph"},  # langgraph: the checkpoint store
    "api": {"fastapi", "pydantic", "starlette", "sqlalchemy"},  # sqlalchemy: error mapping only
}


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_imports_respect_boundaries(layer: str) -> None:
    files = sorted((PACKAGE / layer).rglob("*.py"))
    assert files, f"no source files found for {layer}"
    violations = []
    for path in files:
        relative = path.relative_to(PACKAGE).as_posix()
        if relative in COMPOSITION_ROOTS:
            continue
        for name in _imports(path):
            if (relative, name) in LAYER_EXCEPTIONS:
                continue
            parts = name.split(".")
            if parts[0] == "sales_platform":
                allowed = len(parts) > 1 and parts[1] in ALLOWED[layer]
            else:
                allowed = parts[0] in sys.stdlib_module_names or parts[0] in THIRD_PARTY.get(
                    layer, set()
                )
            if not allowed:
                violations.append(f"{path.relative_to(PACKAGE)} imports {name}")
    assert violations == []


def test_only_the_wiring_builds_stores() -> None:
    """SQL stores are built only by the wiring (ADR 0026 step 1), so the API and the worker
    share one construction of every service and cannot drift apart."""
    wiring = {"composition.py", "worker.py", *COMPOSITION_ROOTS}
    violations = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative in wiring or relative.startswith("persistence/"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id.startswith("Sql")
            ):
                violations.append(f"{relative}:{node.lineno} builds {node.func.id}")
    assert violations == []


def test_http_clients_stay_inside_provider_packages() -> None:
    """A tool's API is called only from its own package (phase 5)."""
    outside = [
        str(path.relative_to(PACKAGE))
        for path in sorted(PACKAGE.rglob("*.py"))
        if any(name.split(".")[0] == "httpx" for name in _imports(path))
        and "providers" not in path.relative_to(PACKAGE).parts[:2]
    ]
    assert outside == []


# Provider names may appear in core code only here (ADR 0018): everything else decides by
# purposes and capabilities, so adding a provider needs no core change.
PROVIDER_NAME_EXCEPTIONS = {
    "api/enquiries.py": "the API provider's own routes",
    "persistence/api_keys.py": "API keys are the api provider's accounts",
    "persistence/seed.py": "synthetic demo data for development",
    "config.py": "the default product source setting (SALES_DEFAULT_CATALOGUE_PROVIDER)",
}


def test_core_code_never_names_a_provider() -> None:
    from importlib.metadata import entry_points

    keys = {entry.name for entry in entry_points(group="sales_platform.providers")}
    assert {"api", "simulated"} <= keys, "run `uv sync` to register the providers"
    violations = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith("adapters/providers/") or relative in PROVIDER_NAME_EXCEPTIONS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value in keys:
                violations.append(f"{relative}:{node.lineno} names provider {node.value!r}")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("sales_platform.adapters.providers.")
            ):
                violations.append(f"{relative}:{node.lineno} imports {node.module}")
    assert violations == []
