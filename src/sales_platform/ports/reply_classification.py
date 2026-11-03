"""The reader of a manager's written reply (D10). Swapping AI providers means one class.

Input: the quotation number and version the reply is about (fixed by the reply-to link, never
by the reader) and the cleaned reply text. Nothing else leaves our system: no prices, no
customer data, no tokens or IDs (user answer P5-6). Output: a suggestion, which the
deterministic guards and the manager's own YES still have to confirm.

Errors: InterpretationRejected for an answer in the wrong shape (read as unclear); any other
exception is temporary (the job retries; after the last attempt the manager is asked).
"""

from typing import Protocol

from sales_platform.domain.manager_replies import ReplyClassification


class ReplyClassifier(Protocol):
    def classify(
        self, quotation_number: str, version: int, reply_text: str
    ) -> ReplyClassification: ...
