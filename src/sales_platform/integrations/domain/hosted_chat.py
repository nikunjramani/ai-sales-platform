"""Moved here by ADR 0026 (module order)."""

import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import urlsplit

HANDLE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")


MAX_ALLOWED_ORIGINS = 10


MAX_GREETING = 300


def handle_from_name(name: str) -> str:
    """'Acme Industrial Pvt. Ltd.' -> 'acme-industrial-pvt-ltd' (3-64 characters)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:56].strip("-")
    return slug if len(slug) >= 3 else f"chat-{slug}".strip("-")


def validate_handle(handle: str) -> str:
    if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
        raise ValueError(
            "A chat address is 3-64 lower-case letters, digits and hyphens, "
            "starting and ending with a letter or digit"
        )
    return handle


def validate_origins(raw: Sequence[str]) -> list[str]:
    if len(raw) > MAX_ALLOWED_ORIGINS:
        raise ValueError(f"At most {MAX_ALLOWED_ORIGINS} websites")
    return list(dict.fromkeys(normalise_origin(item) for item in raw))


def normalise_origin(raw: str) -> str:
    """'https://www.Example.com/' -> 'https://www.example.com' (scheme, host, port only)."""
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.path not in ("", "/"):
        raise ValueError("A website is written like https://www.example.com")
    if parts.query or parts.fragment or parts.username:
        raise ValueError("A website is written like https://www.example.com")
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"
