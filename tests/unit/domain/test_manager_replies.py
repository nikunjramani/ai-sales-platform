"""The deterministic safety rules for written manager replies (D10, phase 5)."""

import json
from pathlib import Path

import pytest

from sales_platform.domain.manager_replies import (
    MAX_REPLY_CHARACTERS,
    ReplyClassification,
    ReplyIntent,
    clean_reply,
    confirm_word,
    guard,
)

NUMBER = "Q-1A2B3C4D"
CASES = Path(__file__).parents[3] / "evals" / "manager_replies.jsonl"


def load_cases() -> list[dict[str, object]]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line]


def suggest(intent: ReplyIntent, quote: str | None = None) -> ReplyClassification:
    return ReplyClassification(intent, quote, "test:model")


def test_the_evaluation_set_is_large_enough_and_well_formed() -> None:
    cases = load_cases()
    assert len(cases) >= 80
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert case["expected"] in {intent.value for intent in ReplyIntent}
        # Anything not a clear approval is in the hard gate (zero false approvals).
        assert bool(case.get("never_approve")) == (case["expected"] != "approve")


@pytest.mark.parametrize(
    "case", [case for case in load_cases() if case.get("guard_blocks")], ids=lambda c: c["id"]
)
def test_the_guards_alone_stop_an_approval_a_reader_wrongly_suggests(
    case: dict[str, object],
) -> None:
    """Even a reader that always says "approve" cannot approve these replies."""
    text = clean_reply(str(case["reply"]))
    assert guard(text, NUMBER, suggest(ReplyIntent.APPROVE)).intent is not ReplyIntent.APPROVE


@pytest.mark.parametrize(
    "case",
    [case for case in load_cases() if case["expected"] == "approve"],
    ids=lambda c: c["id"],
)
def test_clear_approvals_pass_the_guards(case: dict[str, object]) -> None:
    text = clean_reply(str(case["reply"]))
    assert guard(text, NUMBER, suggest(ReplyIntent.APPROVE)).intent is ReplyIntent.APPROVE


def test_a_reason_must_be_copied_from_the_reply() -> None:
    copied = guard("no, too expensive", NUMBER, suggest(ReplyIntent.REJECT, "too expensive"))
    invented = guard("no, too expensive", NUMBER, suggest(ReplyIntent.REJECT, "bad customer"))
    assert (copied.reason, copied.guard) == ("too expensive", None)
    assert (invented.reason, invented.guard) == ("no, too expensive", "reason_not_verbatim")


def test_another_quotation_number_makes_any_reading_unclear() -> None:
    result = guard("reject Q-00000000", NUMBER, suggest(ReplyIntent.REJECT, "x"))
    assert (result.intent, result.guard) == (ReplyIntent.UNCLEAR, "other_quotation")


def test_cleaning_keeps_only_the_new_words() -> None:
    assert clean_reply("  ok\n> earlier text\n>> older  ") == "ok"
    assert len(clean_reply("x" * 5000)) == MAX_REPLY_CHARACTERS


@pytest.mark.parametrize(
    ("text", "answer"),
    [("YES", True), (" yes. ", True), ("No!", False), ("no", False), ("yes please", None),
     ("y", None), ("ok", None), ("", None)],
)  # fmt: skip
def test_only_an_exact_yes_or_no_answers_a_confirmation(text: str, answer: bool | None) -> None:
    assert confirm_word(text) is answer
