"""Play the manager's phone for the simulated approval channel (development only).

  inbox  [--user U]                        list review messages delivered to the local outbox
  enroll --code CODE --user U              link account U using a code from the portal
  decide --user U --task TASK --action A   approve / reject / request_revision from account U
         [--reason TEXT]

Events are signed with SALES_SIMULATED_CHANNEL_SECRET from .env, exactly as the API checks
them, and sent to the running API (default http://127.0.0.1:8000). Action references are read
from the outbox file and never printed.

Events go to the generic /api/v1/hooks/{connection}/{token} route every tool uses (ADR 0018;
run the seed first so the demo connection has its webhook): the answer only says "received",
and the worker records the outcome (see the portal). The old synchronous alias route was
removed in ADR 0026 step 2.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sales_platform.adapters.providers.simulated import DEFAULT_CONNECTION_ID, PROVIDER_KEY
from sales_platform.adapters.providers.simulated.signing import SIGNATURE_HEADER, hook_token, sign
from sales_platform.config import Settings
from sales_platform.persistence.connections import SqlConnectionStore
from sales_platform.persistence.database import build_engine
from sales_platform.persistence.seed import INDUSTRIAL_TENANT


def _outbox(settings: Settings) -> Path:
    return settings.storage_dir / "simulated-channel"


def _messages(settings: Settings, user: str | None) -> list[dict[str, Any]]:
    found = []
    for path in sorted(_outbox(settings).rglob("*.json")):
        message = json.loads(path.read_text(encoding="utf-8"))
        if user is None or message["to"] == user:
            found.append(message)
    return found


def _hook_url(api: str, settings: Settings, tenant_id: UUID, secret: str) -> str:
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        connection = SqlConnectionStore(engine).find_by_account(
            tenant_id, PROVIDER_KEY, DEFAULT_CONNECTION_ID
        )
    finally:
        engine.dispose()
    if connection is None:
        raise SystemExit("No simulated connection for this business; run the seed first")
    token = hook_token(secret, connection.connection_id)
    return f"{api}/api/v1/hooks/{connection.connection_id}/{token}"


def _send(url: str, secret: str, event: dict[str, Any]) -> None:
    body = json.dumps(event).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(secret, body)},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(f"{response.status}: {response.read().decode('utf-8')}")
    except urllib.error.HTTPError as error:
        print(f"{error.code}: {error.read().decode('utf-8')}", file=sys.stderr)
        raise SystemExit(1) from None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant", type=UUID, default=INDUSTRIAL_TENANT)
    commands = parser.add_subparsers(dest="command", required=True)
    inbox = commands.add_parser("inbox")
    inbox.add_argument("--user")
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--code", required=True)
    enroll.add_argument("--user", required=True)
    decide = commands.add_parser("decide")
    decide.add_argument("--user", required=True)
    decide.add_argument("--task", required=True, help="task_id shown by the inbox command")
    decide.add_argument(
        "--action", required=True, choices=["approve", "reject", "request_revision"]
    )
    decide.add_argument("--reason")
    arguments = parser.parse_args()

    settings = Settings()
    if arguments.command == "inbox":
        messages = _messages(settings, arguments.user)
        if not messages:
            print("No messages. Link an account and let the worker process a quotation.")
        for message in messages:
            if "notice_id" in message:  # information only (e.g. a customer accepted)
                print(f"--- to {message['to']} | notice")
            else:
                to, task, expires = message["to"], message["task_id"], message["expires_at"]
                print(f"--- to {to} | task {task} | expires {expires}")
            print(message["text"])
        return

    if settings.simulated_channel_secret is None:
        raise SystemExit(
            "SALES_SIMULATED_CHANNEL_SECRET is not set; run init_local_env.py --add-missing"
        )
    secret = settings.simulated_channel_secret.get_secret_value()
    event: dict[str, Any] = {"event_id": f"cli-{uuid4()}", "external_user_id": arguments.user}
    url = _hook_url(arguments.api, settings, arguments.tenant, secret)
    if arguments.command == "enroll":
        _send(url, secret, {**event, "type": "enroll", "code": arguments.code})
        return
    matching = [
        m for m in _messages(settings, arguments.user) if m.get("task_id") == arguments.task
    ]
    if not matching:
        raise SystemExit("No review message for this account and task in the outbox")
    _send(
        url,
        secret,
        {
            **event,
            "type": "decision",
            "action_reference": matching[0]["action_reference"],
            "action": arguments.action,
            "reason": arguments.reason,
        },
    )


if __name__ == "__main__":
    main()
