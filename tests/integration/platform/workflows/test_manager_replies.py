"""Approval by message on PostgreSQL (D10, phase 5), through the generic webhook and the worker.

The simulated phone stands in for a messaging tool; a fake reader stands in for the AI. Each
test links a manager's phone, has an enquiry drafted, and answers the review message.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from sales_platform.api_app import create_app
from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.enquiries.adapters.interpreters.rules import RulesInterpreter
from sales_platform.enquiries.persistence.workflow import open_checkpointer
from sales_platform.integrations.persistence.connections import SqlConnectionStore
from sales_platform.platform.adapters.secrets.local import new_key
from sales_platform.platform.application.jobs import JobRunner
from sales_platform.platform.config import Settings
from sales_platform.platform.domain.tenancy import Role
from sales_platform.platform.persistence.jobs import SqlJobQueue
from sales_platform.platform.persistence.tenancy import tenant_transaction
from sales_platform.providers.simulated.signing import SIGNATURE_HEADER, hook_token, sign
from sales_platform.quotations.domain.manager_replies import ReplyClassification, ReplyIntent
from sales_platform.quotations.ports.reply_classification import ReplyClassifier
from sales_platform.seed import INDUSTRIAL_TENANT, OFFICE_TENANT, seed_demo_tenants
from tests.integration.conftest import ApiKeyFactory
from tests.integration.quotations.workflows.test_quotation import (
    ENQUIRY,
    MANAGER,
    SECRET,
    base,
    login,
    make_client,  # noqa: F401  (fixture)
    settings,  # noqa: F401  (fixture)
    submit,
)
from tests.support.demo_catalogue import DemoCatalogue

PHONE = "phone-9"


class FakeReader:
    """Answers by exact reply text; anything else is unclear."""

    def __init__(self, answers: dict[str, tuple[ReplyIntent, str | None]]) -> None:
        self.answers = answers
        self.seen: list[tuple[str, int, str]] = []

    def classify(self, quotation_number: str, version: int, reply_text: str) -> ReplyClassification:
        self.seen.append((quotation_number, version, reply_text))
        intent, quote = self.answers.get(reply_text, (ReplyIntent.UNCLEAR, None))
        return ReplyClassification(intent, quote, "fake:reader")


@dataclass
class Phone:
    """The linked manager phone: post its events, read what it received, run the worker."""

    settings: Settings
    engine: Engine
    client: TestClient
    url: str
    reader: ReplyClassifier | None

    def post(self, event: dict[str, Any]) -> None:
        raw = json.dumps(event).encode()
        answer = self.client.post(
            self.url, content=raw, headers={SIGNATURE_HEADER: sign(SECRET, raw)}
        )
        assert answer.status_code == 200, answer.text

    def drain(self) -> None:
        erp = DemoCatalogue({INDUSTRIAL_TENANT: "industrial", OFFICE_TENANT: "office"})
        with open_checkpointer(self.settings.worker_url()) as checkpointer:
            handlers = build_job_handlers(
                self.settings, self.engine, build_runtime(self.settings, self.engine),
                checkpointer, RulesInterpreter(), erp, reply_classifier=self.reader,
            )  # fmt: skip
            runner = JobRunner(SqlJobQueue(self.engine), handlers)
            while runner.run_once():
                pass

    def received(self) -> list[dict[str, Any]]:
        files = Path(self.settings.storage_dir / "simulated-channel").rglob("*.json")
        messages = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        return [message for message in messages if message.get("to") == PHONE]

    def last(self, words: str) -> dict[str, Any]:
        found = [message for message in self.received() if words in message["text"]]
        assert found, (self.received(), self.state())
        return found[-1]

    def state(self) -> list[tuple[object, ...]]:
        """What the worker did, for a failing assertion's message."""
        with tenant_transaction(self.engine, INDUSTRIAL_TENANT) as connection:
            events = connection.execute(
                text("SELECT event_type, outcome FROM inbox_events WHERE event_type <> "
                     "'customer_message' ORDER BY received_at")
            ).all()  # fmt: skip
            sent = connection.execute(
                text("SELECT template_key, status, last_error FROM outbound_messages "
                     "ORDER BY created_at")
            ).all()  # fmt: skip
            jobs = connection.execute(
                text("SELECT kind, status, last_error FROM jobs WHERE kind = "
                     "'classify_manager_reply'")
            ).all()  # fmt: skip
        return [*map(tuple, events), *map(tuple, sent), *map(tuple, jobs)]

    def review(self) -> dict[str, Any]:
        return next(message for message in self.received() if "task_id" in message)

    def reply(self, event_id: str, to: dict[str, Any], words: str) -> None:
        self.post({"event_id": event_id, "external_user_id": PHONE, "type": "reply",
                   "text": words, "reply_to": to["message_id"]})  # fmt: skip


