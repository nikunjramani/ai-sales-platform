"""Create local-only credentials without printing them or changing existing values.

Default: create .env, refusing to overwrite it. With --add-missing: append only the settings an
older .env lacks (e.g. the phase 1 secret store key or the phase 2 demo password), leaving existing
lines as is.
"""

import argparse
from base64 import urlsafe_b64encode
from pathlib import Path
from secrets import token_bytes, token_hex

DATABASE = "@127.0.0.1:55432/quotation_dev"


def _read_existing(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _wanted(existing: dict[str, str]) -> dict[str, str]:
    """All settings, reusing existing passwords so URLs stay consistent with them."""

    def secret(key: str) -> str:
        return existing.get(key) or token_hex(24)

    owner = secret("POSTGRES_OWNER_PASSWORD")
    app = secret("POSTGRES_APP_PASSWORD")
    worker = secret("POSTGRES_WORKER_PASSWORD")
    return {
        "POSTGRES_PASSWORD": secret("POSTGRES_PASSWORD"),
        "POSTGRES_OWNER_PASSWORD": owner,
        "POSTGRES_APP_PASSWORD": app,
        "POSTGRES_WORKER_PASSWORD": worker,
        "SALES_ENVIRONMENT": "development",
        "SALES_DATABASE_URL": f"postgresql+psycopg://quotation_app:{app}{DATABASE}",
        "SALES_MIGRATION_DATABASE_URL": f"postgresql+psycopg://quotation_owner:{owner}{DATABASE}",
        "SALES_WORKER_DATABASE_URL": f"postgresql+psycopg://quotation_worker:{worker}{DATABASE}",
        # Signs simulated manager-channel events (M4); development only.
        "SALES_SIMULATED_CHANNEL_SECRET": secret("SALES_SIMULATED_CHANNEL_SECRET"),
        # Encrypts provider secrets in the local secret store (phase 1, ADR 0018); 32 bytes.
        "SALES_SECRET_STORE_KEY": existing.get("SALES_SECRET_STORE_KEY")
        or urlsafe_b64encode(token_bytes(32)).decode("ascii"),
        # Password of the seeded demo people (phase 2, Q24); development only, never in Git.
        "SALES_DEMO_PASSWORD": secret("SALES_DEMO_PASSWORD"),
        "SALES_CHAT_IP_KEY": secret("SALES_CHAT_IP_KEY"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add-missing", action="store_true", help="append settings .env lacks")
    arguments = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / ".env"

    if arguments.add_missing and destination.exists():
        existing = _read_existing(destination)
        missing = {k: v for k, v in _wanted(existing).items() if k not in existing}
        if missing:
            with destination.open("a", encoding="utf-8") as stream:
                stream.writelines(f"{key}={value}\n" for key, value in missing.items())
        print(f"Added {len(missing)} missing setting(s): {', '.join(missing) or 'none'}.")
        return

    content = "# Generated local development credentials. Never commit this file.\n" + "".join(
        f"{key}={value}\n" for key, value in _wanted({}).items()
    )
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except FileExistsError:
        raise SystemExit(".env already exists; left unchanged. Use --add-missing.") from None
    print("Created ignored .env for local PostgreSQL. No credentials printed.")


if __name__ == "__main__":
    main()
