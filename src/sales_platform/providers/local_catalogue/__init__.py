"""Provider `local_catalogue`: the business's own product list, kept with us (phase 3, ADR 0020).

A product source only (purpose catalogue): staff type products, prices and stock into the
portal, and LocalCatalogue serves them to the quotation core through the ERP port. It has no
messages, webhooks or secrets. Another product source later (a spreadsheet sync, an ERP, a
shop) is another provider package with purpose catalogue that passes the same ERP contract
suite; the business switches its catalogue connection, with no core change.
"""

from collections.abc import Sequence
from typing import cast

from sales_platform.catalogue.adapters.catalogue.local import LocalCatalogue
from sales_platform.catalogue.ports.catalogue_admin import CatalogueReadStore, CatalogueUnavailable
from sales_platform.catalogue.ports.erp import ProductSource
from sales_platform.integrations.domain.connections import Capabilities, Connection, Purpose
from sales_platform.integrations.domain.events import InboundEvent
from sales_platform.integrations.ports.providers import (
    InboundRequest,
    OperationUnsupported,
    ProviderDefaults,
    ProviderEnvironment,
    RenderedMessage,
    SendReceipt,
    TestResult,
)
from sales_platform.platform.ports.secrets import SecretStore

PROVIDER_KEY = "local_catalogue"


class LocalCatalogueProvider(ProviderDefaults):
    key = PROVIDER_KEY
    display_name = "Our product list"  # user answer Q23
    purposes = frozenset({Purpose.CATALOGUE})
    capabilities = Capabilities(
        webhooks=False, text=False, outbound_initiation=False, catalogue_editable=True
    )
    dev_only = False

    def __init__(self, store: CatalogueReadStore | None) -> None:
        # Without a store (a process that has no database) the source cannot be read.
        self._reader = LocalCatalogue(store) if store is not None else None
        self._store = store

    def test(self, conn: Connection, secrets: SecretStore) -> TestResult:
        if self._store is None:
            return TestResult(ok=False, detail="The product list is not available here")
        try:
            self._store.category_names_in_use(conn.tenant_id)
        except CatalogueUnavailable:
            return TestResult(ok=False, detail="The product list could not be read")
        return TestResult(ok=True)

    def verify_inbound(
        self, conn: Connection, secrets: SecretStore, request: InboundRequest
    ) -> None:
        raise OperationUnsupported("The product list receives no callbacks")

    def parse_inbound(self, conn: Connection, request: InboundRequest) -> Sequence[InboundEvent]:
        raise OperationUnsupported("The product list receives no callbacks")

    def send(self, conn: Connection, secrets: SecretStore, message: RenderedMessage) -> SendReceipt:
        raise OperationUnsupported("The product list sends no messages")

    def catalogue_reader(self, conn: Connection, secrets: SecretStore) -> ProductSource:
        if Purpose.CATALOGUE not in conn.purposes or self._reader is None:
            raise OperationUnsupported("This connection has no product list")
        return self._reader


def build(environment: ProviderEnvironment) -> LocalCatalogueProvider:
    """Entry point `local_catalogue` in group sales_platform.providers."""
    return LocalCatalogueProvider(cast(CatalogueReadStore | None, environment.catalogue_store))


__all__ = ["PROVIDER_KEY", "LocalCatalogueProvider", "build"]
