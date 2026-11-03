"""Reading a manager's written reply to an approval request (D10, phase 5). Pure rules.

The AI (when a model is configured) suggests what a reply means; these deterministic rules
decide what is done with that suggestion. They are the safety rules of D10 in code:
- the reply is cleaned first (only the new text, at most MAX_REPLY_CHARACTERS);
- guard(): an approval is never taken from conditional, mixed, negated or questioning words,
  nor when the reply names another quotation; a reason must be copied from the reply;
- an AI-read decision is never recorded at once: the manager confirms it with an exact YES
  (or cancels with NO), user answer P5-13. confirm_word() reads only those two words.
Without a model, written replies are not read at all (user answer P5-5).
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from sales_platform.quotations.domain.quotations import ApprovalAction

MAX_REPLY_CHARACTERS = 1000
_NUMBER = re.compile(r"\bQ-[0-9A-F]{8}\b", re.IGNORECASE)
_QUOTED = re.compile(r"^\s*>")
# Words that make an approval conditional, mixed or doubtful (English and common mixes).
_DOUBT = re.compile(
    r"\b(if|but|except|unless|provided|however|although|though|only|after|before|once|"
    r"not|no|don'?t|do not|never|wait|hold|stop|cancel|check|maybe|perhaps|later|"
    r"change|reduce|increase|lower|higher|discount|instead|also|wrong|recheck|"
    r"nahi|mat|ruko)\b",
    re.IGNORECASE,
)


class ReplyIntent(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"
    UNCLEAR = "unclear"


ACTION_OF: dict[ReplyIntent, ApprovalAction] = {
    ReplyIntent.APPROVE: ApprovalAction.APPROVE,
    ReplyIntent.REJECT: ApprovalAction.REJECT,
    ReplyIntent.REVISE: ApprovalAction.REQUEST_REVISION,
}


@dataclass(frozen=True)
class ReplyClassification:
    """What a reader suggests. reason_quote: the reason words, copied from the reply."""

    intent: ReplyIntent
    reason_quote: str | None
    model: str  # "<provider>:<model>"


@dataclass(frozen=True)
class GuardedReply:
    """What will be asked: the action to confirm (with its reason), or a clarifying question
    (intent UNCLEAR). guard names the rule that changed the reader's suggestion, if any."""

    intent: ReplyIntent
    reason: str | None
    guard: str | None


def clean_reply(text: str) -> str:
    """The new words only: quoted lines ("> ...") removed, spaces tidied, length capped."""
    lines = [line for line in text.splitlines() if not _QUOTED.match(line)]
    cleaned = " ".join(" ".join(lines).split())
    return cleaned[:MAX_REPLY_CHARACTERS]


def quotation_numbers(text: str) -> set[str]:
    return {match.upper() for match in _NUMBER.findall(text)}


def guard(text: str, number: str, suggestion: ReplyClassification) -> GuardedReply:
    """Apply the deterministic safety rules to a reader's suggestion for one request."""
    if not text:
        return GuardedReply(ReplyIntent.UNCLEAR, None, "empty")
    others = quotation_numbers(text) - {number.upper()}
    if others:
        return GuardedReply(ReplyIntent.UNCLEAR, None, "other_quotation")
    intent = suggestion.intent
    if intent is ReplyIntent.UNCLEAR:
        return GuardedReply(intent, None, None)
    if intent is ReplyIntent.APPROVE:
        if "?" in text:
            return GuardedReply(ReplyIntent.UNCLEAR, None, "question")
        if _DOUBT.search(text):
            return GuardedReply(ReplyIntent.UNCLEAR, None, "conditional_approval")
        return GuardedReply(intent, None, None)
    quote = (suggestion.reason_quote or "").strip()
    if quote and quote.lower() in text.lower():
        return GuardedReply(intent, quote, None)
    # Not copied from the reply: the manager's own full words are the reason instead.
    return GuardedReply(intent, text, "reason_not_verbatim" if quote else None)


def confirm_word(text: str) -> bool | None:
    """True for an exact YES, False for an exact NO (any case, end punctuation ignored)."""
    word = text.strip().strip(".!").strip().upper()
    if word == "YES":
        return True
    if word == "NO":
        return False
    return None