@pytest.fixture
def phone_factory(
    settings: Settings,  # noqa: F811
    make_client: Callable[[], TestClient],  # noqa: F811
    make_api_key: ApiKeyFactory,
    worker_engine: Engine,
    owner_engine: Engine,
) -> Iterator[Callable[[ReplyClassifier | None], Phone]]:
    @contextmanager
    def nothing() -> Iterator[None]:
        yield

    keyed = settings.model_copy(update={"secret_store_key": SecretStr(new_key())})
    seed_demo_tenants(keyed)
    connection = SqlConnectionStore(owner_engine).find_by_account(
        INDUSTRIAL_TENANT, "simulated", "simulated-local"
    )
    assert connection is not None
    url = f"/api/v1/hooks/{connection.connection_id}/{hook_token(SECRET, connection.connection_id)}"
    client = TestClient(create_app(keyed))
    client.__enter__()

    def build(reader: ReplyClassifier | None) -> Phone:
        phone = Phone(keyed, worker_engine, client, url, reader)
        manager = make_client()
        code = manager.post(
            f"{base()}/channel-enrollments", json={"channel": "simulated"},
            headers=login(manager, MANAGER),
        ).json()["code"]  # fmt: skip
        phone.post({"event_id": "e-enroll", "external_user_id": PHONE, "type": "enroll",
                    "code": code})  # fmt: skip
        phone.drain()
        submit(make_client(), make_api_key(INDUSTRIAL_TENANT, Role.INTAKE_SERVICE), ENQUIRY)
        phone.drain()
        return phone

    yield build
    client.__exit__(None, None, None)
    del nothing


def decisions(owner_engine: Engine) -> list[tuple[str, str | None, str]]:
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        rows = connection.execute(
            text("SELECT action, reason, evidence_ref FROM approval_decisions ORDER BY decided_at")
        ).all()
    return [(row.action, row.reason, row.evidence_ref) for row in rows]


def test_a_written_approval_is_recorded_only_after_yes(
    phone_factory: Callable[[ReplyClassifier | None], Phone], owner_engine: Engine
) -> None:
    reader = FakeReader({"looks good, approved": (ReplyIntent.APPROVE, None)})
    phone = phone_factory(reader)
    review = phone.review()
    assert review["link"] is not None and review["action_reference"] in review["link"]

    phone.reply("r-1", review, "looks good, approved")
    phone.drain()

    question = phone.last("you want to approve quotation")
    assert question["actions"] == ["confirm", "cancel"]
    assert decisions(owner_engine) == []  # nothing is decided before YES
    number, version, words = reader.seen[0]
    assert (number, words) == (review["quotation"], "looks good, approved")

    phone.reply("r-2", question, "YES")
    phone.drain()

    [(action, _, evidence)] = decisions(owner_engine)
    assert action == "approve" and evidence.endswith(":r-2:ai")
    assert "approved quotation" in phone.last("approved quotation")["text"]
    with tenant_transaction(owner_engine, INDUSTRIAL_TENANT) as connection:
        audit = connection.execute(
            text("SELECT intent, model, outcome, text FROM manager_reply_classifications")
        ).all()
    assert [tuple(row) for row in audit] == [
        ("approve", "fake:reader", "confirmation asked", "looks good, approved")
    ]


def test_a_reject_button_asks_for_the_reason_and_unclear_replies_ask_again(
    phone_factory: Callable[[ReplyClassifier | None], Phone], owner_engine: Engine
) -> None:
    phone = phone_factory(FakeReader({"approve if cheaper": (ReplyIntent.APPROVE, None)}))
    review = phone.review()

    phone.reply("r-1", review, "approve if cheaper")  # the guard refuses a conditional approval
    phone.drain()
    assert (
        "do you want to approve, reject or request a revision" in phone.last("do you want")["text"]
    )

    phone.post({"event_id": "b-1", "external_user_id": PHONE, "type": "decision",
                "action_reference": review["action_reference"], "action": "reject"})  # fmt: skip
    phone.drain()
    question = phone.last("reply to this message with the reason for rejecting")
    assert decisions(owner_engine) == []

    phone.reply("r-2", question, "Too expensive for this customer")
    phone.drain()
    [(action, reason, _)] = decisions(owner_engine)
    assert (action, reason) == ("reject", "Too expensive for this customer")
    assert "Reason: Too expensive" in phone.last("rejected quotation")["text"]


def test_without_a_model_written_replies_are_not_read(
    phone_factory: Callable[[ReplyClassifier | None], Phone], owner_engine: Engine
) -> None:
    phone = phone_factory(None)
    review = phone.review()

    phone.reply("r-1", review, "approved")
    phone.post({"event_id": "r-2", "external_user_id": PHONE, "type": "reply", "text": "ok"})
    phone.drain()

    assert "written replies are not read here" in phone.last("not read")["text"]
    assert "reply to its message" in phone.last("To answer an approval request")["text"]
    assert decisions(owner_engine) == []


def test_the_signed_link_needs_sign_in_as_the_manager_it_was_sent_to(
    phone_factory: Callable[[ReplyClassifier | None], Phone],
    make_client: Callable[[], TestClient],  # noqa: F811
    owner_engine: Engine,
) -> None:
    phone = phone_factory(None)
    token = phone.review()["action_reference"]
    url = f"/api/v1/approval-links/{token}"

    assert make_client().get(url).status_code == 401  # not signed in
    other = make_client()
    login(other, "admin@industrial.example")
    assert other.get(url).status_code == 403  # another person's link
    manager = make_client()
    headers = login(manager, MANAGER)
    assert manager.get("/api/v1/approval-links/not-a-token").status_code == 404

    page = manager.get(url).json()
    assert (page["state"], page["number"]) == ("open", phone.review()["quotation"])
    decision = {"command_id": str(uuid4()), "action": "approve",
                "content_sha256": page["content_sha256"]}  # fmt: skip
    assert manager.post(f"{url}/decision", json=decision).status_code == 403  # no CSRF token
    answer = manager.post(f"{url}/decision", json=decision, headers=headers)

    assert answer.json() == {"action": "approve", "replayed": False}
    assert manager.post(f"{url}/decision", json=decision, headers=headers).json()["replayed"]
    assert manager.get(url).json()["state"] == "decided"
    [(action, _, evidence)] = decisions(owner_engine)
    assert action == "approve" and evidence.startswith("approval-link:")
