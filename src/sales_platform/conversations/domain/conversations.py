"""Which enquiry a customer's new message belongs to (phase 4).

Agreed rules (conversation-follow-ups plan, user 2026-10-23) and the phase 4 answers
(2026-10-28):
- Only enquiries of the same conversation (same customer on the same tool, Q4) are candidates.
- An enquiry waiting for an answer to our question (needs_clarification) is joined if that
  question is less than 7 days old. Exactly 7 days is too old.
- An enquiry that is still being read (received or ready_for_extraction) is also joined (Q3):
  chat customers send several short messages, and the reading is redone with all of them.
- Enquiries in review, ready for quotation or with a quotation are never reopened.
- If several qualify, the newest enquiry wins. Otherwise the message starts a new enquiry.
"""

from enum import StrEnum


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Author(StrEnum):
    CUSTOMER = "customer"
    SYSTEM = "system"
    STAFF = "staff"  # staff replies come later (D16)
