"""What a provider package offers to take orders (ADR 0026: orders owns this contract)."""

from typing import Protocol, runtime_checkable

from sales_platform.catalogue.ports.erp import OrderWriter
from sales_platform.integrations.domain.connections import Connection
from sales_platform.integrations.ports.providers import OperationUnsupported
from sales_platform.platform.ports.secrets import SecretStore


@runtime_checkable
class OrderWriterProvider(Protocol):
    def order_writer(self, conn: Connection, secrets: SecretStore) -> OrderWriter:
        """Where the orders of a connection with Purpose.ORDERS are written. Raises
        OperationUnsupported when this connection does not write orders (then orders stay
        with us, fulfilment local)."""
        ...


def order_writer_of(provider: object) -> OrderWriterProvider:
    if not isinstance(provider, OrderWriterProvider):
        raise OperationUnsupported("This tool does not take orders")
    return provider
