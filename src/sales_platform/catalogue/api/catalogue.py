"""The business's own product list (phase 3, ADR 0020), under /api/v1/tenants/{t}/catalogue.

A non-member gets 404, like for an unknown business. Reads need catalogue:view (all staff);
changes need catalogue:manage (administrators, sales operators), the CSRF header and an
editable product source. Decimals travel as strings, times as ISO 8601 with an offset.

GET    /summary                          source, counts, units, currency, limits
GET    /products?q&category_id&state&limit&after    one page, with current price and stock
POST   /products                         201; 409 SKU taken / limit / read-only; 422 {field}
GET    /products/{id}   PATCH /products/{id} (expected_version; 409 version or unit locked)
POST   /products/{id}/archive   /restore   {expected_version}
DELETE /products/{id}                    204; 409 product_referenced (archive instead)
GET    /products/{id}/prices  POST (201, 200 on a replayed command_id)
GET    /products/{id}/stock   POST (201, 200 on a replayed command_id)
GET    /products/{id}/changes
GET    /categories  POST /categories  PATCH /categories/{id} {name?, archived?, expected_version}
"""

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from sales_platform.accounts.ports.accounts import NotAllowed
from sales_platform.catalogue.application.catalogue_admin import (
    CatalogueSummary,
    CurrencyNotSet,
    PriceHistoryItem,
    ProductDetail,
    ProductListItem,
    ProductService,
    SourceNotEditable,
    StockView,
)
from sales_platform.catalogue.domain.catalogue_admin import (
    Category,
    ChangeRecord,
    EffectivePrice,
    InvalidCatalogueEntry,
    PriceEntry,
    ProductFields,
    StockEntry,
)
from sales_platform.catalogue.ports.catalogue_admin import (
    AddedEntry,
    CatalogueLimitReached,
    CatalogueUnavailable,
    CategoryNameTaken,
    CategoryNotFound,
    ProductArchived,
    ProductNotFound,
    ProductQuery,
    ProductReferenced,
    ProductState,
    SkuAlreadyExists,
    VersionConflict,
)
from sales_platform.platform.api.auth import PortalAuth
from sales_platform.platform.domain.tenancy import TenantContext

logger = logging.getLogger(__name__)
_UNAVAILABLE = "Temporarily unavailable; try again"


def _conflict(code: str, error: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, {"code": code, "message": str(error)})


