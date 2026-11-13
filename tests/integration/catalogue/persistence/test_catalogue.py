"""Our own catalogue on PostgreSQL: isolation, history-only rows, revisions and decimals."""

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from sales_platform.catalogue.domain.catalogue_admin import InvalidCatalogueEntry, ProductFields
from sales_platform.catalogue.domain.catalogue_search import query_terms
from sales_platform.catalogue.persistence.catalogue import SqlCatalogueStore
from sales_platform.catalogue.ports.catalogue_admin import (
    CatalogueLimitReached,
    CategoryNameTaken,
    CategoryNotFound,
    ProductArchived,
    ProductQuery,
    ProductReferenced,
    ProductState,
    SkuAlreadyExists,
    VersionConflict,
)
from sales_platform.composition import build_catalogue_store
from sales_platform.platform.domain.tenancy import Role, TenantContext
from sales_platform.seed import INDUSTRIAL_TENANT, LOCAL_TENANT, OFFICE_TENANT

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def _sales(tenant_id: UUID = LOCAL_TENANT) -> TenantContext:
    return TenantContext(tenant_id, "sales@local.example", frozenset({Role.SALES_OPERATOR}))


def _new_product(store: SqlCatalogueStore, sku: str = "NEW-1", **fields: object) -> UUID:
    values: dict[str, object] = {"name": "New bolt", "unit": "each", **fields}
    created = store.create_product(_sales(), sku, ProductFields(**values), 10_000)  # type: ignore[arg-type]
    return created.product_id


def test_seeded_local_catalogue_reads_the_demo_products(worker_engine: Engine) -> None:
    store = build_catalogue_store(worker_engine)
    found = store.search(LOCAL_TENANT, query_terms("IND-FAN"), 10)
    assert [p.sku for p in found] == ["IND-FAN-450", "IND-FAN-600"]
    assert [p.sku for p in store.search(LOCAL_TENANT, query_terms("IND-FAN-450"), 10)] == [
        "IND-FAN-450"
    ]
    assert [p.sku for p in store.search(LOCAL_TENANT, query_terms("fasteners"), 10)] == [
        "IND-BOLT-M8"
    ]
    assert store.search(LOCAL_TENANT, query_terms("tata sierra car"), 10) == []
    assert store.search(OFFICE_TENANT, query_terms("IND-FAN"), 10) == []  # not its tables
    prices = store.effective_prices(
        LOCAL_TENANT, ["IND-FAN-450", "IND-PUMP-2HP"], "INR", NOW, "standard"
    )
    assert set(prices) == {"IND-FAN-450"}
    fan = prices["IND-FAN-450"]
    assert (str(fan.entry.unit_price), fan.entry.revision, fan.effective_to) == ("4250.00", 2, None)
    stock = store.current_stock(LOCAL_TENANT, ["IND-CABLE-2C", "IND-FAN-450"], "MAIN")
    assert stock["IND-CABLE-2C"].quantity is None and stock["IND-FAN-450"].quantity == 120
    assert store.category_names_in_use(LOCAL_TENANT) == [
        "Cables",
        "Exhaust fans",
        "Fasteners",
        "Pumps",
    ]
    cable = store.active_products(LOCAL_TENANT, ["IND-CABLE-2C"])[0]
    assert str(cable.fields.quantity_step) == "0.5"


def test_tenants_cannot_see_or_write_each_others_catalogue(
    runtime_engine: Engine, worker_engine: Engine
) -> None:
    for engine in (runtime_engine, worker_engine):
        with engine.begin() as connection:
            connection.execute(
                text(f"SELECT set_config('app.tenant_id', '{INDUSTRIAL_TENANT}', true)")
            )
            for table in (
                "catalogue_products",
                "catalogue_prices",
                "catalogue_stock",
                "catalogue_categories",
            ):
                query = f"SELECT count(*) FROM {table} WHERE tenant_id <> '{INDUSTRIAL_TENANT}'"
                assert connection.execute(text(query)).scalar_one() == 0
    with pytest.raises(DBAPIError), runtime_engine.begin() as connection:
        connection.execute(text(f"SELECT set_config('app.tenant_id', '{INDUSTRIAL_TENANT}', true)"))
        connection.execute(
            text(
                "INSERT INTO catalogue_categories (id, tenant_id, name, created_by, updated_by) "
                f"VALUES (gen_random_uuid(), '{LOCAL_TENANT}', 'X', 'a', 'a')"
            )
        )


