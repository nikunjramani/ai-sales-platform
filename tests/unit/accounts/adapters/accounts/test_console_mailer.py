import logging
from pathlib import Path

import pytest

from sales_platform.accounts.adapters.accounts.mail.console import ConsoleMailer, build
from sales_platform.accounts.ports.accounts import MailerEnvironment, OutgoingEmail

MESSAGE = OutgoingEmail(
    to="ana@shop.example",
    sender="no-reply@localhost.example",
    subject="Confirm your email address",
    body="Open http://localhost:8000/portal/#/verify-email?token=SECRET-TOKEN",
    idempotency_key="verify_email:1234",
)


def test_writes_one_file_per_email_and_logs_no_address_or_link(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mailer = ConsoleMailer(tmp_path)
    with caplog.at_level(logging.INFO):
        mailer.send(MESSAGE)
        mailer.send(MESSAGE)  # a retry never writes a second copy
    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 1 and files[0].name.endswith("-verify_email-1234.txt")
    content = files[0].read_text(encoding="utf-8")
    assert "To: ana@shop.example" in content and "SECRET-TOKEN" in content
    assert "ana@shop.example" not in caplog.text and "SECRET-TOKEN" not in caplog.text


def test_console_mailer_is_refused_in_production(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="production"):
        build(MailerEnvironment("production", tmp_path, {}))
    mailer = build(MailerEnvironment("development", tmp_path, {}))
    mailer.send(MESSAGE)
    assert list((tmp_path / "dev-mail").glob("*.txt"))
