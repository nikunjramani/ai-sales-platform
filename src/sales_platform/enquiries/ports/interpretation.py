"""Contracts for reading documents and interpreting enquiries.

Swapping AI providers means writing one class that satisfies EnquiryInterpreter; the workflow,
checks and tests do not change. See adapters/interpreters/factory.py.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sales_platform.catalogue.domain.catalogue import Product
from sales_platform.enquiries.domain.requirements import ExtractedRequirements, RequestedItem


@dataclass(frozen=True)
class CustomerText:
    """One customer message of the enquiry, in order (1-based)."""

    index: int
    text: str


@dataclass(frozen=True)
class OpenItem:
    """An item the previous round understood and matched but could not price yet (its
    quantity or unit was missing): a bare answer such as "10 metres" may complete it."""

    source_quote: str
    product_text: str
    sku: str


@dataclass(frozen=True)
class EnquiryTranscript:
    """Everything the customer wrote for one enquiry, plus our earlier questions.

    messages are untrusted customer data. asked are our own fixed questions (trusted): they help
    a reader see what an answer refers to, but are never a source for verbatim checks.
    """

    messages: tuple[CustomerText, ...]
    asked: tuple[str, ...] = ()
    open_items: tuple[OpenItem, ...] = ()

    @classmethod
    def single(cls, text: str) -> "EnquiryTranscript":
        return cls((CustomerText(1, text),))

    @property
    def texts(self) -> list[str]:
        return [message.text for message in self.messages]


@dataclass(frozen=True)
class DocumentText:
    """Text read from an attachment, or the reason it could not be read."""

    name: str
    text: str | None
    unsupported_reason: str | None


class InterpretationRejected(Exception):
    """The model answered, but not in the required shape. Retrying the same input won't help."""


class DocumentReader(Protocol):
    def read(self, name: str, content_type: str, data: bytes) -> DocumentText: ...


class EnquiryInterpreter(Protocol):
    def extract(
        self, transcript: EnquiryTranscript, documents: Sequence[DocumentText]
    ) -> ExtractedRequirements:
        """Read untrusted customer content. Raise InterpretationRejected for invalid output;
        any other exception is treated as temporary (e.g. model unavailable) and retried.

        Quotes must come from one message or document each. A quantity given in a later
        message than its product goes into quantity_quote (phase 4)."""
        ...

    def choose_product(self, item: RequestedItem, candidates: Sequence[Product]) -> str | None:
        """Pick one candidate SKU, or None if unsure. Answers outside candidates are ignored."""
        ...
