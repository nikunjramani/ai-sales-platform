"""Our own product catalogue: the rules for what staff may type in (phase 3, ADR 0020).

A business without an ERP keeps its products, prices and stock counts with us. These rules
decide what a valid entry is; the user answered every open point on 2026-10-28 (phase 3 plan,
Q1-Q23). In short:
- a product needs a SKU, a name and one of the units the enquiry reader understands (Q1, Q3);
- a SKU is 1-64 letters, digits or "- _ . /", starts with a letter or digit, and never changes
  (Q2); the minimum quantity is a multiple of the step (Q16); the unit is locked once a price
  or stock count exists (Q17);
- prices are > 0, exclude tax, use the business currency, have up to 6 decimals and start now
  or later, never in the past (Q4-Q8). Each change is a new row; nothing is overwritten;
- stock counts are optional, up to 3 decimals, "as of" a time not in the future; "not tracked"
  (no number) is allowed (Q21).

Price selection (`effective_price`): the row with the latest start not after the moment; on a
tie the higher revision wins, so a scheduled price is cancelled by entering another one for the
same start. Stock selection (`current_stock`): the latest "as of"; ties go to the later entry.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sales_platform.domain.requirements import UNIT_ALIASES

# Only units a customer's words can be matched to (Q3); new units extend UNIT_ALIASES first.
CATALOGUE_UNITS: frozenset[str] = frozenset(UNIT_ALIASES.values())
LOCAL_PRICE_LIST = "standard"  # one price list per business (Q23)
DEFAULT_LOCATION = "MAIN"  # one stock location per business (Q21)
MAX_ACTIVE_PRODUCTS = 10_000  # Q13
PRICE_DECIMALS = 6  # Q4
QUANTITY_DECIMALS = 3  # Q4
MAX_PRICE = Decimal(10) ** 12
MAX_QUANTITY = Decimal(10) ** 9
MAX_STOCK = Decimal(10) ** 12
MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_CATEGORY = 100
MAX_NOTE = 500
# Allowance for clocks that differ slightly between the browser, the API and the database.
CLOCK_TOLERANCE = timedelta(minutes=5)

_SKU = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}")


class InvalidCatalogueEntry(ValueError):
    """An entry breaks a catalogue rule; `field` names the form field to show it on."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name


class EntrySource(StrEnum):
    """Where a row came from: typed in, or copied from the business's ERP or shop (phase 6)."""

    MANUAL = "manual"
    SYNC = "sync"


class PriceState(StrEnum):
    CURRENT = "current"
    SCHEDULED = "scheduled"
    PAST = "past"


@dataclass(frozen=True)
class ProductFields:
    """The editable part of a product (everything but the SKU)."""

    name: str
    unit: str
    description: str | None = None
    category_id: UUID | None = None
    minimum_quantity: Decimal = Decimal(1)
    quantity_step: Decimal = Decimal(1)


EDITABLE_FIELDS: frozenset[str] = frozenset(ProductFields.__dataclass_fields__)


@dataclass(frozen=True)
class Category:
    category_id: UUID
    name: str
    archived_at: datetime | None
    row_version: int
    active_products: int = 0


@dataclass(frozen=True)
class CatalogueProduct:
    product_id: UUID
    sku: str
    fields: ProductFields
    category: Category | None
    archived_at: datetime | None
    row_version: int
    created_at: datetime
    updated_at: datetime

    @property
    def active(self) -> bool:
        return self.archived_at is None


@dataclass(frozen=True)
class PriceEntry:
    price_id: UUID
    product_id: UUID
    price_list: str
    revision: int
    currency: str
    unit: str
    unit_price: Decimal
    effective_from: datetime
    source: EntrySource
    note: str | None
    created_at: datetime
    created_by: str


