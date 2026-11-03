"""Deterministic guards around model output: verbatim checks, matching and assessment."""

from decimal import Decimal

import pytest

from sales_platform.catalogue.domain.catalogue import Product
from sales_platform.catalogue.domain.units import normalise_unit
from sales_platform.enquiries.domain.requirements import (
    ExtractedRequirements,
    ExtractionOutcome,
    MatchStatus,
    ProductMatch,
    RequestedItem,
    assess,
    decide_match,
    needs_categories,
    unverified_items,
)

FAN = Product("FAN-450", "Exhaust fan 450 mm", "each", Decimal(1), Decimal(1))
FAN_600 = Product("FAN-600", "Exhaust fan 600 mm", "each", Decimal(1), Decimal(1))
CABLE = Product("CABLE", "Power cable", "metre", Decimal(1), Decimal("0.5"))
PRODUCTS = {product.sku: product for product in (FAN, FAN_600, CABLE)}


def item(quote: str, product: str, quantity: str | None, unit: str | None = None) -> RequestedItem:
    return RequestedItem(quote, product, None if quantity is None else Decimal(quantity), unit)


def requirements(*items: RequestedItem) -> ExtractedRequirements:
    return ExtractedRequirements(tuple(items))


def matched(index: int, product: Product) -> ProductMatch:
    return ProductMatch(index, MatchStatus.MATCHED, product.sku, (product.sku,))


# --- Phase 4: a conversation is several customer messages ----------------------------------


def test_quantity_from_a_later_message_is_verified_through_quantity_quote() -> None:
    answer = RequestedItem("cables price", "cables", Decimal(10), "meter", "10 meter")
    messages = ["can you get cables price?", "10 meter?"]
    assert unverified_items(requirements(answer), messages) == []


def test_quantity_only_in_our_question_is_not_verified() -> None:
    """Our question is never a source, so quoting it back cannot make a number look real."""
    invented = RequestedItem("cables price", "cables", Decimal(2), None, "2-core power cable")
    messages = ["can you get cables price?", "thanks"]
    assert unverified_items(requirements(invented), messages) == [0]


def test_a_quote_spanning_two_messages_fails() -> None:
    spanning = item("cables price? 10 meter", "cables", "10", "meter")
    assert unverified_items(requirements(spanning), ["cables price?", "10 meter"]) == [0]


def test_an_invented_quantity_in_quantity_quote_fails() -> None:
    invented = RequestedItem("cables price", "cables", Decimal(12), "meter", "10 meter")
    assert unverified_items(requirements(invented), ["cables price?", "10 meter"]) == [0]


def test_quantity_quote_must_come_from_a_customer_message_not_a_document() -> None:
    from_document = RequestedItem("cables price", "cables", Decimal(10), None, "10 meter")
    sources = ["cables price?", "spec sheet: 10 meter reel"]
    assert unverified_items(requirements(from_document), sources, ["cables price?"]) == [0]
    assert unverified_items(requirements(from_document), sources, sources) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("pcs", "each"), ("Nos.", "each"), ("Metres", "metre"), ("m", "metre"), ("box", "box"),
     ("dozen", None), (None, None)],
)  # fmt: skip
def test_units_use_a_fixed_alias_table(raw: str | None, expected: str | None) -> None:
    assert normalise_unit(raw) == expected


def test_invented_quotes_products_and_numbers_are_flagged() -> None:
    source = "Please quote 50 exhaust fans and 12.5 m of power cable."
    found = requirements(
        item("50 exhaust fans", "exhaust fans", "50"),  # genuine
        item("60 exhaust fans", "exhaust fans", "60"),  # quote not in source
        item("50 exhaust fans", "pumps", "50"),  # product not in quote
        item("12.5 m of power cable", "power cable", "125", "m"),  # number not in quote
        item("12.5 M OF  POWER cable", "Power Cable", "12.5", "m"),  # case/space insensitive
    )
    assert unverified_items(found, [source]) == [1, 2, 3]


def test_match_accepts_only_erp_candidates() -> None:
    assert decide_match(0, [], "FAN-450").status is MatchStatus.NOT_FOUND
    assert decide_match(0, [FAN], None).sku == "FAN-450"
    assert decide_match(0, [FAN, FAN_600], "FAN-600").sku == "FAN-600"
    invented = decide_match(0, [FAN, FAN_600], "FAN-999")
    assert invented.status is MatchStatus.AMBIGUOUS and invented.sku is None


def test_ready_when_every_line_is_complete() -> None:
    result = assess(
        requirements(
            item("2 x FAN-450", "FAN-450", "2"),  # count without unit -> each
            item("12.5 metres of power cable", "power cable", "12.5", "metres"),
        ),
        [matched(0, FAN), matched(1, CABLE)],
        PRODUCTS,
        [],
        [],
    )
    assert result.outcome is ExtractionOutcome.READY_FOR_QUOTATION
    assert [(line.sku, line.quantity, line.unit) for line in result.lines] == [
        ("FAN-450", Decimal(2), "each"),
        ("CABLE", Decimal("12.5"), "metre"),
    ]


