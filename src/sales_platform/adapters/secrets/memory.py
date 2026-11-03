"""In-memory secret store for tests and evaluation scripts (never registered as an entry point).

Same contract as the local store: secrets belong to one tenant, references are opaque, and a
missing or foreign reference raises SecretNotFound.
"""

from uuid import UUID, uuid4

from sales_platform.ports.secrets import SecretNotFound


class InMemorySecretStore:
    def __init__(self) -> None:
        self._values: dict[tuple[UUID, str], str] = {}
        self._platform: dict[tuple[str, str], str] = {}

    def put(self, tenant_id: UUID, name: str, value: str) -> str:
        reference = f"memory:{uuid4()}"
        self._values[(tenant_id, reference)] = value
        return reference

    def get(self, tenant_id: UUID, reference: str) -> str:
        try:
            return self._values[(tenant_id, reference)]
        except KeyError:
            raise SecretNotFound("Secret not found") from None

    def delete(self, tenant_id: UUID, reference: str) -> None:
        self._values.pop((tenant_id, reference), None)

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        self._platform[(provider_key, name)] = value

    def get_platform(self, provider_key: str, name: str) -> str:
        try:
            return self._platform[(provider_key, name)]
        except KeyError:
            raise SecretNotFound("Secret not found") from None