def guarded[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except NotAllowed as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error) or "Not permitted") from None
    except (ProductNotFound, CategoryNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from None
    except InvalidCatalogueEntry as error:
        detail = {"field": error.field, "message": str(error)}
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from None
    except SkuAlreadyExists as error:
        raise _conflict("sku_taken", error) from None
    except CategoryNameTaken as error:
        raise _conflict("category_name_taken", error) from None
    except VersionConflict as error:
        raise _conflict("version_conflict", error) from None
    except ProductReferenced as error:
        raise _conflict("product_referenced", error) from None
    except CatalogueLimitReached as error:
        raise _conflict("limit_reached", error) from None
    except SourceNotEditable as error:
        raise _conflict("source_not_editable", error) from None
    except ProductArchived as error:
        raise _conflict("product_archived", error) from None
    except CurrencyNotSet as error:
        raise _conflict("currency_not_set", error) from None
    except CatalogueUnavailable:
        logger.exception("Catalogue request failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from None


def _decimal(value: str | None, field: str) -> Decimal | None:
    """Numbers arrive as strings (never floats) and are parsed exactly."""
    if value is None:
        return None
    try:
        result = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise InvalidCatalogueEntry(field, "Enter a number") from None
    if not result.is_finite():
        raise InvalidCatalogueEntry(field, "Enter a number")
    return result


def _required(value: str, field: str) -> Decimal:
    result = _decimal(value, field)
    assert result is not None
    return result


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- output ----------------------------------------------------------------------------------
class CategoryRefOut(BaseModel):
    category_id: UUID
    name: str
    archived: bool


class PriceOut(BaseModel):
    price_id: UUID
    unit_price: str
    currency: str
    unit: str
    price_list: str
    revision: int
    effective_from: datetime
    effective_to: datetime | None = None
    note: str | None
    created_at: datetime
    created_by: str
    state: Literal["current", "scheduled", "past"] | None = None


class StockOut(BaseModel):
    stock_id: UUID
    quantity: str | None  # None: not tracked
    unit: str
    location: str
    as_of: datetime
    source: str
    note: str | None
    recorded_at: datetime
    recorded_by: str
    stale: bool | None = None


class ProductSummaryOut(BaseModel):
    product_id: UUID
    sku: str
    name: str
    unit: str
    category: CategoryRefOut | None
    archived_at: datetime | None
    row_version: int
    current_price: PriceOut | None
    stock: StockOut | None


class ProductOut(ProductSummaryOut):
    description: str | None
    minimum_quantity: str
    quantity_step: str
    scheduled_price: PriceOut | None
    created_at: datetime
    updated_at: datetime


class ProductPageOut(BaseModel):
    items: list[ProductSummaryOut]
    next_after: str | None


class SourceOut(BaseModel):
    provider_key: str
    display_name: str
    editable: bool


class SummaryOut(BaseModel):
    source: SourceOut | None
    active_products: int
    priced_products: int
    products_without_price: int
    stale_stock: int
    unknown_stock: int
    max_active_products: int
    units: list[str]
    currency: str | None
    minor_units: int | None
    max_stock_age_seconds: int | None
    time_zone: str | None


class CategoryOut(BaseModel):
    category_id: UUID
    name: str
    archived_at: datetime | None
    row_version: int
    active_products: int


class ChangeOut(BaseModel):
    entity: str
    action: str
    changes: dict[str, list[str | None]]
    actor: str
    created_at: datetime


# --- input -----------------------------------------------------------------------------------
class ProductIn(_Strict):
    sku: str = Field(max_length=100)
    name: str = Field(max_length=400)
    unit: str = Field(max_length=32)
    description: str | None = Field(default=None, max_length=4000)
    category_id: UUID | None = None
    minimum_quantity: str = Field(default="1", max_length=40)
    quantity_step: str = Field(default="1", max_length=40)


class ProductPatchIn(_Strict):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=400)
    unit: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=4000)
    category_id: UUID | None = None
    minimum_quantity: str | None = Field(default=None, max_length=40)
    quantity_step: str | None = Field(default=None, max_length=40)


class VersionIn(_Strict):
    expected_version: int = Field(ge=1)


class PriceIn(_Strict):
    unit_price: str = Field(max_length=40)
    effective_from: AwareDatetime | None = None  # None: now
    note: str | None = Field(default=None, max_length=1000)
    command_id: UUID


class StockIn(_Strict):
    quantity: str | None = Field(max_length=40)  # null: not tracked
    as_of: AwareDatetime | None = None  # None: now
    note: str | None = Field(default=None, max_length=1000)
    command_id: UUID


class CategoryIn(_Strict):
    name: str = Field(max_length=200)


class CategoryPatchIn(_Strict):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


# --- mapping ---------------------------------------------------------------------------------
def _price(entry: PriceEntry, effective_to: datetime | None = None) -> PriceOut:
    return PriceOut(
        price_id=entry.price_id,
        unit_price=str(entry.unit_price),
        currency=entry.currency,
        unit=entry.unit,
        price_list=entry.price_list,
        revision=entry.revision,
        effective_from=entry.effective_from,
        effective_to=effective_to,
        note=entry.note,
        created_at=entry.created_at,
        created_by=entry.created_by,
    )


def _current(price: EffectivePrice | None) -> PriceOut | None:
    return None if price is None else _price(price.entry, price.effective_to)


def _stock(entry: StockEntry, stale: bool | None = None) -> StockOut:
    return StockOut(
        stock_id=entry.stock_id,
        quantity=None if entry.quantity is None else str(entry.quantity),
        unit=entry.unit,
        location=entry.location,
        as_of=entry.as_of,
        source=entry.source.value,
        note=entry.note,
        recorded_at=entry.recorded_at,
        recorded_by=entry.recorded_by,
        stale=stale,
    )