def test_a_product_cannot_use_another_businesss_category(runtime_engine: Engine) -> None:
    store = build_catalogue_store(runtime_engine)
    other = store.create_category(_sales(INDUSTRIAL_TENANT), "Fans")
    with pytest.raises(CategoryNotFound):
        _new_product(store, category_id=other.category_id)


def test_worker_changes_only_synced_columns_and_history_rows_never_change(
    runtime_engine: Engine, worker_engine: Engine, owner_engine: Engine
) -> None:
    # Phase 6 (ADR 0023): the sync job may update what a sync changes, never the staff-only
    # description or anything else.
    with pytest.raises(ProgrammingError), worker_engine.begin() as connection:
        connection.execute(text(f"SELECT set_config('app.tenant_id', '{LOCAL_TENANT}', true)"))
        connection.execute(text("UPDATE catalogue_products SET description = 'x'"))
    for statement in ("UPDATE catalogue_prices SET note = 'x'", "DELETE FROM catalogue_stock"):
        with pytest.raises(ProgrammingError), runtime_engine.begin() as connection:
            connection.execute(text(f"SELECT set_config('app.tenant_id', '{LOCAL_TENANT}', true)"))
            connection.execute(text(statement))
        with (
            pytest.raises(DBAPIError, match="append-only|not allowed|immutable"),
            owner_engine.begin() as connection,
        ):
            connection.execute(text(f"SELECT set_config('app.tenant_id', '{LOCAL_TENANT}', true)"))
            connection.execute(text(statement))


def test_create_update_archive_and_audit(runtime_engine: Engine) -> None:
    store = build_catalogue_store(runtime_engine)
    category = store.create_category(_sales(), "Bolts")
    product_id = _new_product(store, "bolt-9", category_id=category.category_id)
    with pytest.raises(SkuAlreadyExists):
        _new_product(store, "BOLT-9")
    with pytest.raises(CategoryNameTaken):
        store.create_category(_sales(), "bolts")
    product = store.get_product(LOCAL_TENANT, product_id)
    assert product.category is not None and product.category.name == "Bolts"
    changed = store.update_product(
        _sales(), product_id, ProductFields("Hex bolt", "box", category_id=category.category_id), 1
    )
    assert (changed.row_version, changed.fields.unit) == (2, "box")
    with pytest.raises(VersionConflict):
        store.update_product(_sales(), product_id, ProductFields("x", "box"), 1)
    archived = store.set_archived(_sales(), product_id, True, 2, 10_000)
    assert archived.archived_at is not None
    assert store.active_products(LOCAL_TENANT, ["bolt-9"]) == []
    page = store.list_products(LOCAL_TENANT, ProductQuery(text="hex", state=ProductState.ARCHIVED))
    assert [p.sku for p in page.items] == ["bolt-9"]
    restored = store.set_archived(_sales(), product_id, False, 3, 10_000)
    assert restored.archived_at is None
    actions = [change.action for change in store.list_changes(LOCAL_TENANT, product_id)]
    assert sorted(actions) == ["archived", "created", "restored", "updated"]


def test_unit_is_locked_once_priced_and_referenced_products_cannot_be_deleted(
    runtime_engine: Engine,
) -> None:
    store = build_catalogue_store(runtime_engine)
    unused = _new_product(store, "UNUSED-1")
    store.delete_product(_sales(), unused)
    assert (
        store.list_products(
            LOCAL_TENANT, ProductQuery(text="unused-1", state=ProductState.ALL)
        ).items
        == ()
    )
    priced = _new_product(store, "PRICED-1")
    store.add_price(_sales(), priced, uuid4(), Decimal("10.00"), "INR", NOW, None, "standard")
    with pytest.raises(InvalidCatalogueEntry):
        store.update_product(_sales(), priced, ProductFields("New bolt", "kg"), 1)
    with pytest.raises(ProductReferenced):
        store.delete_product(_sales(), priced)


