"""Website chat end to end on PostgreSQL (phase 4): enable, start, ask, answer, approve, accept
in the chat, STOP; plus the refusals and the daily reading cap."""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from sales_platform.adapters.interpreters.rules import RulesInterpreter
from sales_platform.api.app import create_app
from sales_platform.api.auth import CSRF_HEADER
from sales_platform.application.jobs import JobRunner
from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.config import Settings
from sales_platform.persistence.jobs import SqlJobQueue
from sales_platform.persistence.seed import LOCAL_TENANT
from sales_platform.persistence.tenancy import tenant_transaction
from sales_platform.persistence.workflow import open_checkpointer

pytestmark = pytest.mark.integration

BASE = f"/api/v1/tenants/{LOCAL_TENANT}"


@pytest.fixture
def client(integration_settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(integration_settings)) as app:
        yield app


def drain(settings: Settings, engine: Engine) -> None:
    with open_checkpointer(settings.worker_url()) as checkpointer:
        handlers = build_job_handlers(
            settings, engine, build_runtime(settings, engine), checkpointer, RulesInterpreter()
        )
        runner = JobRunner(SqlJobQueue(engine), handlers)
        while runner.run_once():
            pass


def login(client: TestClient, subject: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/dev-login", json={"subject": subject})
    assert response.status_code == 200, response.text
    return {CSRF_HEADER: response.json()["csrf_token"]}


def enable_chat(client: TestClient) -> str:
    headers = login(client, "admin@local.example")
    response = client.post(
        f"{BASE}/connections/hosted-chat", json={"provider_key": "web_chat"}, headers=headers
    )
    assert response.status_code == 200, response.text
    chat = response.json()["chat"]
    assert chat["chat_link"].endswith(f"/chat/{chat['handle']}")
    assert f'data-chat="{chat["handle"]}"' in chat["snippet"]
    client.post("/api/v1/auth/logout", headers=headers)
    client.cookies.clear()
    return str(chat["handle"])


def start(client: TestClient, handle: str, **changes: Any) -> Any:
    body = {"name": "Asha Rao", "email": "asha@example.com", "website": "", "form_ms": 4000}
    body.update(changes)
    return client.post(f"/api/v1/public/chat/{handle}/sessions", json=body)


def say(client: TestClient, handle: str, token: str, words: str, key: str | None = None) -> Any:
    return client.post(
        f"/api/v1/public/chat/{handle}/messages",
        json={"text": words},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key or f"m-{uuid4()}"},
    )


