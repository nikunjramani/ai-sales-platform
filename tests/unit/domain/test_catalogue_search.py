from sales_platform.domain.catalogue_search import (
    best_matches,
    product_terms,
    query_terms,
    search_words,
)


def test_words_are_lower_case_and_keep_hyphen_compounds() -> None:
    assert search_words("2-Core Power CABLE") == {"2-core", "power", "cable"}


def test_simple_plural_is_removed_but_not_from_short_or_ss_words() -> None:
    assert search_words("fans bus glass pumps") == {"fan", "bus", "glass", "pump"}


def test_words_shorter_than_two_characters_are_dropped() -> None:
    assert search_words("a M8 x 2") == {"m8"}


def test_digits_and_duplicates() -> None:
    assert search_words("450 450 mm") == {"450", "mm"}


def test_query_also_searches_hyphen_parts() -> None:
    assert query_terms("IND-FAN") == {"ind-fan", "ind", "fan"}


def test_product_terms_use_sku_parts_name_and_category_only() -> None:
    terms = product_terms("IND-FAN-450", "Industrial exhaust fan 450 mm", "Exhaust fans")
    assert terms == {"ind-fan-450", "ind", "fan", "450", "industrial", "exhaust", "mm"}
    assert product_terms("X1", "Bolt", None) == {"x1", "bolt"}


def test_best_matches_keeps_only_the_top_score_sorted_by_sku() -> None:
    scored = [(2, "B", "b"), (1, "A", "a"), (2, "A2", "a2"), (0, "C", "c")]
    assert best_matches(scored, 10) == ["a2", "b"]
    assert best_matches(scored, 1) == ["a2"]


def test_best_matches_uses_code_point_order_and_empty_when_nothing_matches() -> None:
    assert best_matches([(1, "b", 1), (1, "B", 2)], 5) == [2, 1]
    assert best_matches([(0, "A", 1)], 5) == []
