"""Word matching for product search, shared by every catalogue source we run ourselves.

The simulated ERP and our own catalogue (phase 3, ADR 0020) must find exactly the same
products for the same words, because one search hit is matched automatically
(requirements.decide_match). So the rules live here once:
- words: lower-case runs of letters and digits, hyphen compounds kept ("2-core"), a simple
  plural "s" removed for words longer than 3 not ending in "ss" (fans -> fan), words shorter
  than 2 characters dropped;
- a query also searches its hyphen parts ("IND-FAN" -> "ind-fan", "ind", "fan");
- a product is found by its SKU (whole and hyphen parts), name and category;
- only the products sharing the most words are kept, sorted by SKU ("best matches only").
"""

import re
from collections.abc import Iterable

_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def search_words(text: str) -> frozenset[str]:
    """Lower-case words of 2+ characters, with a simple plural 's' removed (fans -> fan)."""
    words = set()
    for word in _WORD.findall(text.casefold()):
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if len(word) >= 2:
            words.add(word)
    return frozenset(words)


def query_terms(query: str) -> frozenset[str]:
    """Words of a customer's search; "IND-FAN" also searches "IND" and "FAN"."""
    return search_words(f"{query} {query.replace('-', ' ')}")


def product_terms(sku: str, name: str, category: str | None) -> frozenset[str]:
    """Words a product can be found by. The staff-only description is not searched (Q22)."""
    return search_words(f"{sku} {sku.replace('-', ' ')} {name} {category or ''}")


def best_matches[T](scored: Iterable[tuple[int, str, T]], limit: int) -> list[T]:
    """Keep only the highest score above 0, sorted by SKU (code-point order), cut to limit.

    Like a typical ERP keyword search: "IND-FAN" matches the fans on "ind" and "fan", not
    every IND- product on "ind" alone.
    """
    hits = [entry for entry in scored if entry[0] > 0]
    best = max((score for score, _, _ in hits), default=0)
    ranked = sorted((entry for entry in hits if entry[0] == best), key=lambda entry: entry[1])
    return [item for _, _, item in ranked][:limit]