def _stock_view(view: StockView | None) -> StockOut | None:
    return None if view is None else _stock(view.entry, view.stale)


def _summary_item(item: ProductListItem) -> ProductSummaryOut:
    product = item.product
    category = product.category
    return ProductSummaryOut(
        product_id=product.product_id,
        sku=product.sku,
        name=product.fields.name,
        unit=product.fields.unit,
        category=None
        if category is None
        else CategoryRefOut(
            category_id=category.category_id,
            name=category.name,
            archived=category.archived_at is not None,
        ),
        archived_at=product.archived_at,
        row_version=product.row_version,
        current_price=_current(item.current_price),
        stock=_stock_view(item.stock),
    )


def _product(detail: ProductDetail) -> ProductOut:
    product = detail.product
    summary = _summary_item(ProductListItem(product, detail.current_price, detail.stock))
    return ProductOut(
        **summary.model_dump(),
        description=product.fields.description,
        minimum_quantity=str(product.fields.minimum_quantity),
        quantity_step=str(product.fields.quantity_step),
        scheduled_price=None if detail.scheduled_price is None else _price(detail.scheduled_price),
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


def _category(category: Category) -> CategoryOut:
    return CategoryOut(
        category_id=category.category_id,
        name=category.name,
        archived_at=category.archived_at,
        row_version=category.row_version,
        active_products=category.active_products,
    )


def _summary(summary: CatalogueSummary) -> SummaryOut:
    counts = summary.counts
    source = summary.source
    return SummaryOut(
        source=None
        if source is None
        else SourceOut(
            provider_key=source.provider_key,
            display_name=source.display_name,
            editable=source.editable,
        ),
        active_products=counts.active_products,
        priced_products=counts.priced_products,
        products_without_price=counts.active_products - counts.priced_products,
        stale_stock=counts.stale_stock,
        unknown_stock=counts.unknown_stock,
        max_active_products=summary.max_active_products,
        units=list(summary.units),
        currency=summary.currency,
        minor_units=summary.minor_units,
        max_stock_age_seconds=None
        if summary.max_stock_age is None
        else int(summary.max_stock_age.total_seconds()),
        time_zone=summary.time_zone,
    )


def _history(item: PriceHistoryItem) -> PriceOut:
    out = _price(item.entry)
    out.state = item.state.value
    return out


def _change(change: ChangeRecord) -> ChangeOut:
    return ChangeOut(
        entity=change.entity,
        action=change.action,
        changes={name: list(values) for name, values in change.changes.items()},
        actor=change.actor,
        created_at=change.created_at,
    )


def _added[T](response: Response, added: AddedEntry[T]) -> T:
    """201 for a new entry; 200 when the same command_id was already stored."""
    response.status_code = status.HTTP_200_OK if added.replayed else status.HTTP_201_CREATED
    return added.entry


def build_catalogue_router(auth: PortalAuth, products: ProductService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tenants/{tenant_id}/catalogue", tags=["catalogue"])
    Context = Annotated[TenantContext, Depends(auth.tenant_context())]

    @router.get("/summary", response_model=SummaryOut)
    def summary(context: Context) -> SummaryOut:
        return _summary(guarded(lambda: products.summary(context)))

    @router.get("/products", response_model=ProductPageOut)
    def list_products(
        context: Context,
        q: Annotated[str | None, Query(max_length=100)] = None,
        category_id: UUID | None = None,
        state: ProductState = ProductState.ACTIVE,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        after: Annotated[str | None, Query(max_length=100)] = None,
    ) -> ProductPageOut:
        query = ProductQuery(q.strip() if q else None, category_id, state, limit, after)
        page = guarded(lambda: products.list_products(context, query))
        return ProductPageOut(
            items=[_summary_item(item) for item in page.items], next_after=page.next_after
        )

    @router.post("/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
    def create_product(body: ProductIn, context: Context) -> ProductOut:
        def create() -> ProductDetail:
            fields = ProductFields(
                name=body.name,
                unit=body.unit,
                description=body.description,
                category_id=body.category_id,
                minimum_quantity=_required(body.minimum_quantity, "minimum_quantity"),
                quantity_step=_required(body.quantity_step, "quantity_step"),
            )
            return products.create_product(context, body.sku, fields)

        return _product(guarded(create))

    @router.get("/products/{product_id}", response_model=ProductOut)
    def get_product(product_id: UUID, context: Context) -> ProductOut:
        return _product(guarded(lambda: products.get_product(context, product_id)))

    @router.patch("/products/{product_id}", response_model=ProductOut)
    def update_product(product_id: UUID, body: ProductPatchIn, context: Context) -> ProductOut:
        def update() -> ProductDetail:
            given = body.model_dump(exclude_unset=True, exclude={"expected_version"})
            for name in ("minimum_quantity", "quantity_step"):
                if name in given:
                    given[name] = _required(given[name], name)
            return products.update_product(context, product_id, given, body.expected_version)

        return _product(guarded(update))

    @router.post("/products/{product_id}/archive", response_model=ProductOut)
    def archive_product(product_id: UUID, body: VersionIn, context: Context) -> ProductOut:
        return _product(
            guarded(lambda: products.archive_product(context, product_id, body.expected_version))
        )

    @router.post("/products/{product_id}/restore", response_model=ProductOut)
    def restore_product(product_id: UUID, body: VersionIn, context: Context) -> ProductOut:
        return _product(
            guarded(lambda: products.restore_product(context, product_id, body.expected_version))
        )

    @router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_product(product_id: UUID, context: Context) -> None:
        guarded(lambda: products.delete_product(context, product_id))

    @router.get("/products/{product_id}/prices", response_model=list[PriceOut])
    def price_history(product_id: UUID, context: Context) -> list[PriceOut]:
        return [
            _history(item) for item in guarded(lambda: products.price_history(context, product_id))
        ]

    @router.post(
        "/products/{product_id}/prices",
        response_model=PriceOut,
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "The same command_id was already stored"}},
    )
    def add_price(
        product_id: UUID, body: PriceIn, context: Context, response: Response
    ) -> PriceOut:
        added = guarded(
            lambda: products.add_price(
                context,
                product_id,
                _required(body.unit_price, "unit_price"),
                body.effective_from,
                body.note,
                body.command_id,
            )
        )
        return _price(_added(response, added))

    @router.get("/products/{product_id}/stock", response_model=list[StockOut])
    def stock_history(product_id: UUID, context: Context) -> list[StockOut]:
        return [
            _stock(entry) for entry in guarded(lambda: products.stock_history(context, product_id))
        ]

    @router.post(
        "/products/{product_id}/stock",
        response_model=StockOut,
        status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "The same command_id was already stored"}},
    )
    def record_stock(
        product_id: UUID, body: StockIn, context: Context, response: Response
    ) -> StockOut:
        added = guarded(
            lambda: products.record_stock(
                context,
                product_id,
                _decimal(body.quantity, "quantity"),
                body.as_of,
                body.note,
                body.command_id,
            )
        )
        return _stock(_added(response, added))

    @router.get("/products/{product_id}/changes", response_model=list[ChangeOut])
    def product_changes(product_id: UUID, context: Context) -> list[ChangeOut]:
        def load() -> list[ChangeRecord]:
            products.get_product(context, product_id)  # 404 for another business's ID
            return list(products.changes(context, product_id))

        return [_change(change) for change in guarded(load)]

    @router.get("/categories", response_model=list[CategoryOut])
    def list_categories(context: Context) -> list[CategoryOut]:
        return [_category(c) for c in guarded(lambda: products.list_categories(context))]

    @router.post("/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
    def create_category(body: CategoryIn, context: Context) -> CategoryOut:
        return _category(guarded(lambda: products.create_category(context, body.name)))

    @router.patch("/categories/{category_id}", response_model=CategoryOut)
    def update_category(category_id: UUID, body: CategoryPatchIn, context: Context) -> CategoryOut:
        return _category(
            guarded(
                lambda: products.update_category(
                    context, category_id, body.name, body.archived, body.expected_version
                )
            )
        )

    return router
