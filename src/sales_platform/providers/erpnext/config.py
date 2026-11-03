"""A business's ERPNext connection settings, asked for in the portal's Enable form (phase 6).

The form is this model's JSON schema (the portal renders any provider's form the same way).
The API key and secret go to the SecretStore; everything else is the connection's public
config. Lists are typed comma-separated, because the generic form has text fields only.
Choices made with the user (2026-10-29, ADR 0023): one selling price list, the chosen
warehouses summed, "available" stock by default, enabled sales items of the chosen item groups,
tax-inclusive prices refused, the sync interval set per business.
"""

from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from sales_platform.catalogue.domain.catalogue_sync import (
    DEFAULT_SYNC_MINUTES,
    MAX_SYNC_MINUTES,
    MIN_SYNC_MINUTES,
    SYNC_MINUTES_KEY,
    UnitMap,
)

MAX_LIST_ITEMS = 50


class StockMeaning(StrEnum):
    AVAILABLE = "available"  # actual minus reserved (default, user answer)
    ON_HAND = "on_hand"  # actual quantity only


def split_list(text: str) -> tuple[str, ...]:
    """'Stores - AC, Finished Goods - AC' -> ('Stores - AC', 'Finished Goods - AC')."""
    items = tuple(dict.fromkeys(" ".join(part.split()) for part in text.split(",")))
    return tuple(item for item in items if item)


class ErpnextForm(BaseModel):
    """What the administrator types when enabling ERPNext."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str = Field(
        title="ERPNext address",
        description="For example https://yourcompany.erpnext.com",
        max_length=200,
    )
    api_key: SecretStr = Field(title="API key", description="From the API user's settings")
    api_secret: SecretStr = Field(title="API secret", description="Shown once in ERPNext")
    price_list: str = Field(
        title="Selling price list",
        description="Used for every quotation, e.g. Standard Selling",
        min_length=1,
        max_length=140,
    )
    warehouses: str = Field(
        title="Warehouses that count for stock",
        description="Comma-separated; their stock is added up",
        min_length=1,
        max_length=2000,
    )
    stock_meaning: StockMeaning = Field(
        default=StockMeaning.AVAILABLE,
        title="In stock means",
        description="available = on hand minus reserved for existing orders",
    )
    item_groups: str = Field(
        default="",
        title="Item groups customers may order",
        description="Comma-separated; empty means every group",
        max_length=2000,
    )
    prices_include_tax: bool = Field(
        default=False,
        title="Prices in this list include tax",
        description="Tax-inclusive prices are not used for quotations",
    )
    unit_map: str = Field(
        default="",
        title="Unit names",
        description="Optional renames, e.g. Nos=each, Kgs=kg",
        max_length=1000,
    )
    sync_minutes: int = Field(
        default=DEFAULT_SYNC_MINUTES,
        ge=MIN_SYNC_MINUTES,
        le=MAX_SYNC_MINUTES,
        title="Copy the catalogue every (minutes)",
    )
    # Orders (phase 7): off until the business turns it on (Q16, ADR 0024).
    write_orders: bool = Field(
        default=False,
        title="Write orders to ERPNext",
        description="Try this with a test company first",
    )
    default_customer: str = Field(
        default="",
        title="ERPNext customer for orders",
        description="Used when staff do not choose one",
        max_length=140,
    )
    submit_orders: bool = Field(
        default=False,
        title="Submit orders in ERPNext",
        description="Off: orders arrive as drafts for your team to submit",
    )
    delivery_days: int = Field(
        default=7,
        ge=1,
        le=365,
        title="Days to delivery date in ERPNext",
        description="ERPNext needs a delivery date; it is never shown to customers",
    )

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise ValueError("Enter the full address, starting with https://")
        if parts.query or parts.fragment or parts.username:
            raise ValueError("Enter only the address, without anything after it")
        return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"

    @field_validator("warehouses", "item_groups")
    @classmethod
    def _list(cls, value: str) -> str:
        items = split_list(value)
        if len(items) > MAX_LIST_ITEMS:
            raise ValueError(f"At most {MAX_LIST_ITEMS} names")
        return ", ".join(items)

    @field_validator("unit_map")
    @classmethod
    def _units(cls, value: str) -> str:
        UnitMap.parse(value)
        return value

    @model_validator(mode="after")
    def _orders(self) -> "ErpnextForm":
        if not split_list(self.warehouses):
            raise ValueError("Name at least one warehouse")
        if self.write_orders and not self.default_customer:
            raise ValueError("Writing orders needs the ERPNext customer for orders")
        return self


class ErpnextSettings(BaseModel):
    """The public part, stored in the connection's config (no key or secret)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    base_url: str
    price_list: str
    warehouses: tuple[str, ...]
    stock_meaning: StockMeaning = StockMeaning.AVAILABLE
    item_groups: tuple[str, ...] = ()
    prices_include_tax: bool = False
    unit_map: str = ""
    sync_minutes: int = DEFAULT_SYNC_MINUTES
    write_orders: bool = False
    default_customer: str = ""
    submit_orders: bool = False
    delivery_days: int = 7

    @classmethod
    def from_form(cls, form: ErpnextForm) -> "ErpnextSettings":
        return cls(
            base_url=form.base_url,
            price_list=form.price_list,
            warehouses=split_list(form.warehouses),
            stock_meaning=form.stock_meaning,
            item_groups=split_list(form.item_groups),
            prices_include_tax=form.prices_include_tax,
            unit_map=form.unit_map,
            sync_minutes=form.sync_minutes,
            write_orders=form.write_orders,
            default_customer=form.default_customer,
            submit_orders=form.submit_orders,
            delivery_days=form.delivery_days,
        )

    def as_config(self) -> dict[str, object]:
        # sync_minutes at the top is the generic key the sync job reads (any synced source).
        return {"erpnext": self.model_dump(mode="json"), SYNC_MINUTES_KEY: self.sync_minutes}

    @property
    def units(self) -> UnitMap:
        return UnitMap.parse(self.unit_map)
