"""HMAC signatures of the simulated phone's callbacks, the way webhook providers sign them."""

import hashlib
import hmac
from uuid import UUID

SIGNATURE_HEADER = "X-Simulated-Signature"


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_valid(secret: str, body: bytes, signature: str | None) -> bool:
    return signature is not None and hmac.compare_digest(sign(secret, body), signature)


def hook_token(secret: str, connection_id: UUID) -> str:
    """The demo connection's webhook token, derived from the development secret.

    Development tools can compute the hook URL from .env without reading the database; only
    the token's SHA-256 is stored.
    """
    return sign(secret, f"hook:{connection_id}".encode())
