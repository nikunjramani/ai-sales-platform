"""Per-business product sources: resolution, isolation, caching and the missing-source error."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from sales_platform.catalogue.application.catalogue_sources import (
    CatalogueSourceResolver,
    TenantCatalogue,
)
from sales_platform.catalogue.ports.sources import CatalogueSourceMissing
from sales_platform.integrations.application.providers import ProviderRegistry
from sales_platform.integrations.domain.connections import Connection, ConnectionStatus, Purpose
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.providers.local_catalogue import LocalCatalogueProvider
from sales_platform.seed import DEMO_CATALOGUES
from tests.support.catalogue_store import InMemoryCatalogueStore

NOW = datetime(2026, 10, 27, tzinfo=UTC)
A = UUID("00000000-0000-4000-8000-00000000000a")
B = UUID("00000000-0000-4000-8000-00000000000b")


def source(tenant_id: UUID, name: str, provider: str = "local_catalogue") -> Connection:
    return Connection(
        uuid4(), tenant_id, provider, "Demo", frozenset({Purpose.CATALOGUE}),
        {"catalogue": name}, {}, f"account-{name}", ConnectionStatus.ACTIVE, None, "test",
        NOW, NOW,
    )  # fmt: skip


class Sources:
    def __init__(self) -> None:
        self.rows: dict[UUID, list[Connection]] = {}
        self.reads = 0

    def catalogue_sources(self, tenant_id: UUID) -> list[Connection]:
        self.reads += 1
        return [row for row in self.rows.get(tenant_id, []) if row.active]


def context(tenant_id: UUID) -> TenantContext:
    return TenantContext(tenant_id, "system", frozenset({Role.VIEWER}))


@pytest.fixture
def setup() -> tuple[Sources, list[datetime], TenantCatalogue]:
    sources, now = Sources(), [NOW]
    resolver = CatalogueSourceResolver(
        sources,  # type: ignore[arg-type]
        ProviderRegistry(
            [
                LocalCatalogueProvider(
                    InMemoryCatalogueStore.from_demo(
                        {A: DEMO_CATALOGUES["industrial"], B: DEMO_CATALOGUES["office"]}
                    )
                )
            ]
        ),
        None,  # type: ignore[arg-type]
        ttl=timedelta(seconds=30),
        clock=lambda: now[0],
    )
    return sources, now, TenantCatalogue(resolver)


def test_each_business_reads_its_own_source(
    setup: tuple[Sources, list[datetime], TenantCatalogue],
) -> None:
    sources, _, catalogue = setup
    sources.rows = {A: [source(A, "industrial")], B: [source(B, "office")]}
    assert catalogue.get_products(context(A), ["IND-FAN-450"])
    assert not catalogue.get_products(context(B), ["IND-FAN-450"])
    assert catalogue.get_products(context(B), ["OFF-DESK-160"])


def test_no_active_source_or_no_installed_provider_is_missing(
    setup: tuple[Sources, list[datetime], TenantCatalogue],
) -> None:
    sources, _, catalogue = setup
    with pytest.raises(CatalogueSourceMissing, match="No product source"):
        catalogue.search_products(context(A), "fan")
    sources.rows[A] = [replace(source(A, "industrial"), status=ConnectionStatus.DISABLED)]
    with pytest.raises(CatalogueSourceMissing):
        catalogue.search_products(context(A), "fan")
    sources.rows[B] = [source(B, "industrial", provider="tally")]
    with pytest.raises(CatalogueSourceMissing, match="not installed"):
        catalogue.search_products(context(B), "fan")


def test_readers_are_cached_and_a_changed_connection_is_picked_up(
    setup: tuple[Sources, list[datetime], TenantCatalogue],
) -> None:
    sources, now, catalogue = setup
    sources.rows[A] = [source(A, "industrial")]
    catalogue.list_categories(context(A))
    catalogue.list_categories(context(A))
    assert sources.reads == 1  # within the TTL: no second lookup
    sources.rows[A] = [replace(source(A, "industrial"), status=ConnectionStatus.DISABLED)]
    now[0] += timedelta(seconds=31)  # after the TTL the switched-off source is noticed
    with pytest.raises(CatalogueSourceMissing):
        catalogue.list_categories(context(A))
    assert sources.reads == 2
