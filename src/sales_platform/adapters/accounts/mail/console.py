"""Development mailer: writes each account email to a text file instead of sending it.

Files go to <storage_dir>/dev-mail/<UTC time>-<idempotency key>.txt, so a developer can open
the verification or invitation link from there. The log line names only the file, never the
address or the link (the link holds a single-use token). Refused in production, where a real
sender (phase 5) must be installed and chosen with SALES_MAILER.
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from sales_platform.ports.accounts import MailerEnvironment, OutgoingEmail

logger = logging.getLogger(__name__)
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class ConsoleMailer:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def send(self, message: OutgoingEmail) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        key = _UNSAFE.sub("-", message.idempotency_key)
        if any(self._directory.glob(f"*-{key}.txt")):
            return  # already written: the same email is never written twice
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = self._directory / f"{stamp}-{key}.txt"
        content = (
            f"From: {message.sender}\nTo: {message.to}\nSubject: {message.subject}\n\n"
            f"{message.body}\n"
        )
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        logger.info("Account email written to %s", path.name)


def build(environment: MailerEnvironment) -> ConsoleMailer:
    """Entry point `console` in group sales_platform.mailers."""
    if environment.environment == "production":
        raise ValueError("The console mailer cannot be used in production")
    return ConsoleMailer(environment.storage_dir / "dev-mail")
