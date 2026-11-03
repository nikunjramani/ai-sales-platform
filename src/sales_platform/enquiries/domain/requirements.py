"""What a customer asked for, as interpreted by a model, and the checks that keep it honest.

A model may only *read*: it reports each requested item together with a verbatim quote from
the customer's text. unverified_items() then checks that the quote really exists and contains
the product words and the quantity, so invented items or numbers are caught. In a conversation
the quantity may come from a later message ("cables price?" ... "10 meter"): it is then quoted
separately (quantity_quote) and must be inside one customer message (phase 4).
Units are normalised with a fixed table; nothing is converted or guessed.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sales_platform.catalogue.domain.catalogue import Product
from sales_platform.catalogue.domain.units import normalise_unit
from sales_platform.platform.domain.validation import require_decimal, require_text

MAX_ITEMS = 50


@dataclass(frozen=True)
class RequestedLine:
    """What the customer asked for. Quantity problems become issues so they can be clarified."""

    sku: str
    quantity: Decimal
    unit: str

    def __post_init__(self) -> None:
        require_text(self.sku, "sku", 64)
        require_decimal(self.quantity, "quantity")
        require_text(self.unit, "unit", 32)


def _plain(text: str) -> str:
    """Case- and whitespace-insensitive form used for verbatim checks."""
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class RequestedItem:
    """One item as read by the model. All fields must be supported by source_quote."""

    source_quote: str
    product_text: str
    quantity: Decimal | None
    unit: str | None
    # The customer's words with the quantity, when they are in another message than the product.
    quantity_quote: str | None = None

    def __post_init__(self) -> None:
        require_text(self.source_quote, "source_quote", 500)
        require_text(self.product_text, "product_text", 200)
        if self.quantity_quote is not None:
            require_text(self.quantity_quote, "quantity_quote", 200)
        if self.quantity is not None:
            require_decimal(self.quantity, "quantity")
        if self.unit is not None:
            require_text(self.unit, "unit", 32)


@dataclass(frozen=True)
class ExtractedRequirements:
    items: tuple[RequestedItem, ...]
    delivery_location: str | None = None
    requested_delivery: str | None = None  # kept as the customer's words; never a promise

    def __post_init__(self) -> None:
        if len(self.items) > MAX_ITEMS:
            raise ValueError(f"At most {MAX_ITEMS} items can be extracted")


def _number_appears(quantity: Decimal, quote: str) -> bool:
    numbers = {Decimal(match) for match in re.findall(r"\d+(?:\.\d+)?", quote.replace(",", ""))}
    return quantity in numbers


def unverified_items(
    requirements: ExtractedRequirements,
    sources: Sequence[str],
    customer_messages: Sequence[str] | None = None,
) -> list[int]:
    """Indexes of items whose quote, product words or quantity are not in the sources.

    sources are the customer's texts, each checked on its own (every message and document), so
    a quote may never span two of them. customer_messages (default: sources) are where a
    separate quantity_quote may come from. Our own questions are never a source: otherwise a
    model could quote our product description back and look verified.
    """
    haystack = [_plain(source) for source in sources]
    messages = haystack if customer_messages is None else [_plain(m) for m in customer_messages]
    failures = []
    for index, item in enumerate(requirements.items):
        quote = _plain(item.source_quote)
        if item.quantity is None or item.quantity_quote is None:
            quantity_supported = item.quantity is None or _number_appears(item.quantity, quote)
        else:
            quantity_quote = _plain(item.quantity_quote)
            quantity_supported = any(
                quantity_quote in message for message in messages
            ) and _number_appears(item.quantity, quantity_quote)
        supported = (
            any(quote in source for source in haystack)
            and _plain(item.product_text) in quote
            and quantity_supported
        )
        if not supported:
            failures.append(index)
    return failures


class MatchStatus(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class ProductMatch:
    item_index: int
    status: MatchStatus
    sku: str | None
    candidate_skus: tuple[str, ...]


def decide_match(
    item_index: int, candidates: Sequence[Product], chosen: str | None
) -> ProductMatch:
    """A choice is accepted only if it is one of the ERP's own candidates."""
    skus = tuple(product.sku for product in candidates)
    if not skus:
        return ProductMatch(item_index, MatchStatus.NOT_FOUND, None, skus)
    if len(skus) == 1:
        return ProductMatch(item_index, MatchStatus.MATCHED, skus[0], skus)
    if chosen is not None and chosen in skus:
        return ProductMatch(item_index, MatchStatus.MATCHED, chosen, skus)
    return ProductMatch(item_index, MatchStatus.AMBIGUOUS, None, skus)