@pytest.mark.parametrize(
    ("found", "match", "question_part"),
    [
        (item("fans", "fans", None), matched(0, FAN), "How many each"),
        (item("3 boxes of fans", "fans", "3", "boxes"), matched(0, FAN), "sold per each"),
        (item("2.5 fans", "fans", "2.5"), matched(0, FAN), "sold per each"),
        (item("12 cable", "cable", "12"), matched(0, CABLE), "sold per metre"),
        (item("5 fans", "fans", "5"),
         ProductMatch(0, MatchStatus.AMBIGUOUS, None, ("FAN-450", "FAN-600")),
         "FAN-450 (Exhaust fan 450 mm), FAN-600"),
        (item("5 presses", "presses", "5"),
         ProductMatch(0, MatchStatus.NOT_FOUND, None, ()), "could not find 'presses'"),
    ],
)  # fmt: skip
def test_incomplete_lines_become_fixed_questions(
    found: RequestedItem, match: ProductMatch, question_part: str
) -> None:
    result = assess(requirements(found), [match], PRODUCTS, [], [])
    assert result.outcome is ExtractionOutcome.NEEDS_CLARIFICATION
    assert result.lines == ()
    assert question_part in result.questions[0]


def test_nothing_requested_asks_for_items() -> None:
    result = assess(requirements(), [], PRODUCTS, [], [])
    assert result.outcome is ExtractionOutcome.NEEDS_CLARIFICATION
    assert "Which products and quantities" in result.questions[0]


def test_invented_items_or_unreadable_files_go_to_a_person() -> None:
    found = requirements(item("2 fans", "fans", "2"))
    unverified = assess(found, [], PRODUCTS, [0], [])
    assert unverified.outcome is ExtractionOutcome.NEEDS_REVIEW
    assert unverified.lines == () and "not supported" in unverified.review_reasons[0]
    scan = assess(found, [matched(0, FAN)], PRODUCTS, [], ["scan.png (image_requires_ocr)"])
    assert scan.outcome is ExtractionOutcome.NEEDS_REVIEW
    assert "scan.png" in scan.review_reasons[0]


CATEGORIES = ("Cables", "Exhaust fans", "Pumps")


def test_nothing_understood_names_what_the_business_supplies() -> None:
    result = assess(requirements(), [], PRODUCTS, [], [], CATEGORIES)
    assert result.outcome is ExtractionOutcome.NEEDS_CLARIFICATION
    assert result.questions == (
        "Which products and quantities would you like a quotation for? "
        "We supply: Cables, Exhaust fans, Pumps. Ask about a category to see its products.",
    )


def test_unknown_product_is_reported_with_categories() -> None:
    found = requirements(item("2 tata sierra cars", "tata sierra cars", "2"))
    result = assess(
        found, [ProductMatch(0, MatchStatus.NOT_FOUND, None, ())], {}, [], [], CATEGORIES
    )
    assert result.questions == (
        "We could not find 'tata sierra cars' in our catalogue. We supply: Cables, Exhaust fans, "
        "Pumps. Ask about a category to see its products, or share a product code.",
    )


def test_without_categories_the_reply_asks_for_details() -> None:
    assert assess(requirements(), [], {}, [], []).questions == (
        "Which products and quantities would you like a quotation for?",
    )
    found = requirements(item("2 cars", "cars", "2"))
    result = assess(found, [ProductMatch(0, MatchStatus.NOT_FOUND, None, ())], {}, [], [])
    assert result.questions == (
        "We could not find 'cars' in our catalogue. "
        "Could you share the product code or more details?",
    )


def test_categories_are_needed_only_for_nothing_understood_or_not_found() -> None:
    fans, cars = item("5 fans", "fans", "5"), item("2 cars", "cars", "2")
    not_found = ProductMatch(1, MatchStatus.NOT_FOUND, None, ())
    assert needs_categories(requirements(), [], [])
    assert not needs_categories(requirements(fans), [matched(0, FAN)], [])
    assert needs_categories(requirements(fans, cars), [matched(0, FAN), not_found], [])
    # An invented (unverified) item goes to review, not to a "we supply" reply.
    assert not needs_categories(requirements(fans, cars), [matched(0, FAN)], [1])


def test_the_same_question_is_asked_once() -> None:
    found = requirements(
        item("cables price", "cables", None), item("list of cables", "cables", None)
    )
    result = assess(found, [matched(0, CABLE), matched(1, CABLE)], PRODUCTS, [], [])
    assert result.questions == ("How many metre of Power cable do you need?",)