def test_concurrent_prices_get_distinct_revisions_and_replays_return_the_stored_row(
    runtime_engine: Engine,
) -> None:
    store = build_catalogue_store(runtime_engine)
    product_id = _new_product(store)
    revisions: list[int] = []

    def add(amount: str) -> None:
        added = store.add_price(
            _sales(), product_id, uuid4(), Decimal(amount), "INR", NOW, None, "standard"
        )
        revisions.append(added.entry.revision)

    threads = [threading.Thread(target=add, args=(amount,)) for amount in ("1.00", "2.00")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(revisions) == [1, 2]

    command = uuid4()
    first = store.add_price(
        _sales(), product_id, command, Decimal("4250.00"), "INR", NOW, "n", "standard"
    )
    again = store.add_price(
        _sales(),
        product_id,
        command,
        Decimal("4250.00"),
        "INR",
        NOW + timedelta(seconds=5),
        "n",
        "standard",
    )
    assert (first.replayed, again.replayed) == (False, True)
    assert again.entry.price_id == first.entry.price_id
    assert str(again.entry.unit_price) == "4250.00"  # decimals kept exactly
    with pytest.raises(VersionConflict):
        store.add_price(_sales(), product_id, command, Decimal("1.00"), "INR", NOW, "n", "standard")


def test_stock_counts_and_summary_counts(runtime_engine: Engine) -> None:
    store = build_catalogue_store(runtime_engine)
    product_id = _new_product(store, "CABLE-X", unit="metre", quantity_step=Decimal("0.5"))
    store.add_stock(
        _sales(), product_id, uuid4(), Decimal("12.5"), NOW - timedelta(days=10), None, "MAIN"
    )
    store.add_stock(
        _sales(), product_id, uuid4(), Decimal("3"), NOW - timedelta(days=20), None, "MAIN"
    )
    current = store.current_stock(LOCAL_TENANT, ["CABLE-X"], "MAIN")["CABLE-X"]
    assert str(current.quantity) == "12.5"  # an older count entered later does not win
    counts = store.counts(LOCAL_TENANT, "INR", NOW, NOW - timedelta(days=7))
    # Seed: 5 products, 4 priced, cable stock not tracked; plus CABLE-X: unpriced, stale.
    assert (
        counts.active_products,
        counts.priced_products,
        counts.stale_stock,
        counts.unknown_stock,
    ) == (6, 4, 1, 1)


def test_archived_products_take_no_new_entries_and_limit_is_enforced(
    runtime_engine: Engine,
) -> None:
    store = build_catalogue_store(runtime_engine)
    product_id = _new_product(store)
    store.set_archived(_sales(), product_id, True, 1, 10_000)
    with pytest.raises(ProductArchived):
        store.add_stock(_sales(), product_id, uuid4(), None, NOW, None, "MAIN")
    with pytest.raises(CatalogueLimitReached):
        store.create_product(_sales(), "LIMIT-1", ProductFields("x", "each"), 5)  # seed has 5


def test_category_rename_and_archive_refresh_search_words(runtime_engine: Engine) -> None:
    store = build_catalogue_store(runtime_engine)
    category = next(c for c in store.list_categories(LOCAL_TENANT) if c.name == "Pumps")
    assert category.active_products == 1
    renamed = store.update_category(
        _sales(), category.category_id, "Water movers", None, category.row_version
    )
    assert [p.sku for p in store.search(LOCAL_TENANT, query_terms("water movers"), 5)] == [
        "IND-PUMP-2HP"
    ]
    store.update_category(_sales(), category.category_id, None, True, renamed.row_version)
    assert store.search(LOCAL_TENANT, query_terms("movers"), 5) == []
    assert "Water movers" not in store.category_names_in_use(LOCAL_TENANT)