def needs_categories(
    requirements: ExtractedRequirements, matches: Sequence[ProductMatch], unverified: Sequence[int]
) -> bool:
    """True when assess will tell the customer what the business supplies."""
    if not requirements.items:
        return True
    found = {match.item_index for match in matches if match.status is not MatchStatus.NOT_FOUND}
    return any(
        index not in unverified and index not in found for index in range(len(requirements.items))
    )


def _what_we_supply(categories: Sequence[str], alternative: str) -> str:
    if not categories:
        return ""
    return (
        f" We supply: {', '.join(categories)}. "
        f"Ask about a category to see its products{alternative}."
    )


class ExtractionOutcome(StrEnum):
    READY_FOR_QUOTATION = "ready_for_quotation"
    NEEDS_CLARIFICATION = "needs_clarification"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class Assessment:
    outcome: ExtractionOutcome
    lines: tuple[RequestedLine, ...]
    questions: tuple[str, ...]
    review_reasons: tuple[str, ...]


def assess(
    requirements: ExtractedRequirements,
    matches: Sequence[ProductMatch],
    products: Mapping[str, Product],
    unverified: Sequence[int],
    unsupported_documents: Sequence[str],
    categories: Sequence[str] = (),
) -> Assessment:
    """Decide the next step. Questions are fixed templates, never model-written text.

    categories are the business's product groups from the ERP. They are named when nothing was
    understood or a product is not in the catalogue, so the customer learns what is sold.
    """
    reasons = [f"Item {index + 1} is not supported by the customer's text" for index in unverified]
    reasons += [
        f"Attachment {name!r} could not be read; a person should check it"
        for name in unsupported_documents
    ]
    questions: list[str] = []
    lines: list[RequestedLine] = []
    if not requirements.items:
        questions.append(
            "Which products and quantities would you like a quotation for?"
            + _what_we_supply(categories, "")
        )
    by_index = {match.item_index: match for match in matches}
    for index, item in enumerate(requirements.items):
        if index in unverified:
            continue
        match = by_index.get(index)
        label = item.product_text
        if match is None or match.status is MatchStatus.NOT_FOUND:
            hint = _what_we_supply(categories, ", or share a product code") or (
                " Could you share the product code or more details?"
            )
            questions.append(f"We could not find {label!r} in our catalogue.{hint}")
            continue
        if match.status is MatchStatus.AMBIGUOUS or match.sku is None:
            options = ", ".join(
                f"{sku} ({products[sku].description})" if sku in products else sku
                for sku in match.candidate_skus[:5]
            )
            questions.append(f"For {label!r}, which product did you mean: {options}?")
            continue
        product = products[match.sku]
        if item.quantity is None or item.quantity <= 0:
            questions.append(f"How many {product.unit} of {product.description} do you need?")
            continue
        unit = normalise_unit(item.unit)
        if (
            item.unit is None
            and product.unit == "each"
            and item.quantity == item.quantity.to_integral()
        ):
            unit = "each"  # "50 fans" is a count; only for products sold individually
        if unit != product.unit:
            asked = item.unit or "no unit"
            questions.append(
                f"{product.description} is sold per {product.unit}, but the request says "
                f"{item.quantity} {asked}. How many {product.unit} do you need?"
            )
            continue
        lines.append(RequestedLine(product.sku, item.quantity, product.unit))

    if reasons:
        outcome = ExtractionOutcome.NEEDS_REVIEW
    elif questions:
        outcome = ExtractionOutcome.NEEDS_CLARIFICATION
    else:
        outcome = ExtractionOutcome.READY_FOR_QUOTATION
    # Two items can lead to the same question (e.g. "cable price" and "list of cables").
    unique = tuple(dict.fromkeys(questions))
    return Assessment(outcome, tuple(lines), unique, tuple(reasons))
