"""Moved here by ADR 0026 (module order)."""

import hashlib


class AuthenticationFailed(Exception):
    """Missing, malformed, unknown, revoked or wrong credentials. One message for all cases."""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()
