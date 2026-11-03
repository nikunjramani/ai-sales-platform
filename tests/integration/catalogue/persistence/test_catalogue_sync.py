"""Phase 6 on PostgreSQL: a product source's catalogue copied into ours by the worker role, and
a new product source replacing the business's previous one (and Disable bringing it back)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from sales_platform.catalogue.domain.catalogue_admin import DEFAULT_LOCATION, LOCAL_PRICE_LIST
from sales_platform.catalogue.domain.catalogue_search import query_terms
from sales_platform.catalogue.domain.catalogue_sync import (
    CatalogueFeed,
    FeedPrice,
    FeedProduct,
    FeedStock,
    SyncStatus,
    SyncTrigger,
)
from sales_platform.catalogue.persistence.catalogue_sync import SqlCatalogueSync
from sales_platform.composition import build_catalogue_store
from sales_platform.integrations.domain.connections import ConnectionStatus
from sales_platform.integrations.persistence.connections import SqlConnectionStore
from sales_platform.integrations.persistence.provider_state import SqlConnectionLifecycle
from sales_platform.integrations.ports.connections import ConnectionChange
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.seed import INDUSTRIAL_TENANT, LOCAL_TENANT

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def _source(owner: Engine) -> UUID:
    with tenant_transaction(owner, LOCAL_TENANT) as connection:
        return UUID(
            str(
                connection.execute(
                    text(
                        "SELECT id FROM connections WHERE tenant_id = :t AND status = 'active' "
                        "AND purposes @> ARRAY['catalogue']"
                    ),
                    {"t": LOCAL_TENANT},
                ).scalar_one()
            )
        )


def _feed(price: str = "4300", stock: str = "7") -> CatalogueFeed:
    now = datetime.now(UTC)  # read from the source just now (later than the seeded counts)
    return CatalogueFeed(
        products=(
            FeedProduct("IND-FAN-450", "Exhaust fan 450 mm (ERP)", "each", "IND-FAN-450", "Fans"),
            FeedProduct("ERP-NEW-1", "New from the ERP", "metre", "ERP-NEW-1", "Cables"),
        ),
        prices=(
            FeedPrice("IND-FAN-450", Decimal(price), "INR", "each", NOW - timedelta(days=30)),
            FeedPrice("ERP-NEW-1", Decimal("12.5"), "USD", "metre", NOW),
        ),
        stock=(
            FeedStock("IND-FAN-450", Decimal(stock), "each", now),
            FeedStock("ERP-NEW-1", None, "metre", now),
        ),
    )


def test_a_feed_is_stored_as_a_copy_and_only_changes_add_rows(
    worker_engine: Engine, runtime_engine: Engine, owner_engine: Engine
) -> None:
    sync = SqlCatalogueSync(worker_engine)
    source = _source(owner_engine)
    run = sync.start(LOCAL_TENANT, source, SyncTrigger.CONNECTED)
    next_run = NOW + timedelta(minutes=15)
    counts = sync.apply(LOCAL_TENANT, run, _feed(), "sync:test", next_run)
    assert (counts.created, counts.updated) == (1, 1)
    assert counts.archived >= 1  # every other typed-in product is no longer offered
    assert (counts.prices_added, counts.stock_added) == (1, 2)
    latest = sync.latest(LOCAL_TENANT, source)
    assert latest is not None and latest.status is SyncStatus.SUCCEEDED
    assert {issue.code for issue in latest.issues} == {"currency_mismatch"}  # USD never stored

    reader = build_catalogue_store(runtime_engine)
    [fan] = reader.active_products(LOCAL_TENANT, ["IND-FAN-450"])
    assert fan.fields.name == "Exhaust fan 450 mm (ERP)"
    assert [p.sku for p in reader.search(LOCAL_TENANT, query_terms("ERP-NEW-1"), 5)] == [
        "ERP-NEW-1"
    ]
    price = reader.effective_prices(
        LOCAL_TENANT, ["IND-FAN-450"], "INR", datetime.now(UTC), LOCAL_PRICE_LIST
    )["IND-FAN-450"]
    assert price.entry.unit_price == Decimal("4300") and price.entry.source.value == "sync"
    stock = reader.current_stock(LOCAL_TENANT, ["IND-FAN-450", "ERP-NEW-1"], DEFAULT_LOCATION)
    assert stock["IND-FAN-450"].quantity == 7 and stock["ERP-NEW-1"].quantity is None
    assert reader.active_products(INDUSTRIAL_TENANT, ["ERP-NEW-1"]) == []  # other businesses

    again = sync.apply(LOCAL_TENANT, sync.start(LOCAL_TENANT, source, SyncTrigger.SCHEDULE),
                       _feed(), "sync:test", None)  # fmt: skip
    assert (again.created, again.updated, again.prices_added, again.stock_added) == (0, 0, 0, 0)
    changed = sync.apply(LOCAL_TENANT, sync.start(LOCAL_TENANT, source, SyncTrigger.MANUAL),
                         _feed(price="4350", stock="6"), "sync:test", None)  # fmt: skip
    assert (changed.prices_added, changed.stock_added) == (1, 1)
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        scheduled = connection.execute(
            text(
                "SELECT count(*) FROM jobs WHERE tenant_id = :t AND kind = 'sync_catalogue' "
                "AND payload->>'trigger' = 'schedule' AND run_at > now()"
            ),
            {"t": LOCAL_TENANT},
        ).scalar_one()
    assert scheduled == 1  # only the run that was given next_run_at queued one


def test_a_failed_run_keeps_the_copy_and_an_interrupted_one_is_closed(
    worker_engine: Engine, runtime_engine: Engine, owner_engine: Engine
) -> None:
    sync = SqlCatalogueSync(worker_engine)
    source = _source(owner_engine)
    sync.start(LOCAL_TENANT, source, SyncTrigger.SCHEDULE)  # the worker "dies" here
    run = sync.start(LOCAL_TENANT, source, SyncTrigger.SCHEDULE)
    sync.fail(LOCAL_TENANT, run, "ERPNext refused the API key", (), None)
    latest = sync.latest(LOCAL_TENANT, source)
    assert latest is not None and latest.status is SyncStatus.FAILED
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        statuses = connection.execute(
            text("SELECT status, error FROM catalogue_sync_runs WHERE tenant_id = :t"),
            {"t": LOCAL_TENANT},
        ).all()
    assert sorted(row.status for row in statuses) == ["failed", "failed"]
    assert any("Interrupted" in (row.error or "") for row in statuses)
    assert build_catalogue_store(runtime_engine).active_products(LOCAL_TENANT, ["IND-FAN-450"])


def test_sync_now_queues_one_manual_run_at_a_time(
    runtime_engine: Engine, owner_engine: Engine
) -> None:
    sync = SqlCatalogueSync(runtime_engine)  # the API role
    source = _source(owner_engine)
    assert sync.request(LOCAL_TENANT, source, "sales@local.example") is True
    assert sync.request(LOCAL_TENANT, source, "sales@local.example") is False


def test_a_new_product_source_replaces_the_old_one_and_disable_restores_it(
    runtime_engine: Engine, owner_engine: Engine
) -> None:
    connections = SqlConnectionStore(runtime_engine)
    lifecycle = SqlConnectionLifecycle(runtime_engine)
    old = _source(owner_engine)
    new = lifecycle.create(LOCAL_TENANT, "erpnext", "ERPNext", ["catalogue", "orders"], "admin")
    lifecycle.apply(
        LOCAL_TENANT,
        new,
        ConnectionChange(
            status=ConnectionStatus.ACTIVE,
            config={"sync_minutes": 15},
            secret_refs={},
            last_error=None,
            pending_action=None,
            event_kind="resumed",
            exclusive_purposes=("catalogue", "orders"),
            jobs=(("sync_catalogue", {"connection_id": str(new), "trigger": "connected"}),),
        ),
        "admin",
    )
    [active] = connections.catalogue_sources(LOCAL_TENANT)
    assert active.connection_id == new
    previous = connections.get(LOCAL_TENANT, old)
    assert previous is not None and previous.status is ConnectionStatus.DISABLED
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        queued = connection.execute(
            text("SELECT count(*) FROM jobs WHERE tenant_id = :t AND kind = 'sync_catalogue'"),
            {"t": LOCAL_TENANT},
        ).scalar_one()
    assert queued == 1

    lifecycle.apply(
        LOCAL_TENANT,
        new,
        ConnectionChange(
            status=ConnectionStatus.DISABLED,
            config={},
            secret_refs={},
            last_error=None,
            pending_action=None,
            event_kind="deprovisioned",
            restore_replaced=True,
        ),
        "admin",
    )
    [restored] = connections.catalogue_sources(LOCAL_TENANT)
    assert restored.connection_id == old
