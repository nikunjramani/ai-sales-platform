"""No secret store: every call fails clearly.

The production default until hosting and its cloud secret manager are chosen (D24). Only
connections that need no secrets (such as the API) work with it.
"""

from uuid import UUID

from sales_platform.platform.ports.secrets import SecretStoreEnvironment, SecretStoreUnavailable

_MESSAGE = "No secret store is configured (see D24)"


class NoSecretStore:
    def put(self, tenant_id: UUID, name: str, value: str) -> str:
        raise SecretStoreUnavailable(_MESSAGE)

    def get(self, tenant_id: UUID, reference: str) -> str:
        raise SecretStoreUnavailable(_MESSAGE)

    def delete(self, tenant_id: UUID, reference: str) -> None:
        raise SecretStoreUnavailable(_MESSAGE)

    def put_platform(self, provider_key: str, name: str, value: str) -> None:
        raise SecretStoreUnavailable(_MESSAGE)

    def get_platform(self, provider_key: str, name: str) -> str:
        raise SecretStoreUnavailable(_MESSAGE)


def build(environment: SecretStoreEnvironment) -> NoSecretStore:
    """Entry point `none` in group sales_platform.secret_stores."""
    return NoSecretStore()
