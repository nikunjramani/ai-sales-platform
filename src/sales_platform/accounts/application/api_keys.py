"""API keys: service-account credentials scoped to one tenant and one role.

Token format: qk_<key id as 32 hex>_<secret>. Only a SHA-256 hash of the secret is stored; the
secret is shown once at creation. A 256-bit random secret makes a fast hash appropriate.
"""

import hmac
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from sales_platform.accounts.ports.api_keys import ApiKeyReader
from sales_platform.platform.domain.security import AuthenticationFailed, hash_secret
from sales_platform.platform.domain.tenancy import TenantContext

PREFIX = "qk"


@dataclass(frozen=True)
class NewApiKey:
    key_id: UUID
    token: str
    secret_sha256: str


def generate_api_key() -> NewApiKey:
    key_id = uuid4()
    secret = secrets.token_urlsafe(32)
    return NewApiKey(key_id, f"{PREFIX}_{key_id.hex}_{secret}", hash_secret(secret))


def parse_token(token: str) -> tuple[UUID, str]:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != PREFIX or len(parts[1]) != 32 or not parts[2]:
        raise AuthenticationFailed("Invalid credentials")
    try:
        return UUID(hex=parts[1]), parts[2]
    except ValueError:
        raise AuthenticationFailed("Invalid credentials") from None


class ApiKeyAuthenticator:
    def __init__(self, keys: ApiKeyReader) -> None:
        self._keys = keys

    def authenticate(self, token: str) -> TenantContext:
        key_id, secret = parse_token(token)
        record = self._keys.find(key_id)
        # compare_digest takes the same time whether or not the hashes match.
        supplied = hash_secret(secret)
        expected = record.secret_sha256 if record is not None else "0" * 64
        if record is None or record.revoked or not hmac.compare_digest(supplied, expected):
            raise AuthenticationFailed("Invalid credentials")
        return TenantContext(record.tenant_id, f"api-key:{record.key_id}", frozenset({record.role}))