def thread(client: TestClient, handle: str, token: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/api/v1/public/chat/{handle}/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    messages: list[dict[str, Any]] = response.json()["messages"]
    return messages


def approve_pending(client: TestClient) -> None:
    headers = login(client, "manager@local.example")
    [listed] = client.get(f"{BASE}/quotations", params={"status": "pending_approval"}).json()
    task = client.get(f"{BASE}/quotations/{listed['quotation_id']}").json()["pending_task"]
    decision = client.post(
        f"{BASE}/approval-tasks/{task['task_id']}/decision",
        json={"command_id": str(uuid4()), "action": "approve",
              "content_sha256": task["content_sha256"]},
        headers=headers,
    )  # fmt: skip
    assert decision.status_code == 200, decision.text
    client.cookies.clear()


def test_a_visitor_asks_answers_and_accepts_in_the_chat(
    client: TestClient, integration_settings: Settings, worker_engine: Engine
) -> None:
    handle = enable_chat(client)
    assert client.get(f"/api/v1/public/chat/{handle}/config").json()["require"] == (
        "email_or_phone"
    )
    token = start(client, handle).json()["session_token"]

    first = say(client, handle, token, "We need 5 boxes of 2-core cable", key="msg-000001")
    assert first.status_code == 202, first.text
    replay = say(client, handle, token, "We need 5 boxes of 2-core cable", key="msg-000001")
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    drain(integration_settings, worker_engine)
    texts = [(m["direction"], m["kind"], m["text"]) for m in thread(client, handle, token)]
    assert texts[0] == ("inbound", "text", "We need 5 boxes of 2-core cable")
    assert texts[1] == ("outbound", "notice", "Thanks, we are preparing your quotation.")
    assert texts[2][1] == "questions"
    assert texts[2][2].startswith("We need a bit more information:\n- ")

    assert say(client, handle, token, "10 metres").status_code == 202
    drain(integration_settings, worker_engine)
    approve_pending(client)
    drain(integration_settings, worker_engine)

    [card] = [m for m in thread(client, handle, token) if m["kind"] == "quotation"]
    assert card["text"].startswith("Your quotation Q-") and "valid until" in card["text"]
    quotation = card["quotation"]
    assert [a["action"] for a in quotation["actions"]] == ["accept", "decline"]
    auth = {"Authorization": f"Bearer {token}"}
    pdf = client.get(quotation["document_path"], headers=auth)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    # Another visitor of the same chat cannot press this visitor's button.
    stranger = start(client, handle, name="Other", email="o@example.com").json()["session_token"]
    ref = quotation["actions"][0]["ref"]
    other = client.post(
        f"/api/v1/public/chat/{handle}/actions/{ref}",
        json={"action": "accept"},
        headers={"Authorization": f"Bearer {stranger}"},
    )
    assert other.status_code == 410

    accepted = client.post(
        f"/api/v1/public/chat/{handle}/actions/{ref}", json={"action": "accept"}, headers=auth
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["outcome"] == "accepted"
    drain(integration_settings, worker_engine)
    last = thread(client, handle, token)[-1]
    assert last["kind"] == "confirmation"
    assert last["text"].startswith("Thank you, we received your acceptance of Q-")
    [card] = [m for m in thread(client, handle, token) if m["kind"] == "quotation"]
    assert card["quotation"]["actions"] == []  # answered: no more buttons

    # STOP: consent recorded and confirmed; nothing else is sent afterwards.
    assert say(client, handle, token, "STOP").status_code == 202
    drain(integration_settings, worker_engine)
    assert thread(client, handle, token)[-1]["text"].startswith(
        "You will not receive more messages from"
    )


def test_refusals_reveal_nothing(client: TestClient) -> None:
    handle = enable_chat(client)
    assert client.get("/api/v1/public/chat/no-such-chat/config").status_code == 404
    assert start(client, handle, form_ms=500).status_code == 422  # too fast: a bot
    assert start(client, handle, website="http://spam").status_code == 422  # honeypot
    assert start(client, handle, email=None).status_code == 422  # email or phone (D12)
    other_site = client.get(
        f"/api/v1/public/chat/{handle}/config", headers={"Origin": "https://evil.example"}
    )
    assert other_site.status_code == 404
    token = start(client, handle).json()["session_token"]
    assert say(client, handle, token + "x", "hello").status_code == 401
    assert say(client, handle, "cs_garbage", "hello").status_code == 401
    ended = client.post(
        f"/api/v1/public/chat/{handle}/session/end", headers={"Authorization": f"Bearer {token}"}
    )
    assert ended.status_code == 204
    assert say(client, handle, token, "hello").status_code == 401


def test_the_daily_reading_cap_sends_the_enquiry_to_review(
    client: TestClient,
    integration_settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    handle = enable_chat(client)
    token = start(client, handle).json()["session_token"]
    say(client, handle, token, "We need 5 boxes of 2-core cable")
    drain(integration_settings, worker_engine)
    # 200 readings of chat enquiries already today (the user's limit, Q9).
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_runs (id, tenant_id, enquiry_id, graph_version, round, "
                "status) SELECT gen_random_uuid(), :t, e.id, 'enquiry-extraction-v1', r, "
                "'needs_clarification' FROM enquiries e, generate_series(100, 299) r "
                "WHERE e.channel = 'web_chat'"
            ),
            {"t": LOCAL_TENANT},
        )
    say(client, handle, token, "10 metres")
    drain(integration_settings, worker_engine)
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        status, reasons = connection.execute(
            text(
                "SELECT e.status, r.outcome->'review_reasons' FROM enquiries e "
                "JOIN workflow_runs r ON r.enquiry_id = e.id ORDER BY r.round DESC LIMIT 1"
            )
        ).one()
    assert status == "needs_review"
    assert reasons == ["Daily reading limit reached; a person should read this enquiry"]


def test_a_secure_link_shows_the_offer_and_answers_only_on_confirm(
    client: TestClient,
    integration_settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
) -> None:
    """Q1: for tools without buttons. Opening the page (link previews do) changes nothing."""
    import hashlib

    handle = enable_chat(client)
    token = start(client, handle).json()["session_token"]
    say(client, handle, token, "Please quote 2 x IND-FAN-450")
    drain(integration_settings, worker_engine)
    approve_pending(client)
    drain(integration_settings, worker_engine)
    link = "synthetic-link-token-0123456789abcdef"
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        connection.execute(
            text("UPDATE customer_action_refs SET token_sha256 = :h"),
            {"h": hashlib.sha256(link.encode()).hexdigest()},
        )

    page = client.get(f"/q/{link}")
    assert page.status_code == 200 and "Quotation Q-" in page.text
    assert page.headers["cache-control"] == "no-store"
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        assert connection.execute(text("SELECT count(*) FROM quotation_responses")).scalar() == 0

    unconfirmed = client.post(f"/api/v1/public/actions/{link}", json={"action": "accept"})
    assert unconfirmed.status_code == 422
    answered = client.post(
        f"/api/v1/public/actions/{link}", json={"action": "decline", "confirm": True}
    )
    assert answered.status_code == 200 and answered.json()["outcome"] == "declined"
    again = client.post(
        f"/api/v1/public/actions/{link}", json={"action": "accept", "confirm": True}
    )
    assert again.status_code == 409  # answered once
    assert client.get("/q/not-a-real-token").status_code == 410


def test_staff_see_the_inbox_merge_contacts_and_switch_off_the_acknowledgement(
    client: TestClient, integration_settings: Settings, worker_engine: Engine
) -> None:
    handle = enable_chat(client)
    first = start(client, handle).json()["session_token"]
    second = start(client, handle, name="Asha R", email=None, phone="+91 98765 43210").json()[
        "session_token"
    ]
    say(client, handle, first, "We need 5 boxes of 2-core cable")
    drain(integration_settings, worker_engine)

    headers = login(client, "sales@local.example")
    [talked] = client.get(f"{BASE}/conversations").json()  # the silent chat is not listed
    assert (
        talked["provider_key"] == "web_chat" and talked["enquiry_status"] == "needs_clarification"
    )
    detail = client.get(f"{BASE}/conversations/{talked['conversation_id']}").json()
    authors = [(m["direction"], m["author"]) for m in detail["messages"]]
    assert authors[0] == ("inbound", "customer") and ("outbound", "system") in authors
    assert detail["identity"]["verified"] is True and detail["identity"]["consent"] == "implied"
    thread = client.get(f"{BASE}/enquiries/{talked['enquiry_id']}/messages").json()
    assert thread[0]["text"] == "We need 5 boxes of 2-core cable"

    contacts = client.get(f"{BASE}/contacts", params={"q": "asha"}).json()
    assert {c["email"] for c in contacts} == {"asha@example.com", None}
    assert all(not c["email_verified"] for c in contacts)  # claimed only (Q2)
    source, target = (c["contact_id"] for c in contacts)
    assert client.get(f"{BASE}/contacts/{target}").json()["suggestions"] == []  # none verified
    merged = client.post(
        f"{BASE}/contacts/{source}/merge", json={"into_contact_id": target}, headers=headers
    )
    assert merged.status_code == 204
    after = client.get(f"{BASE}/contacts/{target}").json()
    assert len(after["identities"]) == 2 and len(after["conversations"]) == 2
    assert client.get(f"{BASE}/contacts/{source}").status_code == 404  # merged away
    client.cookies.clear()

    # D14: administrators switch the acknowledgement off; sales staff may not.
    admin = login(client, "admin@local.example")
    off = client.put(
        f"{BASE}/customer-messages", json={"acknowledge_enquiries": False}, headers=admin
    )
    assert off.status_code == 200
    client.cookies.clear()
    try:
        say(client, handle, second, "Please quote 2 x IND-FAN-450")
        drain(integration_settings, worker_engine)
        assert [m["kind"] for m in thread_of(client, handle, second)] == ["text"]
    finally:  # the demo business is shared by later tests: switch it back on
        admin = login(client, "admin@local.example")
        client.put(f"{BASE}/customer-messages", json={"acknowledge_enquiries": True}, headers=admin)
        client.cookies.clear()


def thread_of(client: TestClient, handle: str, token: str) -> list[dict[str, Any]]:
    return thread(client, handle, token)


def test_the_hosted_chat_page_loads_the_widget_in_page_mode(client: TestClient) -> None:
    handle = enable_chat(client)
    page = client.get(f"/chat/{handle}")
    assert page.status_code == 200
    assert f"data-chat='{handle}' data-mode='page'" in page.text
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert client.get("/chat/no-such-chat").status_code == 404


def test_an_enquiry_stored_before_phase_1_is_still_delivered(
    client: TestClient,
    integration_settings: Settings,
    worker_engine: Engine,
    owner_engine: Engine,
    make_api_key: Any,
) -> None:
    """Regression (found in the phase 4 live run): an envelope without reply_to (written by
    an older process) made the approved quotation's delivery fail on a NULL recipient."""
    from sales_platform.domain.tenancy import Role

    token = make_api_key(LOCAL_TENANT, Role.INTAKE_SERVICE)
    response = client.post(
        "/api/v1/enquiries",
        json={"text": "Please quote 2 x IND-FAN-450"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": f"old-{uuid4()}"},
    )
    enquiry_id = response.json()["enquiry_id"]
    with tenant_transaction(owner_engine, LOCAL_TENANT) as connection:
        connection.execute(
            text(
                "UPDATE inbox_events SET envelope = envelope - 'reply_to' WHERE id = "
                "(SELECT inbox_event_id FROM enquiries WHERE id = :e)"
            ),
            {"e": enquiry_id},
        )
    drain(integration_settings, worker_engine)
    approve_pending(client)
    drain(integration_settings, worker_engine)
    issued = client.get(
        f"/api/v1/enquiries/{enquiry_id}/quotation", headers={"Authorization": f"Bearer {token}"}
    )
    assert issued.status_code == 200 and issued.json()["status"] == "open"
