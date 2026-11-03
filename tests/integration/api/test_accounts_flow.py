"""Phase 2 end to end: a new business from zero to Go live without scripts, through the real
API, the real worker and PostgreSQL. Emails are read from the console mailer's files."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from sales_platform.adapters.interpreters.rules import RulesInterpreter
from sales_platform.api.app import create_app
from sales_platform.api.auth import CSRF_HEADER
from sales_platform.application.jobs import JobRunner
from sales_platform.composition import build_job_handlers, build_runtime
from sales_platform.config import Settings
from sales_platform.persistence.jobs import SqlJobQueue
from sales_platform.persistence.workflow import open_checkpointer

pytestmark = pytest.mark.usefixtures("fresh_accounts")

PASSWORD = "a long passphrase for tests"


@pytest.fixture
def app(integration_settings: Settings, runtime_engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(integration_settings)) as client:
        yield client


def drain(settings: Settings, engine: Engine) -> None:
    with open_checkpointer(settings.worker_url()) as checkpointer:
        runtime = build_runtime(settings, engine)
        handlers = build_job_handlers(settings, engine, runtime, checkpointer, RulesInterpreter())
        runner = JobRunner(SqlJobQueue(engine), handlers)
        while runner.run_once():
            pass


def mail(settings: Settings, to: str) -> list[str]:
    folder = Path(settings.storage_dir) / "dev-mail"
    found = (
        [p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.txt"))]
        if folder.exists()
        else []
    )
    return [text for text in found if f"To: {to}\n" in text]


def token_in(text: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_-]+)", text)
    assert match is not None, text
    return match.group(1)


def sign_up(client: TestClient, email: str) -> None:
    response = client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 202, response.text


def log_in(client: TestClient, email: str) -> dict[str, Any]:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return dict(response.json())


def verified_person(
    client: TestClient, settings: Settings, engine: Engine, email: str
) -> dict[str, str]:
    sign_up(client, email)
    drain(settings, engine)
    [message] = mail(settings, email)
    assert (
        client.post("/api/v1/auth/verify-email", json={"token": token_in(message)}).status_code
        == 200
    )
    return {CSRF_HEADER: log_in(client, email)["csrf_token"]}


def test_new_business_from_zero_to_go_live(
    app: TestClient, integration_settings: Settings, worker_engine: Engine
) -> None:
    settings, engine = integration_settings, worker_engine
    # Sign-up never tells whether an address is taken.
    sign_up(app, "ana@shop.example")
    again = app.post(
        "/api/v1/auth/signup", json={"email": "ANA@shop.example", "password": PASSWORD}
    )
    assert again.status_code == 202 and again.json() == {"status": "check_email"}
    form = app.post(
        "/api/v1/auth/signup",
        content=b"email=x@y.example&password=whatever-long",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    weak = app.post("/api/v1/auth/signup", json={"email": "z@shop.example", "password": "Zq7!x"})
    assert weak.status_code == 422 and weak.json()["detail"]["problems"] == ["too_short"]
    drain(settings, engine)
    messages = mail(settings, "ana@shop.example")
    assert len(messages) == 2  # the verification email and the "someone tried" notice
    verify = next(m for m in messages if "verify-email" in m)

    # Login works before verification (Q2), but creating a business does not.
    session = log_in(app, "ana@shop.example")
    assert session["user"]["email_verified"] is False and session["tenants"] == []
    headers = {CSRF_HEADER: session["csrf_token"]}
    new_business = {"name": "Acme Traders", "country": "IN", "time_zone": "Asia/Kolkata",
                    "pricing": {"currency": "INR", "tax_rate": "0.18"}}  # fmt: skip
    assert app.post("/api/v1/businesses", json=new_business, headers=headers).status_code == 403
    assert (
        app.post("/api/v1/auth/verify-email", json={"token": token_in(verify)}).status_code == 200
    )
    reused = app.post("/api/v1/auth/verify-email", json={"token": token_in(verify)})
    assert reused.status_code == 400
    assert app.post("/api/v1/businesses", json=new_business).status_code == 403  # no CSRF
    created = app.post("/api/v1/businesses", json=new_business, headers=headers)
    assert created.status_code == 201, created.text
    tenant = created.json()["tenant_id"]
    base = f"/api/v1/tenants/{tenant}"
    [mine] = app.get("/api/v1/auth/session").json()["tenants"]
    assert mine["status"] == "setting_up" and "members:manage" in mine["permissions"]

    # Settings, then the checklist.
    assert app.get(f"{base}/setup").json()["can_go_live"] is False
    business = app.get(f"{base}/business").json()
    profile = {"legal_name": "Acme Traders Pvt Ltd", "address": "12 Ring Road\nPune 411001",
               "tax_id_label": "GSTIN", "tax_id": "27ABCDE1234F1Z5",
               "expected_version": business["profile"]["version"]}  # fmt: skip
    assert app.put(f"{base}/business/profile", json=profile, headers=headers).status_code == 200
    stale = app.put(f"{base}/business/profile", json=profile, headers=headers)
    assert stale.status_code == 409
    pricing = app.get(f"{base}/pricing-policy").json()
    assert pricing["complete"] is False and pricing["tax_rate"] == "0.18"
    complete = {"currency": "INR", "minor_units": 2, "rounding_mode": "half_up",
                "tax_rate": "0.18", "max_price_age_seconds": 900, "max_stock_age_seconds": 300,
                "quote_validity_days": 7, "expected_version": pricing["version"]}  # fmt: skip
    assert app.put(f"{base}/pricing-policy", json=complete, headers=headers).status_code == 200
    key = app.post(f"{base}/api-keys", json={"name": "Shop website"}, headers=headers)
    assert key.status_code == 201 and key.headers["cache-control"] == "no-store"
    token = key.json()["token"]
    missing = app.post(f"{base}/go-live", headers=headers)
    assert missing.status_code == 409 and missing.json()["detail"]["missing"] == ["test_enquiry"]
    enquiry = app.post(
        "/api/v1/enquiries",
        json={"text": "2 x IND-FAN-450", "customer_reference": "TEST-1"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "test-enquiry-1"},
    )
    assert enquiry.status_code == 202, enquiry.text
    setup = app.get(f"{base}/setup").json()
    assert setup["can_go_live"] is True
    product = next(item for item in setup["items"] if item["key"] == "product_source")
    assert product == {"key": "product_source", "done": False, "required": False}
    live = app.post(f"{base}/go-live", headers=headers)
    assert live.status_code == 200 and live.json()["status"] == "active"
    events = [k["name"] for k in app.get(f"{base}/api-keys").json()]
    assert events == ["Shop website"]


def test_colleagues_join_by_invitation_and_businesses_stay_apart(
    app: TestClient, integration_settings: Settings, worker_engine: Engine
) -> None:
    settings, engine = integration_settings, worker_engine
    ana = verified_person(app, settings, engine, "ana@shop.example")
    tenant = app.post(
        "/api/v1/businesses",
        headers=ana,
        json={"name": "Acme Traders", "country": "IN", "time_zone": "Asia/Kolkata"},
    ).json()["tenant_id"]
    base = f"/api/v1/tenants/{tenant}"
    invited = app.post(f"{base}/invitations", headers=ana,
                       json={"email": "Bo@shop.example", "roles": ["sales_operator"]})  # fmt: skip
    assert invited.status_code == 202 and "token" not in invited.text
    drain(settings, engine)
    [invitation] = mail(settings, "bo@shop.example")
    link = token_in(invitation)
    assert "#/invitations/accept?token=" in invitation

    # Bo signs up (the invitation link also proves the address) and accepts.
    sign_up(app, "bo@shop.example")
    bo = {CSRF_HEADER: log_in(app, "bo@shop.example")["csrf_token"]}
    preview = app.post("/api/v1/invitations/preview", json={"token": link}).json()
    assert (
        preview["business_name"] == "Acme Traders" and preview["email_hint"] == "b***@shop.example"
    )
    assert app.get(f"{base}/business").status_code == 404  # not a member yet
    accepted = app.post("/api/v1/invitations/accept", json={"token": link}, headers=bo)
    assert accepted.status_code == 200 and accepted.json()["tenant_id"] == tenant
    assert (
        app.post("/api/v1/invitations/accept", json={"token": link}, headers=bo).status_code == 404
    )
    assert app.get(f"{base}/members").status_code == 403  # sales staff do not see colleagues

    # Ana changes Bo's roles, cannot demote herself, then removes Bo.
    ana = {CSRF_HEADER: log_in(app, "ana@shop.example")["csrf_token"]}
    members = {m["email"]: m for m in app.get(f"{base}/members").json()}
    bo_id, ana_id = members["bo@shop.example"]["user_id"], members["ana@shop.example"]["user_id"]
    assert app.put(f"{base}/members/{bo_id}/roles", headers=ana,
                   json={"roles": ["approver"]}).status_code == 204  # fmt: skip
    last = app.put(f"{base}/members/{ana_id}/roles", headers=ana, json={"roles": ["viewer"]})
    assert last.status_code == 409
    assert app.delete(f"{base}/members/{bo_id}", headers=ana).status_code == 204

    # Carl's own business: nothing of Ana's is visible, and the reverse.
    carl = verified_person(app, settings, engine, "carl@other.example")
    other = app.post(
        "/api/v1/businesses",
        headers=carl,
        json={"name": "Carl Supplies", "country": "DE", "time_zone": "Europe/Berlin"},
    ).json()["tenant_id"]
    for path in ("business", "members", "setup", "pricing-policy", "api-keys", "invitations"):
        assert app.get(f"{base}/{path}").status_code == 404, path
    assert [t["tenant_id"] for t in app.get("/api/v1/auth/session").json()["tenants"]] == [other]
    assert UUID(other) != UUID(tenant)


def test_login_answers_are_the_same_for_every_failure(
    app: TestClient, integration_settings: Settings, worker_engine: Engine
) -> None:
    verified_person(app, integration_settings, worker_engine, "ana@shop.example")
    answers = [
        app.post("/api/v1/auth/login", json={"email": email, "password": password})
        for email, password in [
            ("ana@shop.example", "wrong password, long"),
            ("nobody@shop.example", PASSWORD),
            ("not an email", PASSWORD),
        ]
    ]
    assert {(r.status_code, r.text) for r in answers} == {
        (401, '{"detail":"Email or password is incorrect"}')
    }
    forgot = [
        app.post("/api/v1/auth/password/forgot", json={"email": email})
        for email in ("ana@shop.example", "nobody@shop.example")
    ]
    assert {(r.status_code, r.text) for r in forgot} == {(202, '{"status":"check_email"}')}
