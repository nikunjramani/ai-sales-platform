"""Secret store contract: provider credentials live here, never in the database (D8).

The database keeps only opaque references such as "local:<uuid>". Each secret belongs to one
tenant; asking for it with another tenant's ID fails as if it did not exist. Implementations
are chosen by the entry point group `sales_platform.secret_stores` (local for development,
none until a cloud store is chosen, D24).

Company-level secrets (our own bot token, app secret, master account...) live in a separate
platform namespace per provider key (phase 5, contracts C5). They are written once by an
operator command, never through the business portal, and are named rather than referenced.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID


class SecretNotFound(Exception):
    """No such secret for this tenant (missing, deleted, or another tenant's)."""


class SecretStoreUnavailable(Exception):
    """No secret store is configured, or it cannot be reached."""


class SecretStore(Protocol):
    def put(self, tenant_id: UUID, name: str, value: str) -> str:
        """Store a secret and return its opaque reference."""
        ...

    def get(self, tenant_id: UUID, reference: str) -> str: ...

    def delete(self, tenant_id: UUID, reference: str) -> None:
        """Remove a secret; deleting a missing one is not an error."""
        ...

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        """Store (or replace) a company-level secret of one provider."""
        ...

    def get_platform(self, provider_key: str, name: str) -> str:
        """Raises SecretNotFound when the operator has not set it."""
        ...


@dataclass(frozen=True)
class SecretStoreEnvironment:
    """What a secret store factory receives (from generic settings, never provider-named)."""

    environment: str
    storage_dir: Path
    key: str | None  # SALES_SECRET_STORE_KEY, for stores that encrypt locally
    options: Mapping[str, object] = field(default_factory=dict)


type SecretStoreFactory = Callable[[SecretStoreEnvironment], SecretStore]
