"""Contract for looking up API keys (service accounts) by their public ID."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sales_platform.domain.tenancy import Role


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: UUID
    tenant_id: UUID
    role: Role
    secret_sha256: str
    revoked: bool


class ApiKeyReader(Protocol):
    def find(self, key_id: UUID) -> ApiKeyRecord | None: ...
