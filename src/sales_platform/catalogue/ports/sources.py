"""What a provider package offers to be a product source (ADR 0026: catalogue owns this
contract; the generic provider contract in integrations covers messaging and webhooks).

A provider is a product source when it has `catalogue_reader`, and copies its catalogue when
it has `fetch_catalogue` (capabilities.catalogue.sync). Callers ask through the helpers below,
which raise OperationUnsupported for a tool that does not offer it.
"""

from typing import Protocol, runtime_checkable

from sales_platform.catalogue.domain.catalogue_sync import CatalogueFeed
from sales_platform.catalogue.ports.erp import ProductSource
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.ports.providers import OperationUnsupported
from sales_platform.platform.ports.secrets import SecretStore


@runtime_checkable
class ProductSourceProvider(Protocol):
    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ProductSource:
        """The product source behind a connection with Purpose.CATALOGUE."""
        ...


@runtime_checkable
class CatalogueFeedProvider(Protocol):
    def fetch_catalogue(self, conn: Connection, secrets: SecretStore) -> CatalogueFeed:
        """The source's whole current catalogue in our words (capabilities.catalogue.sync).
        Raises ErpError; never a partial feed presented as complete."""
        ...


def product_source_of(provider: object) -> ProductSourceProvider:
    if not isinstance(provider, ProductSourceProvider):
        raise OperationUnsupported("This tool is not a product source")
    return provider


def catalogue_feed_of(provider: object) -> CatalogueFeedProvider:
    if not isinstance(provider, CatalogueFeedProvider):
        raise OperationUnsupported("This tool does not copy its catalogue")
    return provider
