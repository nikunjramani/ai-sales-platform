"""Our own product catalogue in PostgreSQL (phase 3, ADR 0020).

SqlCatalogueStore implements both catalogue ports over the catalogue_* tables:
- CatalogueReadStore for LocalCatalogue (the API and the worker read through it);
- CatalogueStore for the product screens (API role only; the worker cannot write).

Every query runs in a tenant transaction and filters by tenant_id; row-level security is the
second guard. Prices and stock counts are inserted, never updated. Writes to one product lock
its row first, so concurrent price entries get revisions 1, 2, ... and never a duplicate.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import (
    Connection,
    Engine,
    Row,
    and_,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError

from sales_platform.catalogue.domain.catalogue_admin import (
    CatalogueProduct,
    Category,
    ChangeRecord,
    EffectivePrice,
    EntrySource,
    InvalidCatalogueEntry,
    PriceEntry,
    ProductFields,
    StockEntry,
    effective_price,
    field_changes,
    fields_as_dict,
)
from sales_platform.catalogue.domain.catalogue_search import product_terms
from sales_platform.catalogue.persistence.tables import (
    CatalogueCategory,
    CatalogueChange,
    CataloguePriceRow,
    CatalogueProductRow,
    CatalogueStockRow,
)
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
from sales_platform.platform.persistence.tenancy import tenant_transaction

T = TypeVar("T")
_P = CatalogueProductRow
_C = CatalogueCategory
_PRICE = CataloguePriceRow
_STOCK = CatalogueStockRow
_UNIQUE_VIOLATION = "23505"

_SEARCH = text(
    """
    WITH scored AS (
        SELECT p.id, p.sku,
               cardinality(ARRAY(
                   SELECT unnest(p.search_terms) INTERSECT SELECT unnest(CAST(:terms AS text[]))
               )) AS score
        FROM catalogue_products p
        WHERE p.tenant_id = :tenant AND p.archived_at IS NULL
          AND p.search_terms && CAST(:terms AS text[])
    )
    SELECT id FROM scored WHERE score = (SELECT max(score) FROM scored)
    ORDER BY sku COLLATE "C"
    LIMIT :limit
    """
)


def _constraint(error: DBAPIError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


def _category(row: Row[Any], prefix: str = "") -> Category | None:
    category_id = getattr(row, f"{prefix}id")
    if category_id is None:
        return None
    return Category(
        category_id=category_id,
        name=getattr(row, f"{prefix}name"),
        archived_at=getattr(row, f"{prefix}archived_at"),
        row_version=getattr(row, f"{prefix}row_version"),
    )


_CATEGORY_COLUMNS = (
    _C.id.label("c_id"),
    _C.name.label("c_name"),
    _C.archived_at.label("c_archived_at"),
    _C.row_version.label("c_row_version"),
)


def _product_select() -> Any:
    return select(_P, *_CATEGORY_COLUMNS).outerjoin(
        _C, and_(_C.tenant_id == _P.tenant_id, _C.id == _P.category_id)
    )


def _product(row: Row[Any]) -> CatalogueProduct:
    product = row
    return CatalogueProduct(
        product_id=product.id,
        sku=product.sku,
        fields=ProductFields(
            name=product.name,
            unit=product.unit,
            description=product.description,
            category_id=product.category_id,
            minimum_quantity=product.minimum_quantity,
            quantity_step=product.quantity_step,
        ),
        category=_category(row, "c_"),
        archived_at=product.archived_at,
        row_version=product.row_version,
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def _price(row: Row[Any]) -> PriceEntry:
    return PriceEntry(
        price_id=row.id,
        product_id=row.product_id,
        price_list=row.price_list,
        revision=row.revision,
        currency=row.currency,
        unit=row.unit,
        unit_price=row.unit_price,
        effective_from=row.effective_from,
        source=EntrySource(row.source),
        note=row.note,
        created_at=row.created_at,
        created_by=row.created_by,
    )


def _stock(row: Row[Any]) -> StockEntry:
    return StockEntry(
        stock_id=row.id,
        product_id=row.product_id,
        location=row.location_code,
        quantity=row.quantity,
        unit=row.unit,
        as_of=row.as_of,
        source=EntrySource(row.source),
        note=row.note,
        recorded_at=row.recorded_at,
        recorded_by=row.recorded_by,
    )


def _terms(sku: str, name: str, category: Category | None) -> list[str]:
    """Search words; an archived category no longer describes the product."""
    category_name = category.name if category and category.archived_at is None else None
    return sorted(product_terms(sku, name, category_name))


# A later module that may still need a product (e.g. quotations: "was this SKU ever
# quoted?"). Runs in the delete's own transaction (ADR 0026: catalogue does not read their tables).
type ProductInUse = Callable[[Connection, UUID, str], bool]


class SqlCatalogueStore:
    def __init__(self, engine: Engine, in_use: Sequence[ProductInUse] = ()) -> None:
        self._engine = engine
        self._in_use = tuple(in_use)

    @contextmanager
    def _read(self, tenant_id: UUID) -> Iterator[Connection]:
        """A tenant transaction whose connection failures become CatalogueUnavailable."""
        try:
            with tenant_transaction(self._engine, tenant_id) as connection:
                yield connection
        except (OperationalError, InterfaceError) as error:
            raise CatalogueUnavailable("The product list could not be read") from error

    # --- CatalogueReadStore ------------------------------------------------------------------

    def search(
        self, tenant_id: UUID, terms: frozenset[str], limit: int
    ) -> Sequence[CatalogueProduct]:
        if not terms:
            return []
        with self._read(tenant_id) as connection:
            ids = list(
                connection.execute(
                    _SEARCH, {"tenant": tenant_id, "terms": sorted(terms), "limit": limit}
                ).scalars()
            )
            found = self._products_by(connection, tenant_id, _P.id.in_(ids))
        order = {product_id: index for index, product_id in enumerate(ids)}
        return sorted(found, key=lambda product: order[product.product_id])

    def active_products(self, tenant_id: UUID, skus: Sequence[str]) -> Sequence[CatalogueProduct]:
        if not skus:
            return []
        with self._read(tenant_id) as connection:
            return self._products_by(
                connection, tenant_id, _P.sku.in_(list(skus)), _P.archived_at.is_(None)
            )

    def effective_prices(
        self, tenant_id: UUID, skus: Sequence[str], currency: str, at: datetime, price_list: str
    ) -> Mapping[str, EffectivePrice]:
        if not skus:
            return {}
        with self._read(tenant_id) as connection:
            rows = connection.execute(
                select(_PRICE, _P.sku)
                .join(_P, and_(_P.tenant_id == _PRICE.tenant_id, _P.id == _PRICE.product_id))
                .where(
                    _PRICE.tenant_id == tenant_id,
                    _P.sku.in_(list(skus)),
                    _P.archived_at.is_(None),
                    _PRICE.currency == currency,
                    _PRICE.price_list == price_list,
                )
            ).all()
        by_sku: dict[str, list[PriceEntry]] = {}
        for row in rows:
            by_sku.setdefault(row.sku, []).append(_price(row))
        result: dict[str, EffectivePrice] = {}
        for sku, entries in by_sku.items():
            chosen = effective_price(entries, at)
            if chosen is not None:
                result[sku] = chosen
        return result

    def current_stock(
        self, tenant_id: UUID, skus: Sequence[str], location: str
    ) -> Mapping[str, StockEntry]:
        if not skus:
            return {}
        with self._read(tenant_id) as connection:
            rows = connection.execute(
                select(_STOCK, _P.sku)
                .join(_P, and_(_P.tenant_id == _STOCK.tenant_id, _P.id == _STOCK.product_id))
                .where(
                    _STOCK.tenant_id == tenant_id,
                    _P.sku.in_(list(skus)),
                    _P.archived_at.is_(None),
                    _STOCK.location_code == location,
                )
                .order_by(_P.sku, _STOCK.as_of.desc(), _STOCK.recorded_at.desc())
                .distinct(_P.sku)
            ).all()
        return {row.sku: _stock(row) for row in rows}

    def category_names_in_use(self, tenant_id: UUID) -> Sequence[str]:
        with self._read(tenant_id) as connection:
            return list(
                connection.execute(
                    select(_C.name)
                    .where(
                        _C.tenant_id == tenant_id,
                        _C.archived_at.is_(None),
                        exists().where(
                            _P.tenant_id == _C.tenant_id,
                            _P.category_id == _C.id,
                            _P.archived_at.is_(None),
                        ),
                    )
                    .order_by(_C.name.collate("C"))
                ).scalars()
            )

    # --- CatalogueStore: products ------------------------------------------------------------

    def list_products(self, tenant_id: UUID, query: ProductQuery) -> ProductPage:
        conditions: list[Any] = [_P.tenant_id == tenant_id]
        if query.state is ProductState.ACTIVE:
            conditions.append(_P.archived_at.is_(None))
        elif query.state is ProductState.ARCHIVED:
            conditions.append(_P.archived_at.is_not(None))
        if query.category_id is not None:
            conditions.append(_P.category_id == query.category_id)
        if query.text:
            pattern = "%" + _escape_like(query.text.casefold()) + "%"
            conditions.append(
                or_(
                    func.lower(_P.sku).like(pattern, escape="\\"),
                    func.lower(_P.name).like(pattern, escape="\\"),
                )
            )
        if query.after is not None:
            conditions.append(func.lower(_P.sku).collate("C") > query.after)
        with self._read(tenant_id) as connection:
            rows = connection.execute(
                _product_select()
                .where(*conditions)
                .order_by(func.lower(_P.sku).collate("C"))
                .limit(query.limit + 1)
            ).all()
        items = tuple(_product(row) for row in rows[: query.limit])
        more = len(rows) > query.limit
        return ProductPage(items, items[-1].sku.casefold() if more and items else None)

    def get_product(self, tenant_id: UUID, product_id: UUID) -> CatalogueProduct:
        with self._read(tenant_id) as connection:
            return self._product_or_raise(connection, tenant_id, product_id)

    def create_product(
        self, context: TenantContext, sku: str, fields: ProductFields, max_active: int
    ) -> CatalogueProduct:
        tenant_id = context.tenant_id
        product_id = uuid4()
        try:
            with tenant_transaction(self._engine, tenant_id) as connection:
                self._lock_catalogue(connection, tenant_id)
                if self._active_count(connection, tenant_id) >= max_active:
                    raise CatalogueLimitReached(f"A business can have {max_active:,} products")
                category = self._category_for(connection, tenant_id, fields.category_id)
                connection.execute(
                    insert(_P).values(
                        id=product_id,
                        tenant_id=tenant_id,
                        sku=sku,
                        search_terms=_terms(sku, fields.name, category),
                        created_by=context.subject,
                        updated_by=context.subject,
                        **fields_as_dict(fields),
                    )
                )
                after = {"sku": sku, **fields_as_dict(fields)}
                self._record(connection, context, "product", product_id, "created", {}, after)
                return self._product_or_raise(connection, tenant_id, product_id)
        except IntegrityError as error:
            if _constraint(error) == "uq_catalogue_products_sku":
                raise SkuAlreadyExists(f"A product with SKU {sku} already exists") from None
            raise

    def update_product(
        self,
        context: TenantContext,
        product_id: UUID,
        fields: ProductFields,
        expected_version: int,
    ) -> CatalogueProduct:
        tenant_id = context.tenant_id
        with tenant_transaction(self._engine, tenant_id) as connection:
            current = self._locked(connection, tenant_id, product_id, expected_version)
            if current.fields.unit != fields.unit and self._has_entries(
                connection, tenant_id, product_id
            ):
                raise InvalidCatalogueEntry(
                    "unit", "The unit cannot change once a price or stock count exists"
                )
            category = self._category_for(connection, tenant_id, fields.category_id)
            before, after = fields_as_dict(current.fields), fields_as_dict(fields)
            connection.execute(
                update(_P)
                .where(_P.tenant_id == tenant_id, _P.id == product_id)
                .values(
                    **after,
                    search_terms=_terms(current.sku, fields.name, category),
                    row_version=_P.row_version + 1,
                    updated_at=func.now(),
                    updated_by=context.subject,
                )
            )
            self._record(connection, context, "product", product_id, "updated", before, after)
            return self._product_or_raise(connection, tenant_id, product_id)

    def set_archived(
        self,
        context: TenantContext,
        product_id: UUID,
        archived: bool,
        expected_version: int,
        max_active: int,
    ) -> CatalogueProduct:
        tenant_id = context.tenant_id
        with tenant_transaction(self._engine, tenant_id) as connection:
            if not archived:
                self._lock_catalogue(connection, tenant_id)
            current = self._locked(connection, tenant_id, product_id, expected_version)
            if current.active != archived:
                return current  # already in the wanted state; nothing to record
            if not archived and self._active_count(connection, tenant_id) >= max_active:
                raise CatalogueLimitReached(f"A business can have {max_active:,} products")
            connection.execute(
                update(_P)
                .where(_P.tenant_id == tenant_id, _P.id == product_id)
                .values(
                    archived_at=func.now() if archived else None,
                    archived_by=context.subject if archived else None,
                    row_version=_P.row_version + 1,
                    updated_at=func.now(),
                    updated_by=context.subject,
                )
            )
            action = "archived" if archived else "restored"
            self._record(connection, context, "product", product_id, action, {}, {})
            return self._product_or_raise(connection, tenant_id, product_id)

    def delete_product(self, context: TenantContext, product_id: UUID) -> None:
        tenant_id = context.tenant_id
        with tenant_transaction(self._engine, tenant_id) as connection:
            current = self._product_or_raise(connection, tenant_id, product_id, lock=True)
            if self._has_entries(connection, tenant_id, product_id) or any(
                check(connection, tenant_id, current.sku) for check in self._in_use
            ):
                raise ProductReferenced("This product has been priced, counted or quoted")
            connection.execute(delete(_P).where(_P.tenant_id == tenant_id, _P.id == product_id))
            before = {"sku": current.sku, **fields_as_dict(current.fields)}
            self._record(connection, context, "product", product_id, "deleted", before, {})

    # --- CatalogueStore: prices and stock ---------------------------------------------------

    def list_prices(self, tenant_id: UUID, product_id: UUID) -> Sequence[PriceEntry]:
        with self._read(tenant_id) as connection:
            self._product_or_raise(connection, tenant_id, product_id)
            rows = connection.execute(
                select(_PRICE)
                .where(_PRICE.tenant_id == tenant_id, _PRICE.product_id == product_id)
                .order_by(_PRICE.effective_from.desc(), _PRICE.revision.desc())
            ).all()
            return [_price(row) for row in rows]

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
        tenant_id = context.tenant_id

        def same(row: Row[Any]) -> bool:
            return (row.product_id, row.unit_price, row.currency, row.note) == (
                product_id,
                unit_price,
                currency,
                note,
            )

        def write(connection: Connection, product: CatalogueProduct) -> Row[Any]:
            revision = connection.execute(
                select(func.coalesce(func.max(_PRICE.revision), 0) + 1).where(
                    _PRICE.tenant_id == tenant_id,
                    _PRICE.product_id == product_id,
                    _PRICE.price_list == price_list,
                    _PRICE.currency == currency,
                )
            ).scalar_one()
            return connection.execute(
                insert(_PRICE)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    product_id=product_id,
                    price_list=price_list,
                    revision=revision,
                    currency=currency,
                    unit=product.fields.unit,
                    unit_price=unit_price,
                    effective_from=effective_from,
                    source=EntrySource.MANUAL.value,
                    note=note,
                    command_id=command_id,
                    created_by=context.subject,
                )
                .returning(*_PRICE.__table__.c)
            ).one()

        stored, replayed = self._append(
            tenant_id, product_id, command_id, _PRICE, "uq_catalogue_prices_command", same, write
        )
        return AddedEntry(_price(stored), replayed)

    def list_stock(self, tenant_id: UUID, product_id: UUID) -> Sequence[StockEntry]:
        with self._read(tenant_id) as connection:
            self._product_or_raise(connection, tenant_id, product_id)
            rows = connection.execute(
                select(_STOCK)
                .where(_STOCK.tenant_id == tenant_id, _STOCK.product_id == product_id)
                .order_by(_STOCK.as_of.desc(), _STOCK.recorded_at.desc())
            ).all()
            return [_stock(row) for row in rows]

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
        tenant_id = context.tenant_id

        def same(row: Row[Any]) -> bool:
            return (row.product_id, row.quantity, row.location_code, row.note) == (
                product_id,
                quantity,
                location,
                note,
            )

        def write(connection: Connection, product: CatalogueProduct) -> Row[Any]:
            return connection.execute(
                insert(_STOCK)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    product_id=product_id,
                    location_code=location,
                    quantity=quantity,
                    unit=product.fields.unit,
                    as_of=as_of,
                    source=EntrySource.MANUAL.value,
                    note=note,
                    command_id=command_id,
                    recorded_by=context.subject,
                )
                .returning(*_STOCK.__table__.c)
            ).one()

        stored, replayed = self._append(
            tenant_id, product_id, command_id, _STOCK, "uq_catalogue_stock_command", same, write
        )
        return AddedEntry(_stock(stored), replayed)

    def list_changes(self, tenant_id: UUID, entity_id: UUID) -> Sequence[ChangeRecord]:
        with self._read(tenant_id) as connection:
            rows = connection.execute(
                select(CatalogueChange)
                .where(
                    CatalogueChange.tenant_id == tenant_id,
                    CatalogueChange.entity_id == entity_id,
                )
                .order_by(CatalogueChange.created_at.desc(), CatalogueChange.id)
            ).all()
            return [
                ChangeRecord(
                    row.entity,
                    row.entity_id,
                    row.action,
                    dict(row.changes),
                    row.actor,
                    row.created_at,
                )  # fmt: skip
                for row in rows
            ]

    # --- CatalogueStore: categories ---------------------------------------------------------

    def list_categories(self, tenant_id: UUID) -> Sequence[Category]:
        in_use = (
            select(func.count())
            .where(_P.tenant_id == _C.tenant_id, _P.category_id == _C.id, _P.archived_at.is_(None))
            .scalar_subquery()
        )
        with self._read(tenant_id) as connection:
            rows = connection.execute(
                select(_C.id, _C.name, _C.archived_at, _C.row_version, in_use.label("products"))
                .where(_C.tenant_id == tenant_id)
                .order_by(_C.name.collate("C"))
            ).all()
        return [
            Category(row.id, row.name, row.archived_at, row.row_version, row.products)
            for row in rows
        ]

    def create_category(self, context: TenantContext, name: str) -> Category:
        category_id = uuid4()
        try:
            with tenant_transaction(self._engine, context.tenant_id) as connection:
                connection.execute(
                    insert(_C).values(
                        id=category_id,
                        tenant_id=context.tenant_id,
                        name=name,
                        created_by=context.subject,
                        updated_by=context.subject,
                    )
                )
                self._record(
                    connection, context, "category", category_id, "created", {}, {"name": name}
                )
        except IntegrityError as error:
            if _constraint(error) == "uq_catalogue_categories_name":
                raise CategoryNameTaken(f"A category named {name} already exists") from None
            raise
        return Category(category_id, name, None, 1, 0)

    def update_category(
        self,
        context: TenantContext,
        category_id: UUID,
        name: str | None,
        archived: bool | None,
        expected_version: int,
    ) -> Category:
        tenant_id = context.tenant_id
        try:
            with tenant_transaction(self._engine, tenant_id) as connection:
                row = connection.execute(
                    select(_C)
                    .where(_C.tenant_id == tenant_id, _C.id == category_id)
                    .with_for_update()
                ).one_or_none()
                if row is None:
                    raise CategoryNotFound("No such category")
                if row.row_version != expected_version:
                    raise VersionConflict("This category was changed by someone else")
                new_name = row.name if name is None else name
                was_archived = row.archived_at is not None
                now_archived = was_archived if archived is None else archived
                values: dict[str, Any] = {"name": new_name}
                if now_archived != was_archived:
                    values["archived_at"] = func.now() if now_archived else None
                connection.execute(
                    update(_C)
                    .where(_C.tenant_id == tenant_id, _C.id == category_id)
                    .values(
                        **values,
                        row_version=_C.row_version + 1,
                        updated_at=func.now(),
                        updated_by=context.subject,
                    )
                )
                refreshed = connection.execute(
                    select(_C).where(_C.tenant_id == tenant_id, _C.id == category_id)
                ).one()
                category = Category(
                    refreshed.id, refreshed.name, refreshed.archived_at, refreshed.row_version
                )
                self._refresh_terms(connection, tenant_id, category)
                before = {"name": row.name, "archived": was_archived}
                after = {"name": new_name, "archived": now_archived}
                action = (
                    "archived"
                    if now_archived and not was_archived
                    else "restored"
                    if was_archived and not now_archived
                    else "updated"
                )
                self._record(connection, context, "category", category_id, action, before, after)
        except IntegrityError as error:
            if _constraint(error) == "uq_catalogue_categories_name":
                raise CategoryNameTaken(f"A category named {name} already exists") from None
            raise
        return category

    def counts(
        self, tenant_id: UUID, currency: str | None, at: datetime, stale_before: datetime | None
    ) -> CatalogueCounts:
        active = and_(_P.tenant_id == tenant_id, _P.archived_at.is_(None))
        current = (
            select(_STOCK.product_id, _STOCK.quantity, _STOCK.as_of)
            .where(_STOCK.tenant_id == tenant_id)
            .order_by(
                _STOCK.product_id,
                _STOCK.location_code,
                _STOCK.as_of.desc(),
                _STOCK.recorded_at.desc(),
            )
            .distinct(_STOCK.product_id, _STOCK.location_code)
            .subquery()
        )
        priced = (
            exists().where(
                _PRICE.tenant_id == _P.tenant_id,
                _PRICE.product_id == _P.id,
                _PRICE.currency == currency,
                _PRICE.effective_from <= at,
            )
            if currency is not None
            else literal(False)
        )
        stale = (
            and_(current.c.quantity.is_not(None), current.c.as_of < stale_before)
            if stale_before is not None
            else literal(False)
        )
        with self._read(tenant_id) as connection:
            row = connection.execute(
                select(
                    func.count(),
                    func.count().filter(priced),
                    func.count().filter(stale),
                    func.count().filter(current.c.quantity.is_(None)),
                )
                .select_from(_P)
                .outerjoin(current, current.c.product_id == _P.id)
                .where(active)
            ).one()
        return CatalogueCounts(int(row[0]), int(row[1]), int(row[2]), int(row[3]))

    # --- helpers -----------------------------------------------------------------------------

    def _products_by(
        self, connection: Connection, tenant_id: UUID, *conditions: Any
    ) -> list[CatalogueProduct]:
        rows = connection.execute(
            _product_select().where(_P.tenant_id == tenant_id, *conditions)
        ).all()
        return [_product(row) for row in rows]

    def _product_or_raise(
        self, connection: Connection, tenant_id: UUID, product_id: UUID, *, lock: bool = False
    ) -> CatalogueProduct:
        statement = _product_select().where(_P.tenant_id == tenant_id, _P.id == product_id)
        if lock:
            statement = statement.with_for_update(of=_P)
        row = connection.execute(statement).first()
        if row is None:
            raise ProductNotFound("No such product")
        return _product(row)

    def _locked(
        self, connection: Connection, tenant_id: UUID, product_id: UUID, expected_version: int
    ) -> CatalogueProduct:
        current = self._product_or_raise(connection, tenant_id, product_id, lock=True)
        if current.row_version != expected_version:
            raise VersionConflict("This product was changed by someone else")
        return current

    @staticmethod
    def _lock_catalogue(connection: Connection, tenant_id: UUID) -> None:
        """Serialise the active-product limit check per business."""
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"catalogue-limit:{tenant_id}"},
        )

    @staticmethod
    def _active_count(connection: Connection, tenant_id: UUID) -> int:
        return int(
            connection.execute(
                select(func.count()).where(_P.tenant_id == tenant_id, _P.archived_at.is_(None))
            ).scalar_one()
        )

    @staticmethod
    def _category_for(
        connection: Connection, tenant_id: UUID, category_id: UUID | None
    ) -> Category | None:
        if category_id is None:
            return None
        row = connection.execute(
            select(_C).where(_C.tenant_id == tenant_id, _C.id == category_id)
        ).one_or_none()
        if row is None:
            raise CategoryNotFound("No such category")
        return Category(row.id, row.name, row.archived_at, row.row_version)

    @staticmethod
    def _has_entries(connection: Connection, tenant_id: UUID, product_id: UUID) -> bool:
        return bool(
            connection.execute(
                select(
                    exists().where(_PRICE.tenant_id == tenant_id, _PRICE.product_id == product_id)
                    | exists().where(_STOCK.tenant_id == tenant_id, _STOCK.product_id == product_id)
                )
            ).scalar_one()
        )

    def _refresh_terms(self, connection: Connection, tenant_id: UUID, category: Category) -> None:
        rows = connection.execute(
            select(_P.id, _P.sku, _P.name).where(
                _P.tenant_id == tenant_id, _P.category_id == category.category_id
            )
        ).all()
        for row in rows:
            connection.execute(
                update(_P)
                .where(_P.tenant_id == tenant_id, _P.id == row.id)
                .values(search_terms=_terms(row.sku, row.name, category))
            )

    def _append(
        self,
        tenant_id: UUID,
        product_id: UUID,
        command_id: UUID,
        table: type[Any],
        command_constraint: str,
        same: Callable[[Row[Any]], bool],
        write: Callable[[Connection, CatalogueProduct], Row[Any]],
    ) -> tuple[Row[Any], bool]:
        """Insert one history row for an active product, or return the stored replay."""
        try:
            with tenant_transaction(self._engine, tenant_id) as connection:
                product = self._product_or_raise(connection, tenant_id, product_id, lock=True)
                stored = self._by_command(connection, tenant_id, command_id, table)
                if stored is not None:
                    if not same(stored):
                        raise VersionConflict("This request ID was already used for other values")
                    return stored, True
                if not product.active:
                    raise ProductArchived("Restore the product before entering prices or stock")
                return write(connection, product), False
        except IntegrityError as error:
            if _constraint(error) != command_constraint:
                raise
        # The same request ID was stored for another product at the same moment.
        raise VersionConflict("This request ID was already used for other values")

    @staticmethod
    def _by_command(
        connection: Connection, tenant_id: UUID, command_id: UUID, table: type[Any]
    ) -> Row[Any] | None:
        model: Any = table
        return connection.execute(
            select(model).where(model.tenant_id == tenant_id, model.command_id == command_id)
        ).one_or_none()

    @staticmethod
    def _record(
        connection: Connection,
        context: TenantContext,
        entity: str,
        entity_id: UUID,
        action: str,
        before: Mapping[str, object],
        after: Mapping[str, object],
    ) -> None:
        connection.execute(
            insert(CatalogueChange).values(
                id=uuid4(),
                tenant_id=context.tenant_id,
                entity=entity,
                entity_id=entity_id,
                action=action,
                changes=field_changes(before, after),
                actor=context.subject,
            )
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
