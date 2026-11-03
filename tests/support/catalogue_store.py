"""Test double: our own catalogue kept in memory (moved from src, ADR 0026 step 2; never real data).

It implements both catalogue ports with the same rules as the PostgreSQL store: one business
never sees another's rows, prices and counts are only added, row versions guard edits, and
search uses the shared word matching. The ERP contract and search-quality tests run
LocalCatalogue over this store and over PostgreSQL, so both must behave the same.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sales_platform.catalogue.adapters.erp.simulated import SimulatedCatalogue
from sales_platform.catalogue.domain.catalogue_admin import (
    DEFAULT_LOCATION,
    LOCAL_PRICE_LIST,
    CatalogueProduct,
    Category,
    ChangeRecord,
    EffectivePrice,
    EntrySource,
    InvalidCatalogueEntry,
    PriceEntry,
    ProductFields,
    StockEntry,
    current_stock,
    effective_price,
    field_changes,
    fields_as_dict,
)
from sales_platform.catalogue.domain.catalogue_search import best_matches, product_terms
from sales_platform.catalogue.ports.catalogue_admin import (
    AddedEntry,
    CatalogueCounts,
    CatalogueLimitReached,
    CatalogueUnavailable,
    CategoryNameTaken,
    CategoryNotFound,
    ProductArchived,
    ProductNotFound,
    ProductPage,
    ProductQuery,
    ProductReferenced,
    ProductState,
    SkuAlreadyExists,
    VersionConflict,
)
from sales_platform.platform.domain.tenancy import TenantContext

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InMemoryCatalogueStore:
    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._products: dict[UUID, tuple[UUID, CatalogueProduct]] = {}
        self._categories: dict[UUID, tuple[UUID, Category]] = {}
        self._prices: list[tuple[UUID, PriceEntry, UUID]] = []  # tenant, entry, command
        self._stock: list[tuple[UUID, StockEntry, UUID]] = []
        self._changes: list[tuple[UUID, ChangeRecord]] = []
        # SKUs used in quotations (tests mark them, as the SQL store reads quotation tables).
        self.quoted: set[tuple[UUID, str]] = set()
        self.unavailable = False  # tests: the store cannot be reached

    @classmethod
    def from_simulated(
        cls, catalogues: Mapping[UUID, SimulatedCatalogue], clock: Callable[[], datetime] = _utc_now
    ) -> "InMemoryCatalogueStore":
        """The synthetic demo catalogues, copied the way the seed copies them."""
        store = cls(clock)
        for tenant_id, catalogue in catalogues.items():
            names = sorted({p.category for p in catalogue.products.values() if p.category})
            ids = {name: uuid4() for name in names}
            for name in names:
                store._categories[ids[name]] = (tenant_id, Category(ids[name], name, None, 1))
            for sku, product in sorted(catalogue.products.items()):
                product_id = uuid4()
                record = CatalogueProduct(
                    product_id, sku,
                    ProductFields(
                        product.description, product.unit, None,
                        ids.get(product.category or ""), product.minimum_quantity,
                        product.quantity_step,
                    ),
                    store._categories[ids[product.category]][1] if product.category else None,
                    None, 1, _EPOCH, _EPOCH,
                )  # fmt: skip
                store._products[product_id] = (tenant_id, record)
                ordered = sorted(catalogue.prices.get(sku, ()), key=lambda p: p.effective_from)
                for revision, price in enumerate(ordered, start=1):
                    entry = PriceEntry(
                        uuid4(), product_id, LOCAL_PRICE_LIST, revision, price.currency,
                        product.unit, price.unit_price, price.effective_from, EntrySource.MANUAL,
                        None, _EPOCH, "seed",
                    )  # fmt: skip
                    store._prices.append((tenant_id, entry, uuid4()))
                if sku in catalogue.stock:
                    now = store._clock()
                    count = StockEntry(
                        uuid4(), product_id, DEFAULT_LOCATION, catalogue.stock[sku],
                        product.unit, now, EntrySource.MANUAL, None, now, "seed",
                    )  # fmt: skip
                    store._stock.append((tenant_id, count, uuid4()))
        return store

    # --- helpers -----------------------------------------------------------------------------

    def _check(self) -> None:
        if self.unavailable:
            raise CatalogueUnavailable("The product list could not be read")

    def _all(self, tenant_id: UUID) -> list[CatalogueProduct]:
        return [self._fresh(p) for t, p in self._products.values() if t == tenant_id]

    def _fresh(self, product: CatalogueProduct) -> CatalogueProduct:
        """The product with its category as it is now."""
        category_id = product.fields.category_id
        category = self._categories[category_id][1] if category_id else None
        return replace(product, category=category)

    def _active(self, tenant_id: UUID) -> list[CatalogueProduct]:
        return [p for p in self._all(tenant_id) if p.active]

    def _get(self, tenant_id: UUID, product_id: UUID) -> CatalogueProduct:
        found = self._products.get(product_id)
        if found is None or found[0] != tenant_id:
            raise ProductNotFound("No such product")
        return self._fresh(found[1])

    def _category(self, tenant_id: UUID, category_id: UUID | None) -> Category | None:
        if category_id is None:
            return None
        found = self._categories.get(category_id)
        if found is None or found[0] != tenant_id:
            raise CategoryNotFound("No such category")
        return found[1]

    def _terms(self, product: CatalogueProduct) -> frozenset[str]:
        category = product.category
        name = category.name if category and category.archived_at is None else None
        return product_terms(product.sku, product.fields.name, name)

    def _entries(self, tenant_id: UUID, product_id: UUID) -> bool:
        return any(t == tenant_id and e.product_id == product_id for t, e, _ in self._prices) or (
            any(t == tenant_id and e.product_id == product_id for t, e, _ in self._stock)
        )

    def _record(
        self,
        context: TenantContext,
        entity: str,
        entity_id: UUID,
        action: str,
        before: Mapping[str, object],
        after: Mapping[str, object],
    ) -> None:
        change = ChangeRecord(
            entity, entity_id, action, field_changes(before, after), context.subject, self._clock()
        )
        self._changes.append((context.tenant_id, change))

    def _by_sku(self, tenant_id: UUID, skus: Sequence[str]) -> dict[str, CatalogueProduct]:
        wanted = set(skus)
        return {p.sku: p for p in self._active(tenant_id) if p.sku in wanted}

    # --- CatalogueReadStore ------------------------------------------------------------------

    def search(
        self, tenant_id: UUID, terms: frozenset[str], limit: int
    ) -> Sequence[CatalogueProduct]:
        self._check()
        scored = ((len(terms & self._terms(p)), p.sku, p) for p in self._active(tenant_id))
        return best_matches(scored, limit)

    def active_products(self, tenant_id: UUID, skus: Sequence[str]) -> Sequence[CatalogueProduct]:
        self._check()
        return list(self._by_sku(tenant_id, skus).values())

    def effective_prices(
        self, tenant_id: UUID, skus: Sequence[str], currency: str, at: datetime, price_list: str
    ) -> Mapping[str, EffectivePrice]:
        self._check()
        result: dict[str, EffectivePrice] = {}
        for sku, product in self._by_sku(tenant_id, skus).items():
            entries = [
                e
                for t, e, _ in self._prices
                if t == tenant_id
                and e.product_id == product.product_id
                and e.currency == currency
                and e.price_list == price_list
            ]
            chosen = effective_price(entries, at)
            if chosen is not None:
                result[sku] = chosen
        return result

    def current_stock(
        self, tenant_id: UUID, skus: Sequence[str], location: str
    ) -> Mapping[str, StockEntry]:
        self._check()
        result: dict[str, StockEntry] = {}
        for sku, product in self._by_sku(tenant_id, skus).items():
            entries = [
                e
                for t, e, _ in self._stock
                if t == tenant_id and e.product_id == product.product_id
            ]
            count = current_stock(entries, location)
            if count is not None:
                result[sku] = count
        return result

    def category_names_in_use(self, tenant_id: UUID) -> Sequence[str]:
        self._check()
        used = {p.category.name for p in self._active(tenant_id) if p.category}
        active = {
            c.name for t, c in self._categories.values() if t == tenant_id and not c.archived_at
        }
        return sorted(used & active)

    # --- CatalogueStore: products ------------------------------------------------------------

    def list_products(self, tenant_id: UUID, query: ProductQuery) -> ProductPage:
        self._check()
        items = self._all(tenant_id)
        if query.state is ProductState.ACTIVE:
            items = [p for p in items if p.active]
        elif query.state is ProductState.ARCHIVED:
            items = [p for p in items if not p.active]
        if query.category_id is not None:
            items = [p for p in items if p.fields.category_id == query.category_id]
        if query.text:
            text = query.text.casefold()
            items = [
                p for p in items if text in p.sku.casefold() or text in p.fields.name.casefold()
            ]
        items.sort(key=lambda p: p.sku.casefold())
        if query.after is not None:
            items = [p for p in items if p.sku.casefold() > query.after]
        page = tuple(items[: query.limit])
        more = len(items) > query.limit
        return ProductPage(page, page[-1].sku.casefold() if more and page else None)

    def get_product(self, tenant_id: UUID, product_id: UUID) -> CatalogueProduct:
        self._check()
        return self._get(tenant_id, product_id)

    def create_product(
        self, context: TenantContext, sku: str, fields: ProductFields, max_active: int
    ) -> CatalogueProduct:
        with self._lock:
            tenant_id = context.tenant_id
            if len(self._active(tenant_id)) >= max_active:
                raise CatalogueLimitReached(f"A business can have {max_active:,} products")
            if any(p.sku.casefold() == sku.casefold() for p in self._all(tenant_id)):
                raise SkuAlreadyExists(f"A product with SKU {sku} already exists")
            category = self._category(tenant_id, fields.category_id)
            now = self._clock()
            product = CatalogueProduct(uuid4(), sku, fields, category, None, 1, now, now)
            self._products[product.product_id] = (tenant_id, product)
            self._record(
                context, "product", product.product_id, "created", {},
                {"sku": sku, **fields_as_dict(fields)},
            )  # fmt: skip
            return product

    def update_product(
        self,
        context: TenantContext,
        product_id: UUID,
        fields: ProductFields,
        expected_version: int,
    ) -> CatalogueProduct:
        with self._lock:
            current = self._get(context.tenant_id, product_id)
            if current.row_version != expected_version:
                raise VersionConflict("This product was changed by someone else")
            if current.fields.unit != fields.unit and self._entries(context.tenant_id, product_id):
                raise InvalidCatalogueEntry(
                    "unit", "The unit cannot change once a price or stock count exists"
                )
            category = self._category(context.tenant_id, fields.category_id)
            changed = replace(
                current,
                fields=fields,
                category=category,
                row_version=current.row_version + 1,
                updated_at=self._clock(),
            )
            self._products[product_id] = (context.tenant_id, changed)
            self._record(
                context, "product", product_id, "updated",
                fields_as_dict(current.fields), fields_as_dict(fields),
            )  # fmt: skip
            return changed

    def set_archived(
        self,
        context: TenantContext,
        product_id: UUID,
        archived: bool,
        expected_version: int,
        max_active: int,
    ) -> CatalogueProduct:
        with self._lock:
            current = self._get(context.tenant_id, product_id)
            if current.row_version != expected_version:
                raise VersionConflict("This product was changed by someone else")
            if current.active != archived:
                return current
            if not archived and len(self._active(context.tenant_id)) >= max_active:
                raise CatalogueLimitReached(f"A business can have {max_active:,} products")
            changed = replace(
                current,
                archived_at=self._clock() if archived else None,
                row_version=current.row_version + 1,
                updated_at=self._clock(),
            )
            self._products[product_id] = (context.tenant_id, changed)
            action = "archived" if archived else "restored"
            self._record(context, "product", product_id, action, {}, {})
            return changed

    def delete_product(self, context: TenantContext, product_id: UUID) -> None:
        with self._lock:
            current = self._get(context.tenant_id, product_id)
            if self._entries(context.tenant_id, product_id) or (
                (context.tenant_id, current.sku) in self.quoted
            ):
                raise ProductReferenced("This product has been priced, counted or quoted")
            del self._products[product_id]
            before = {"sku": current.sku, **fields_as_dict(current.fields)}
            self._record(context, "product", product_id, "deleted", before, {})

    # --- CatalogueStore: prices and stock ---------------------------------------------------

    def list_prices(self, tenant_id: UUID, product_id: UUID) -> Sequence[PriceEntry]:
        self._get(tenant_id, product_id)
        entries = [e for t, e, _ in self._prices if t == tenant_id and e.product_id == product_id]
        return sorted(entries, key=lambda e: (e.effective_from, e.revision), reverse=True)

    def add_price(
        self,
        context: TenantContext,
        product_id: UUID,
        command_id: UUID,
        unit_price: Decimal,
        currency: str,
        effective_from: datetime,
        note: str | None,
        price_list: str,
    ) -> AddedEntry[PriceEntry]:
        with self._lock:
            tenant_id = context.tenant_id
            product = self._get(tenant_id, product_id)
            for t, entry, command in self._prices:
                if t == tenant_id and command == command_id:
                    key = (entry.product_id, entry.unit_price, entry.currency, entry.note)
                    if key != (product_id, unit_price, currency, note):
                        raise VersionConflict("This request ID was already used for other values")
                    return AddedEntry(entry, True)
            if not product.active:
                raise ProductArchived("Restore the product before entering prices or stock")
            revision = 1 + max(
                (
                    e.revision
                    for t, e, _ in self._prices
                    if t == tenant_id
                    and e.product_id == product_id
                    and e.price_list == price_list
                    and e.currency == currency
                ),
                default=0,
            )
            entry = PriceEntry(
                uuid4(), product_id, price_list, revision, currency, product.fields.unit,
                unit_price, effective_from, EntrySource.MANUAL, note, self._clock(),
                context.subject,
            )  # fmt: skip
            self._prices.append((tenant_id, entry, command_id))
            return AddedEntry(entry, False)

    def list_stock(self, tenant_id: UUID, product_id: UUID) -> Sequence[StockEntry]:
        self._get(tenant_id, product_id)
        entries = [e for t, e, _ in self._stock if t == tenant_id and e.product_id == product_id]
        return sorted(entries, key=lambda e: (e.as_of, e.recorded_at), reverse=True)

    def add_stock(
        self,
        context: TenantContext,
        product_id: UUID,
        command_id: UUID,
        quantity: Decimal | None,
        as_of: datetime,
        note: str | None,
        location: str,
    ) -> AddedEntry[StockEntry]:
        with self._lock:
            tenant_id = context.tenant_id
            product = self._get(tenant_id, product_id)
            for t, entry, command in self._stock:
                if t == tenant_id and command == command_id:
                    key = (entry.product_id, entry.quantity, entry.location, entry.note)
                    if key != (product_id, quantity, location, note):
                        raise VersionConflict("This request ID was already used for other values")
                    return AddedEntry(entry, True)
            if not product.active:
                raise ProductArchived("Restore the product before entering prices or stock")
            entry = StockEntry(
                uuid4(), product_id, location, quantity, product.fields.unit, as_of,
                EntrySource.MANUAL, note, self._clock(), context.subject,
            )  # fmt: skip
            self._stock.append((tenant_id, entry, command_id))
            return AddedEntry(entry, False)

    def list_changes(self, tenant_id: UUID, entity_id: UUID) -> Sequence[ChangeRecord]:
        found = [c for t, c in self._changes if t == tenant_id and c.entity_id == entity_id]
        return list(reversed(found))

    # --- CatalogueStore: categories ---------------------------------------------------------

    def list_categories(self, tenant_id: UUID) -> Sequence[Category]:
        active = self._active(tenant_id)
        return sorted(
            (
                replace(
                    c,
                    active_products=sum(1 for p in active if p.fields.category_id == c.category_id),
                )
                for t, c in self._categories.values()
                if t == tenant_id
            ),
            key=lambda c: c.name,
        )

    def create_category(self, context: TenantContext, name: str) -> Category:
        with self._lock:
            self._unique_name(context.tenant_id, name, None)
            category = Category(uuid4(), name, None, 1)
            self._categories[category.category_id] = (context.tenant_id, category)
            self._record(context, "category", category.category_id, "created", {}, {"name": name})
            return category

    def update_category(
        self,
        context: TenantContext,
        category_id: UUID,
        name: str | None,
        archived: bool | None,
        expected_version: int,
    ) -> Category:
        with self._lock:
            current = self._category(context.tenant_id, category_id)
            assert current is not None
            if current.row_version != expected_version:
                raise VersionConflict("This category was changed by someone else")
            if name is not None:
                self._unique_name(context.tenant_id, name, category_id)
            was_archived = current.archived_at is not None
            now_archived = was_archived if archived is None else archived
            changed = Category(
                category_id,
                current.name if name is None else name,
                (current.archived_at or self._clock()) if now_archived else None,
                current.row_version + 1,
            )
            self._categories[category_id] = (context.tenant_id, changed)
            action = (
                "archived"
                if now_archived and not was_archived
                else "restored"
                if was_archived and not now_archived
                else "updated"
            )
            self._record(
                context, "category", category_id, action,
                {"name": current.name, "archived": was_archived},
                {"name": changed.name, "archived": now_archived},
            )  # fmt: skip
            return changed

    def _unique_name(self, tenant_id: UUID, name: str, own: UUID | None) -> None:
        for t, category in self._categories.values():
            if (
                t == tenant_id
                and category.category_id != own
                and category.name.casefold() == name.casefold()
            ):
                raise CategoryNameTaken(f"A category named {name} already exists")

    def counts(
        self, tenant_id: UUID, currency: str | None, at: datetime, stale_before: datetime | None
    ) -> CatalogueCounts:
        active = self._active(tenant_id)
        skus = [p.sku for p in active]
        priced = (
            set(self.effective_prices(tenant_id, skus, currency, at, LOCAL_PRICE_LIST))
            if currency
            else set()
        )
        counts = {}
        for product in active:
            entries = [
                e
                for t, e, _ in self._stock
                if t == tenant_id and e.product_id == product.product_id
            ]
            latest = max(entries, key=lambda e: (e.as_of, e.recorded_at), default=None)
            counts[product.sku] = latest
        stale = sum(
            1
            for c in counts.values()
            if c is not None and c.quantity is not None and stale_before and c.as_of < stale_before
        )
        unknown = sum(1 for c in counts.values() if c is None or c.quantity is None)
        return CatalogueCounts(len(active), len(priced), stale, unknown)
