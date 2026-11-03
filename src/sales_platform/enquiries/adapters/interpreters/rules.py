"""Free, offline, deterministic interpreter using simple text patterns (no AI).

It understands phrases like "50 exhaust fans", "150 pcs M8 bolts" or "2 x IND-FAN-450", and
always returns verbatim quotes. It is the default provider and the baseline for evaluations;
anything it cannot read becomes a clarification question rather than a guess.

In a conversation each message is read on its own (a quote never spans two messages). One more
rule (phase 4, Q6): when the latest message is a bare answer, exactly one number with an
optional unit such as "10 metres", and the previous round left exactly one open item, the
answer completes that item. It cannot read "cables price?" (no number), so the literal demo
conversation needs a model provider; that limitation is documented.
"""

import re
from collections.abc import Sequence
from decimal import Decimal

from sales_platform.catalogue.domain.catalogue import Product
from sales_platform.catalogue.domain.units import normalise_unit
from sales_platform.enquiries.domain.requirements import (
    MAX_ITEMS,
    ExtractedRequirements,
    RequestedItem,
)
from sales_platform.enquiries.ports.interpretation import DocumentText, EnquiryTranscript

# A quantity: a number not glued to letters or hyphens (so "M8" and "2-core" are not counts).
_QUANTITY = re.compile(r"(?<![\w.\-])(\d+(?:\.\d+)?)(?=\s)")
# A word may start with a digit if it also has letters ("2-core"); plain numbers end a phrase.
_WORD = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9\-]*)")
_STOP_WORDS = frozenset(
    {"and", "for", "to", "at", "by", "with", "please", "also", "in", "on", "before", "delivered",
     "delivery", "each", "per", "or", "plus", "we", "need", "required", "urgently"}
)  # fmt: skip
# Numbers followed by these are measurements ("450 mm"), not order quantities.
_MEASURES = frozenset(
    {"mm", "cm", "hp", "kw", "v", "w", "inch", "inches", "amp", "amps", "percent", "pct"}
)
_LINKERS = frozenset({"x", "of"})
_MAX_PRODUCT_WORDS = 8


def _words(text: str) -> set[str]:
    found = set()
    for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.casefold()):
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if len(word) >= 2:
            found.add(word)
    return found


def _items_in(text: str) -> list[RequestedItem]:
    items = []
    for number in _QUANTITY.finditer(text):
        position = number.end()
        unit: str | None = None
        product_words: list[str] = []
        product_start = product_end = -1
        while len(product_words) < _MAX_PRODUCT_WORDS:
            word = _WORD.match(text, position)
            if word is None:
                break
            value = word.group(1)
            lowered = value.casefold()
            if not re.search(r"[A-Za-z]", value):
                break
            if not product_words and unit is None and lowered in _MEASURES:
                break  # a dimension, e.g. "fan 450 mm"
            if not product_words and unit is None and normalise_unit(value) is not None:
                unit = value
            elif not product_words and lowered in _LINKERS:
                pass
            elif lowered in _STOP_WORDS:
                break
            else:
                if not product_words:
                    product_start = word.start(1)
                product_words.append(value)
                product_end = word.end(1)
            position = word.end()
        if not product_words:
            continue
        items.append(
            RequestedItem(
                source_quote=text[number.start() : product_end],
                product_text=text[product_start:product_end],
                quantity=Decimal(number.group(1)),
                unit=unit,
            )
        )
    return items


def _bare_answer(text: str) -> tuple[Decimal, str | None, str] | None:
    """(quantity, unit, quoted words) when the message is one number with an optional unit."""
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    match = re.fullmatch(
        r"\W*(?P<quote>(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]+\.?)?)\W*", text.strip()
    )
    if len(numbers) != 1 or match is None:
        return None
    unit = match.group("unit")
    if unit is not None and normalise_unit(unit) is None:
        return None
    return Decimal(match.group("number")), unit, match.group("quote")


class RulesInterpreter:
    def extract(
        self, transcript: EnquiryTranscript, documents: Sequence[DocumentText]
    ) -> ExtractedRequirements:
        items: list[RequestedItem] = []
        for message in transcript.messages:
            items.extend(_items_in(message.text))
        latest = transcript.messages[-1].text if transcript.messages else ""
        answer = _bare_answer(latest) if len(transcript.open_items) == 1 else None
        if answer is not None and not _items_in(latest):
            [open_item] = transcript.open_items
            quantity, unit, quote = answer
            # The answer replaces what the earlier message said about that item.
            items = [item for item in items if item.source_quote != open_item.source_quote]
            items.append(
                RequestedItem(
                    source_quote=open_item.source_quote,
                    product_text=open_item.product_text,
                    quantity=quantity,
                    unit=unit,
                    quantity_quote=quote,
                )
            )
        for document in documents:
            if document.text:
                items.extend(_items_in(document.text))
        return ExtractedRequirements(items=tuple(items[:MAX_ITEMS]))

    def choose_product(self, item: RequestedItem, candidates: Sequence[Product]) -> str | None:
        """Choose only when exactly one candidate contains every word the customer used."""
        wanted = _words(item.product_text)
        fitting = [
            product.sku
            for product in candidates
            if wanted
            <= _words(f"{product.sku} {product.sku.replace('-', ' ')} {product.description}")
        ]
        return fitting[0] if len(fitting) == 1 else None