@dataclass(frozen=True)
class StockEntry:
    stock_id: UUID
    product_id: UUID
    location: str
    quantity: Decimal | None  # None: "not tracked", i.e. unknown
    unit: str
    as_of: datetime
    source: EntrySource
    note: str | None
    recorded_at: datetime
    recorded_by: str


@dataclass(frozen=True)
class EffectivePrice:
    entry: PriceEntry
    effective_to: datetime | None  # start of the next price, if one is scheduled


@dataclass(frozen=True)
class ChangeRecord:
    entity: str  # "product" | "category"
    entity_id: UUID
    action: str  # created | updated | archived | restored | deleted
    changes: Mapping[str, list[str | None]]
    actor: str
    created_at: datetime


def _fail(field_name: str, message: str) -> InvalidCatalogueEntry:
    return InvalidCatalogueEntry(field_name, message)


def check_sku(sku: str) -> str:
    if not isinstance(sku, str) or not _SKU.fullmatch(sku):
        raise _fail(
            "sku",
            "SKU is 1-64 letters, digits or - _ . / and starts with a letter or digit",
        )
    return sku


def _text(value: str | None, field_name: str, max_length: int, *, required: bool) -> str | None:
    cleaned = value.strip() if isinstance(value, str) else None
    if not cleaned:
        if required:
            raise _fail(field_name, f"{field_name.capitalize()} is required")
        return None
    if len(cleaned) > max_length or not cleaned.isprintable():
        raise _fail(field_name, f"Use at most {max_length} printable characters")
    return cleaned


def check_category_name(name: str) -> str:
    result = _text(name, "name", MAX_CATEGORY, required=True)
    assert result is not None
    return result


def check_note(note: str | None) -> str | None:
    return _text(note, "note", MAX_NOTE, required=False)


def _decimal(value: object, field_name: str, places: int, below: Decimal) -> Decimal:
    """A positive-or-zero finite Decimal with at most `places` decimals.

    Extra trailing zeros are dropped ("1.5000" -> "1.500"); anything else is refused rather
    than rounded.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _fail(field_name, "Enter a number")
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    if -exponent > places:
        quantum = Decimal(1).scaleb(-places)
        shortened = value.quantize(quantum)
        if shortened != value:
            raise _fail(field_name, f"Use at most {places} decimal places")
        value = shortened
    if value < 0 or value >= below:
        raise _fail(field_name, f"Enter a number from 0 to below {below:,}")
    return value


def check_product_fields(fields: ProductFields) -> ProductFields:
    name = _text(fields.name, "name", MAX_NAME, required=True)
    assert name is not None
    if fields.unit not in CATALOGUE_UNITS:
        raise _fail("unit", f"Unit must be one of: {', '.join(sorted(CATALOGUE_UNITS))}")
    minimum = _decimal(fields.minimum_quantity, "minimum_quantity", QUANTITY_DECIMALS, MAX_QUANTITY)
    step = _decimal(fields.quantity_step, "quantity_step", QUANTITY_DECIMALS, MAX_QUANTITY)
    if minimum <= 0:
        raise _fail("minimum_quantity", "Minimum quantity must be greater than 0")
    if step <= 0:
        raise _fail("quantity_step", "Quantity step must be greater than 0")
    if minimum % step != 0:
        raise _fail("minimum_quantity", "Minimum quantity must be a multiple of the step")
    return ProductFields(
        name=name,
        unit=fields.unit,
        description=_text(fields.description, "description", MAX_DESCRIPTION, required=False),
        category_id=fields.category_id,
        minimum_quantity=minimum,
        quantity_step=step,
    )


def check_unit_change(before: ProductFields, after: ProductFields, *, has_entries: bool) -> None:
    """Prices and stock are stored per unit, so the unit is locked once either exists (Q17)."""
    if before.unit != after.unit and has_entries:
        raise _fail("unit", "The unit cannot change once a price or stock count exists")


def normalise_price(amount: Decimal, minor_units: int | None) -> Decimal:
    """Never fewer decimals than the currency's minor units: 4250 -> 4250.00 for INR.

    The stored text is what quotations, PDFs and the acceptance comparison show, so it is
    fixed once here and never re-scaled later.
    """
    if minor_units is None:
        return amount
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)
    if -exponent < minor_units:
        return amount.quantize(Decimal(1).scaleb(-minor_units))
    return amount


def check_price(
    unit_price: Decimal,
    effective_from: datetime | None,
    *,
    now: datetime,
    minor_units: int | None,
) -> tuple[Decimal, datetime]:
    """The cleaned price and its start ("now" when not given). Never in the past (Q8)."""
    amount = _decimal(unit_price, "unit_price", PRICE_DECIMALS, MAX_PRICE)
    if amount <= 0:
        raise _fail("unit_price", "Price must be greater than 0")
    start = now if effective_from is None else effective_from
    if start.tzinfo is None or start.utcoffset() is None:
        raise _fail("effective_from", "Start time needs a time zone")
    if start < now - CLOCK_TOLERANCE:
        raise _fail("effective_from", "A price cannot start in the past")
    return normalise_price(amount, minor_units), start


def check_stock(
    quantity: Decimal | None, as_of: datetime | None, *, now: datetime
) -> tuple[Decimal | None, datetime]:
    """The cleaned count and its "as of" time ("now" when not given); never in the future."""
    if quantity is not None:
        quantity = _decimal(quantity, "quantity", QUANTITY_DECIMALS, MAX_STOCK)
    moment = now if as_of is None else as_of
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise _fail("as_of", "The time needs a time zone")
    if moment > now + CLOCK_TOLERANCE:
        raise _fail("as_of", "A stock count cannot be in the future")
    return quantity, moment


def effective_price(entries: Sequence[PriceEntry], at: datetime) -> EffectivePrice | None:
    """The price in force at `at` among one product's entries for one list and currency."""
    started = [entry for entry in entries if entry.effective_from <= at]
    if not started:
        return None
    chosen = max(started, key=lambda entry: (entry.effective_from, entry.revision))
    later = [entry.effective_from for entry in entries if entry.effective_from > at]
    return EffectivePrice(chosen, min(later, default=None))


