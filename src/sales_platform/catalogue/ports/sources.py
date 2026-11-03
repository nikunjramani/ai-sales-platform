"""What a provider package offers to be a product source (ADR 0026: catalogue owns this
contract; the generic provider contract in integrations covers messaging and webhooks).

A provider is a product source when it has `catalogue_reader`. Callers ask through
product_source_of, which raises OperationUnsupported for a tool that is not one.
"""

from typing import Protocol, runtime_checkable

from sales_platform.catalogue.ports.erp import ProductSource
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.ports.providers import OperationUnsupported
from sales_platform.platform.ports.secrets import SecretStore


@runtime_checkable
class ProductSourceProvider(Protocol):
    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ProductSource:
        """The product source behind a connection with Purpose.CATALOGUE."""
        ...


def product_source_of(provider: object) -> ProductSourceProvider:
    if not isinstance(provider, ProductSourceProvider):
        raise OperationUnsupported("This tool is not a product source")
    return provider
