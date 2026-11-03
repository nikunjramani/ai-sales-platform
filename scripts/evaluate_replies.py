"""Evaluate the manager reply reader on the synthetic set (evals/manager_replies.jsonl).

Each reply goes through the configured model and then the same deterministic guards the
worker uses (domain/manager_replies.py guard). Two numbers are reported:
- false approvals: replies marked never_approve that would be offered as "approve". This is
  the hard gate: it must be 0 (an approval still needs the manager's YES, but the question
  itself must not suggest it);
- exact: the share of all other cases read as expected (target 90 %).

Example (any "<provider>:<model>"; the key is SALES_MODEL_API_KEY from .env):
  uv run python scripts/evaluate_replies.py --model google_genai:gemini-3.5-flash-lite
Live model runs are opt-in and use only synthetic data. Without a model, written replies are
not read at all (user answer P5-5), so there is nothing to evaluate. Exit code 1 if the hard
gate fails.
"""

import argparse
import json
import sys
from pathlib import Path

from sales_platform.enquiries.ports.interpretation import InterpretationRejected
from sales_platform.platform.config import Settings
from sales_platform.quotations.adapters.interpreters.reply_classifier import build_reply_classifier
from sales_platform.quotations.domain.manager_replies import (
    ReplyClassification,
    ReplyIntent,
    clean_reply,
    guard,
)

CASES = Path(__file__).parents[1] / "evals" / "manager_replies.jsonl"
NUMBER = "Q-1A2B3C4D"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help='"<provider>:<model>"')
    arguments = parser.parse_args()
    settings = Settings()
    key = settings.model_api_key.get_secret_value() if settings.model_api_key else None
    classifier = build_reply_classifier(arguments.model, key, settings.model_options)
    assert classifier is not None
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line]

    false_approvals: list[str] = []
    exact = total = 0
    for case in cases:
        text = clean_reply(case["reply"])
        try:
            suggestion = classifier.classify(NUMBER, 2, text) if text else None
        except InterpretationRejected:
            suggestion = None
        guarded = guard(
            text, NUMBER, suggestion or ReplyClassification(ReplyIntent.UNCLEAR, None, "")
        )
        if case.get("never_approve") and guarded.intent is ReplyIntent.APPROVE:
            false_approvals.append(case["id"])
        total += 1
        exact += guarded.intent.value == case["expected"]
        print(f"{case['id']:<12} expected {case['expected']:<8} got {guarded.intent.value:<8}"
              f" guard {guarded.guard or '-'}")  # fmt: skip
    print(f"\nexact: {exact}/{total} ({exact / total:.0%}), target 90 %")
    print(f"false approvals (hard gate, must be 0): {len(false_approvals)} {false_approvals}")
    sys.exit(1 if false_approvals else 0)


if __name__ == "__main__":
    main()
