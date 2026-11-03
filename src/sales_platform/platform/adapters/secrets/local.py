"""Local encrypted secret store for development and tests (D8: a stand-in for a cloud store).

Each secret is one file, <storage_dir>/secrets/<tenant_id>/<uuid>.bin, holding a random nonce
and the AES-256-GCM ciphertext. The tenant ID and reference are bound in as associated data, so
a file copied into another tenant's folder or renamed does not decrypt. Company-level secrets
(phase 5) are <storage_dir>/secrets/platform/<provider_key>/<name>.bin, bound the same way.
The key comes from SALES_SECRET_STORE_KEY (32 bytes, url-safe base64);
scripts/init_local_env.py creates it.
"""

import base64
import binascii
import os
import re
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sales_platform.platform.ports.secrets import (
    SecretNotFound,
    SecretStoreEnvironment,
    SecretStoreUnavailable,
)

PREFIX = "local:"
_NONCE_BYTES = 12
_PLATFORM_NAME = re.compile(r"^[a-z0-9_]{1,64}$")
_REFERENCE = re.compile(r"^local:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise ValueError("SALES_SECRET_STORE_KEY must be url-safe base64") from None
    if len(key) != 32:
        raise ValueError("SALES_SECRET_STORE_KEY must encode exactly 32 bytes")
    return key


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


class LocalSecretStore:
    def __init__(self, root: Path, key: bytes) -> None:
        self._root = root.resolve() / "secrets"
        self._aead = AESGCM(key)

    def __repr__(self) -> str:
        return "LocalSecretStore(<key hidden>)"

    def put(self, tenant_id: UUID, name: str, value: str) -> str:
        reference = f"{PREFIX}{uuid4()}"
        nonce = os.urandom(_NONCE_BYTES)
        sealed = nonce + self._aead.encrypt(
            nonce, value.encode("utf-8"), self._bound(tenant_id, reference)
        )
        folder = self._root / str(tenant_id)
        folder.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".secret-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(sealed)
            os.replace(temporary, self._path(tenant_id, reference))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return reference

    def get(self, tenant_id: UUID, reference: str) -> str:
        try:
            sealed = self._path(tenant_id, reference).read_bytes()
        except FileNotFoundError:
            raise SecretNotFound("Secret not found") from None
        nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
        try:
            plain = self._aead.decrypt(nonce, ciphertext, self._bound(tenant_id, reference))
        except InvalidTag:
            # Wrong key, tampered file, or a file moved from elsewhere: never return garbage.
            raise SecretNotFound("Secret not found") from None
        return plain.decode("utf-8")

    def delete(self, tenant_id: UUID, reference: str) -> None:
        self._path(tenant_id, reference).unlink(missing_ok=True)

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        path = self._platform_path(provider_key, name)
        nonce = os.urandom(_NONCE_BYTES)
        bound = f"platform:{provider_key}:{name}".encode()
        sealed = nonce + self._aead.encrypt(nonce, value.encode("utf-8"), bound)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".secret-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(sealed)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get_platform(self, provider_key: str, name: str) -> str:
        try:
            sealed = self._platform_path(provider_key, name).read_bytes()
        except FileNotFoundError:
            raise SecretNotFound("Secret not found") from None
        nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
        bound = f"platform:{provider_key}:{name}".encode()
        try:
            return self._aead.decrypt(nonce, ciphertext, bound).decode("utf-8")
        except InvalidTag:
            raise SecretNotFound("Secret not found") from None

    def _platform_path(self, provider_key: str, name: str) -> Path:
        if not _PLATFORM_NAME.fullmatch(provider_key) or not _PLATFORM_NAME.fullmatch(name):
            raise SecretNotFound("Secret not found")
        return self._root / "platform" / provider_key / f"{name}.bin"

    def _path(self, tenant_id: UUID, reference: str) -> Path:
        if not _REFERENCE.fullmatch(reference):
            raise SecretNotFound("Secret not found")
        return self._root / str(tenant_id) / f"{reference.removeprefix(PREFIX)}.bin"

    @staticmethod
    def _bound(tenant_id: UUID, reference: str) -> bytes:
        return f"{tenant_id}:{reference}".encode()


def build(environment: SecretStoreEnvironment) -> LocalSecretStore:
    """Entry point `local` in group sales_platform.secret_stores."""
    if environment.environment == "production":
        raise SecretStoreUnavailable("The local secret store is not allowed in production")
    if not environment.key:
        raise SecretStoreUnavailable(
            "SALES_SECRET_STORE_KEY is required for the local secret store; "
            "run `uv run python scripts/init_local_env.py --add-missing`"
        )
    return LocalSecretStore(environment.storage_dir, decode_key(environment.key))
