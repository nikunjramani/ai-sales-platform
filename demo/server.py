"""Two-window demo page: customer chat on the left, manager phone on the right.

Development only. It talks to the real running API (default http://127.0.0.1:8000):
- the customer side uses an intake API key that this server creates and keeps in
  var/demo/state.json, so the browser never sees a key;
- the manager side is the simulated messaging channel: review messages are read from the local
  outbox and decisions are sent as signed events to the generic hook route
  (/api/v1/hooks/{connection}/{token}), exactly like scripts/simulated_channel.py; the worker
  records them a moment later.

Start everything with: uv run python demo/run_demo.py   then open http://127.0.0.1:8001
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from quotation_app.adapters.providers.simulated import DEFAULT_CONNECTION_ID, PROVIDER_KEY
from quotation_app.adapters.providers.simulated.signing import SIGNATURE_HEADER, hook_token, sign
from quotation_app.application.api_keys import generate_api_key
from quotation_app.config import Settings
from quotation_app.domain.tenancy import Role
from quotation_app.persistence.api_keys import insert_api_key
from quotation_app.persistence.connections import SqlConnectionStore, SqlHostedChatAdmin
from quotation_app.persistence.database import build_engine
from quotation_app.persistence.seed import INDUSTRIAL_TENANT

API = os.environ.get("DEMO_API", "http://127.0.0.1:8000")
MANAGER = "manager@industrial.example"
PHONE = "demo-manager-phone"
HERE = Path(__file__).parent
DEMO_ORIGIN = "http://127.0.0.1:8001"

settings = Settings()
if settings.environment == "production":
    raise SystemExit("The demo page is for development only.")
STATE_FILE = settings.storage_dir.parent / "demo" / "state.json"
OUTBOX = settings.storage_dir / "simulated-channel" / str(INDUSTRIAL_TENANT) / PHONE


# ---------------------------------------------------------------- small helpers


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        state: dict[str, Any] = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state
    return {"token": None, "enquiries": [], "decisions": {}, "linked": False}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def call(
    method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, bytes, Any]:
    """Call the real API. Returns (status, raw body, response headers)."""
    data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(API + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers
    except urllib.error.URLError:
        raise HTTPException(503, f"The API is not running at {API}") from None


def as_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw.decode(errors="replace")}


def customer_headers(state: dict[str, Any]) -> dict[str, str]:
    if not state["token"]:
        key = generate_api_key()
        engine = build_engine(settings.migration_url())
        try:
            insert_api_key(
                engine,
                key.key_id,
                INDUSTRIAL_TENANT,
                "demo page",
                Role.INTAKE_SERVICE,
                key.secret_sha256,
            )
        finally:
            engine.dispose()
        state["token"] = key.token
        save_state(state)
    return {"Authorization": f"Bearer {state['token']}"}


def secret() -> str:
    if settings.simulated_channel_secret is None:
        raise HTTPException(500, "QUOTATION_SIMULATED_CHANNEL_SECRET missing in .env")
    return settings.simulated_channel_secret.get_secret_value()


def hook_path() -> str:
    """The demo phone connection's webhook path (the seed gives it its token)."""
    engine = build_engine(settings.migration_url())
    try:
        connection = SqlConnectionStore(engine).find_by_account(
            INDUSTRIAL_TENANT, PROVIDER_KEY, DEFAULT_CONNECTION_ID
        )
    finally:
        engine.dispose()
    if connection is None:
        raise HTTPException(500, "No demo phone connection; run the seed first")
    return (
        f"/api/v1/hooks/{connection.connection_id}/{hook_token(secret(), connection.connection_id)}"
    )


def send_event(event: dict[str, Any]) -> tuple[int, Any]:
    """Send one signed phone event; the API answers "received" and the worker records it."""
    body = json.dumps({"event_id": f"demo-{uuid4()}", "external_user_id": PHONE, **event}).encode()
    status, raw, _ = call("POST", hook_path(), body, {SIGNATURE_HEADER: sign(secret(), body)})
    return status, as_json(raw)


