from decimal import Decimal

import pytest

from sales_platform.adapters.interpreters.rules import RulesInterpreter
from sales_platform.domain.catalogue import Product
from sales_platform.domain.requirements import RequestedItem, unverified_items
from sales_platform.ports.interpretation import (
    CustomerText,
    DocumentText,
    EnquiryTranscript,
    OpenItem,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please quote 50 industrial exhaust fans and 150 pcs M8 bolts for Pune.",
         [("50", None, "industrial exhaust fans"), ("150", "pcs", "M8 bolts")]),
        ("Need 2 x IND-FAN-450, 12.5 metres of 2-core cable.",
         [("2", None, "IND-FAN-450"), ("12.5", "metres", "2-core cable")]),
        ("6 fans 450 mm, discount 100 percent", [("6", None, "fans")]),
        ("Hello, please call me back.", []),
    ],
)  # fmt: skip
def test_extracts_quantity_unit_and_product_words(
    text: str, expected: list[tuple[str, str | None, str]]
) -> None:
    found = RulesInterpreter().extract(EnquiryTranscript.single(text), [])
    assert [(str(i.quantity), i.unit, i.product_text) for i in found.items] == expected
    assert unverified_items(found, [text]) == []  # quotes are always verbatim


def test_reads_attachment_text_but_not_unreadable_documents() -> None:
    documents = [
        DocumentText("po.pdf", "Item list: 3 x IND-PUMP-2HP", None),
        DocumentText("scan.png", None, "image_requires_ocr"),
    ]
    found = RulesInterpreter().extract(EnquiryTranscript.single("See attached."), documents)
    assert [item.product_text for item in found.items] == ["IND-PUMP-2HP"]


def test_chooses_only_when_one_candidate_fits_every_word() -> None:
    fan_450 = Product("IND-FAN-450", "Exhaust fan 450 mm", "each", Decimal(1), Decimal(1))
    fan_600 = Product("IND-FAN-600", "Exhaust fan 600 mm", "each", Decimal(1), Decimal(1))
    rules = RulesInterpreter()

    def ask(words: str) -> str | None:
        return rules.choose_product(RequestedItem(words, words, None, None), [fan_450, fan_600])

    assert ask("exhaust fans 600") == "IND-FAN-600"
    assert ask("exhaust fans") is None  # both fit: ask the customer instead of guessing


# --- Phase 4 (Q6): a bare answer completes the one open item -------------------------------

OPEN_CABLE = OpenItem("5 boxes of 2-core cable", "2-core cable", "IND-CABLE-2C")


def transcript(*texts: str, open_items: tuple[OpenItem, ...] = (OPEN_CABLE,)) -> EnquiryTranscript:
    messages = tuple(CustomerText(index, text) for index, text in enumerate(texts, start=1))
    return EnquiryTranscript(messages, ("How many metre do you need?",), open_items)


def test_a_bare_answer_completes_the_single_open_item() -> None:
    conversation = transcript("We need 5 boxes of 2-core cable", "10 metres")
    found = RulesInterpreter().extract(conversation, [])
    [item] = found.items  # the earlier "5 boxes" reading is replaced by the answer
    assert (item.quantity, item.unit, item.product_text) == (Decimal(10), "metres", "2-core cable")
    assert item.quantity_quote == "10 metres"
    assert unverified_items(found, conversation.texts) == []


@pytest.mark.parametrize(
    ("answer", "open_items"),
    [
        ("10 metres", (OPEN_CABLE, OpenItem("2 fans", "fans", "IND-FAN-450"))),  # which one?
        ("10 or 12 metres", (OPEN_CABLE,)),  # two numbers
        ("10 parsecs", (OPEN_CABLE,)),  # not a unit we know
        ("thanks", (OPEN_CABLE,)),  # no number
    ],
)
def test_no_binding_when_the_answer_is_not_clear(
    answer: str, open_items: tuple[OpenItem, ...]
) -> None:
    conversation = transcript("We need 5 boxes of 2-core cable", answer, open_items=open_items)
    found = RulesInterpreter().extract(conversation, [])
    assert all(item.quantity_quote is None for item in found.items)


def test_each_message_is_read_on_its_own() -> None:
    conversation = transcript("need 2", "fans please", open_items=())
    found = RulesInterpreter().extract(conversation, [])
    assert found.items == ()  # "2" and "fans" are in different messages: never one quote