def scheduled_price(entries: Sequence[PriceEntry], at: datetime) -> PriceEntry | None:
    """The next price that will take over after `at` (the winner for the earliest start)."""
    upcoming = [entry for entry in entries if entry.effective_from > at]
    if not upcoming:
        return None
    start = min(entry.effective_from for entry in upcoming)
    return max(
        (entry for entry in upcoming if entry.effective_from == start),
        key=lambda entry: entry.revision,
    )


def price_state(entry: PriceEntry, entries: Sequence[PriceEntry], at: datetime) -> PriceState:
    if entry.effective_from > at:
        return PriceState.SCHEDULED
    current = effective_price(entries, at)
    if current is not None and current.entry.price_id == entry.price_id:
        return PriceState.CURRENT
    return PriceState.PAST


def current_stock(entries: Sequence[StockEntry], location: str) -> StockEntry | None:
    here = [entry for entry in entries if entry.location == location]
    return max(here, key=lambda entry: (entry.as_of, entry.recorded_at), default=None)


def stock_is_stale(as_of: datetime, max_age: timedelta, now: datetime) -> bool:
    """The same rule the price calculator applies to stock (domain/pricing.py)."""
    return now - as_of > max_age


def _text_value(value: object) -> str | None:
    return None if value is None else str(value)


def field_changes(
    before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, list[str | None]]:
    """{"field": [old, new]} for every field whose value changed, as text for the audit log."""
    return {
        name: [_text_value(before.get(name)), _text_value(after.get(name))]
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    }


def fields_as_dict(fields: ProductFields) -> dict[str, object]:
    return {name: getattr(fields, name) for name in sorted(EDITABLE_FIELDS)}
