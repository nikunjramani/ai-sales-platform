"""Operator commands for company-level provider settings (phase 5, contracts C5).

  uv run python -m sales_platform.ops platform-config set <provider_key> --file settings.json
  uv run python -m sales_platform.ops platform-config set <provider_key>      (JSON on stdin)
  uv run python -m sales_platform.ops platform-config show <provider_key>     (secrets masked)
  uv run python -m sales_platform.ops platform-setup <provider_key>

Settings are checked against the provider's own platform_config_model and stored in the
secret store's platform namespace (name "config"), never in .env or the business portal.
platform-setup runs the provider's one-time company setup (e.g. registering our platform
webhook URL, SALES_PUBLIC_BASE_URL, with the tool). Nothing here names a provider.
"""

import argparse
import json
import sys
from typing import Any

from pydantic import BaseModel, SecretStr, ValidationError

from sales_platform.composition import build_runtime
from sales_platform.integrations.ports.providers import OperationUnsupported, Provider
from sales_platform.platform.config import Settings
from sales_platform.platform.ports.secrets import SecretNotFound

CONFIG = "config"


def _provider(key: str) -> Provider:
    provider = build_runtime(Settings()).registry.find(key)
    if provider is None:
        raise SystemExit(f"No installed provider '{key}'")
    return provider


def _model(provider: Provider) -> type[BaseModel]:
    model = provider.platform_config_model
    if model is None or not issubclass(model, BaseModel):
        raise SystemExit(f"Provider '{provider.key}' has no company-level settings")
    return model


def _masked(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: "********" if isinstance(value, SecretStr) else value
        for name, value in values.items()
    }


def set_config(key: str, raw: str) -> str:
    provider = _provider(key)
    model = _model(provider)
    try:
        data = json.loads(raw)
        parsed = model.model_validate(data)
    except (ValueError, ValidationError) as error:
        raise SystemExit(f"Invalid settings for '{key}': {error}") from None
    runtime = build_runtime(Settings())
    runtime.secrets.put_platform(key, CONFIG, json.dumps(data))  # validated, stored as given
    return json.dumps(_masked(dict(parsed)), indent=2)


def show_config(key: str) -> str:
    provider = _provider(key)
    model = _model(provider)
    try:
        raw = build_runtime(Settings()).secrets.get_platform(key, CONFIG)
    except SecretNotFound:
        return f"'{key}' has no company-level settings yet"
    return json.dumps(_masked(dict(model.model_validate_json(raw))), indent=2)


def setup(key: str) -> str:
    settings = Settings()
    if not settings.public_base_url:
        raise SystemExit("Set SALES_PUBLIC_BASE_URL (our public HTTPS address) first")
    runtime = build_runtime(settings)
    provider = runtime.registry.find(key)
    if provider is None:
        raise SystemExit(f"No installed provider '{key}'")
    try:
        return provider.platform_setup(runtime.secrets, settings.public_base_url)
    except OperationUnsupported as error:
        raise SystemExit(str(error)) from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m sales_platform.ops", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("platform-config", help="company-level provider settings")
    config.add_argument("action", choices=("set", "show"))
    config.add_argument("provider_key")
    config.add_argument("--file", help="JSON file with the settings (default: stdin)")
    run = commands.add_parser("platform-setup", help="one-time company setup with the tool")
    run.add_argument("provider_key")
    arguments = parser.parse_args(argv)
    if arguments.command == "platform-setup":
        print(setup(arguments.provider_key))
    elif arguments.action == "show":
        print(show_config(arguments.provider_key))
    else:
        if arguments.file:
            with open(arguments.file, encoding="utf-8") as stream:
                raw = stream.read()
        else:
            raw = sys.stdin.read()
        print(set_config(arguments.provider_key, raw))


if __name__ == "__main__":
    main()