def wait_until(check: Any, seconds: float = 15) -> bool:
    """The worker processes events within a few seconds; wait for the result to appear."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.5)
    return False


def link_manager_phone(state: dict[str, Any]) -> None:
    """Link the demo phone to the manager once (portal code + signed enroll event)."""
    if state.get("linked"):
        return
    status, raw, headers = call("POST", "/api/v1/auth/dev-login", {"subject": MANAGER})
    if status != 200:
        raise HTTPException(502, f"Manager dev sign-in failed ({status})")
    cookie = headers.get("Set-Cookie", "").split(";")[0]
    session = {"Cookie": cookie, "X-CSRF-Token": as_json(raw)["csrf_token"]}
    tenant = f"/api/v1/tenants/{INDUSTRIAL_TENANT}"
    _, raw, _ = call("GET", f"{tenant}/channel-bindings", headers=session)
    bindings = as_json(raw)
    if not any(b["external_user_id"] == PHONE and not b["revoked_at"] for b in bindings):
        status, raw, _ = call(
            "POST", f"{tenant}/channel-enrollments", {"channel": "simulated"}, session
        )
        if status != 201:
            raise HTTPException(502, f"Could not start linking ({status}): {raw!r}")
        status, result = send_event({"type": "enroll", "code": as_json(raw)["code"]})
        if status != 200:
            raise HTTPException(502, f"Could not link the manager phone ({status}): {result}")

        def linked() -> bool:
            _, listed, _ = call("GET", f"{tenant}/channel-bindings", headers=session)
            items = as_json(listed)
            return any(b["external_user_id"] == PHONE and not b["revoked_at"] for b in items)

        if not wait_until(linked):
            raise HTTPException(504, "The worker did not link the phone; is it running?")
    call("POST", "/api/v1/auth/logout", headers=session)
    state["linked"] = True
    save_state(state)


# ---------------------------------------------------------------- the demo app

app = FastAPI(title="Quotation demo")


class EnquiryIn(BaseModel):
    text: str


class AnswerIn(BaseModel):
    action: str  # accept | decline
    reason: str | None = None


class DecisionIn(BaseModel):
    task_id: str
    action: str  # approve | reject | request_revision
    reason: str | None = None


@app.get("/")
def page() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/demo/chat")
def website_chat() -> dict[str, str]:
    """The industrial demo business's website chat (phase 4), enabled once like the portal's
    Enable button would, with this demo page allowed to show it."""
    state = load_state()
    handle = state.get("chat_handle")
    if handle is None:
        engine = build_engine(settings.migration_url())
        chat = next(
            (
                row
                for row in SqlConnectionStore(engine).list_for(INDUSTRIAL_TENANT)
                if row.provider_key == "web_chat" and row.active and row.public_handle
            ),
            None,
        )
        if chat is None:
            admin = SqlHostedChatAdmin(engine)
            connection_id = admin.create(
                INDUSTRIAL_TENANT,
                "web_chat",
                "Website chat",
                ["customer_messages"],
                {"allowed_origins": [DEMO_ORIGIN], "greeting": "Hello! What can we quote for you?"},
                "demo",
            )
            chat = SqlConnectionStore(engine).get(INDUSTRIAL_TENANT, connection_id)
        engine.dispose()
        assert chat is not None and chat.public_handle is not None
        handle = chat.public_handle
        state["chat_handle"] = handle
        save_state(state)
    return {"chat_url": f"{API}/chat/{handle}"}


@app.post("/demo/customer/enquiries")
def send_enquiry(body: EnquiryIn) -> Any:
    state = load_state()
    link_manager_phone(state)  # the review message only reaches phones linked beforehand
    status, raw, _ = call(
        "POST",
        "/api/v1/enquiries",
        {"text": body.text, "customer_reference": "DEMO-CUSTOMER"},
        {**customer_headers(state), "Idempotency-Key": f"demo-{uuid4()}"},
    )
    result = as_json(raw)
    if status not in (200, 202):
        raise HTTPException(status, result.get("detail", result))
    # A follow-up continues the open enquiry (phase 4): it is listed once, with all texts.
    if not result.get("continued"):
        state["enquiries"].append(result["enquiry_id"])
    texts = state.setdefault("texts", {})
    previous = texts.get(result["enquiry_id"])
    texts[result["enquiry_id"]] = body.text if previous is None else f"{previous}\n{body.text}"
    save_state(state)
    return result


@app.get("/demo/customer/enquiries")
def customer_view() -> list[dict[str, Any]]:
    state = load_state()
    if not state["enquiries"]:
        return []
    headers = customer_headers(state)
    items = []
    for enquiry_id in state["enquiries"]:
        _, raw, _ = call("GET", f"/api/v1/enquiries/{enquiry_id}", headers=headers)
        enquiry = as_json(raw)
        status, raw, _ = call("GET", f"/api/v1/enquiries/{enquiry_id}/quotation", headers=headers)
        items.append(
            {
                "enquiry_id": enquiry_id,
                "text": state.get("texts", {}).get(enquiry_id, ""),
                "status": enquiry.get("status"),
                "received_at": enquiry.get("received_at"),
                "extraction": enquiry.get("extraction"),
                "quotation": as_json(raw) if status == 200 else None,
            }
        )
    return items


@app.get("/demo/customer/enquiries/{enquiry_id}/quotation.pdf")
def customer_pdf(enquiry_id: UUID) -> Response:
    state = load_state()
    status, raw, _ = call(
        "GET", f"/api/v1/enquiries/{enquiry_id}/quotation.pdf", headers=customer_headers(state)
    )
    if status != 200:
        raise HTTPException(status, "No approved PDF yet")
    return Response(raw, media_type="application/pdf")


@app.post("/demo/customer/enquiries/{enquiry_id}/answer")
def customer_answer(enquiry_id: UUID, body: AnswerIn) -> Any:
    state = load_state()
    headers = customer_headers(state)
    status, raw, _ = call("GET", f"/api/v1/enquiries/{enquiry_id}/quotation", headers=headers)
    if status != 200:
        raise HTTPException(status, "No quotation to answer")
    offer = as_json(raw)
    answer = {"action": body.action, "version": offer["version"]}
    answer["content_sha256"] = offer["content_sha256"]
    if body.reason:
        answer["reason"] = body.reason
    status, raw, _ = call(
        "POST",
        offer["response_path"],
        answer,
        {**headers, "Idempotency-Key": f"demo-{enquiry_id}-{offer['version']}-{body.action}"},
    )
    result = as_json(raw)
    if status != 200:
        raise HTTPException(status, result.get("detail", result))
    return result


@app.get("/demo/manager/messages")
def manager_messages() -> list[dict[str, Any]]:
    state = load_state()
    messages = []
    if OUTBOX.exists():
        for path in sorted(OUTBOX.glob("*.json"), key=lambda p: p.stat().st_mtime):
            message = json.loads(path.read_text(encoding="utf-8"))
            message.pop("action_reference", None)  # stays on the server, like a button payload
            message["kind"] = "notice" if "notice_id" in message else "review"
            message["decision"] = state["decisions"].get(message.get("task_id", ""))
            messages.append(message)
    return messages


@app.post("/demo/manager/decide")
def manager_decide(body: DecisionIn) -> Any:
    path = OUTBOX / f"{Path(body.task_id).name}.json"
    if not path.exists():
        raise HTTPException(404, "No review message for this task")
    message = json.loads(path.read_text(encoding="utf-8"))
    status, result = send_event(
        {
            "type": "decision",
            "action_reference": message["action_reference"],
            "action": body.action,
            "reason": body.reason,
        }
    )
    if status != 200:
        raise HTTPException(status, result.get("detail", result))
    state = load_state()
    state["decisions"][body.task_id] = body.action
    save_state(state)
    return result


@app.post("/demo/reset")
def reset() -> dict[str, str]:
    """Clear both windows (the records stay in the database and the portal)."""
    state = load_state()
    state["enquiries"], state["decisions"], state["texts"] = [], {}, {}
    state["token"], state["linked"] = None, False  # a fresh key and link, e.g. after a DB reset
    save_state(state)
    if OUTBOX.exists():
        for path in OUTBOX.glob("*.json"):
            path.unlink()
    return {"result": "cleared"}
