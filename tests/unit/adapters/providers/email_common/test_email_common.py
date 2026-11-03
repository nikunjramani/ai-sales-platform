"""Email helpers for future email packages: threading, new text, automatic mail, trust."""

from sales_platform.adapters.providers.email_common import (
    auth_results,
    automatic,
    new_text,
    our_message_id,
    threading,
    trusted_sender,
)


def test_a_reply_names_the_message_it_answers_newest_first() -> None:
    ours = our_message_id("6f1c", "mail.example.com")
    headers = {
        "in-reply-to": ours,
        "references": "<root@x.example> <middle@x.example> " + ours,
    }
    assert threading(headers) == ["6f1c@mail.example.com", "middle@x.example", "root@x.example"]
    assert threading({}) == []


def test_only_the_new_words_of_a_reply_are_kept() -> None:
    body = (
        "No, too expensive.\r\n"
        "Please check with me first.\r\n"
        "\r\n"
        "On Tue, 27 Oct 2026 at 10:00, Quotations <acme@in.example.com> wrote:\r\n"
        "> Reply APPROVE to approve\r\n"
        "> approved\r\n"
    )
    assert new_text(body) == "No, too expensive.\nPlease check with me first."
    assert new_text("ok\n-- \nPriya, Sales Manager") == "ok"
    assert new_text("change qty\n-----Original Message-----\nFrom: us") == "change qty"
    assert new_text("approved\n\nSent from my iPhone") == "approved"


def test_auto_replies_bounces_and_bulk_mail_are_not_people() -> None:
    assert automatic({"auto-submitted": "auto-replied"}, "a@b.example")
    assert automatic({"precedence": "bulk"}, "a@b.example")
    assert automatic({}, "MAILER-DAEMON@mx.example")
    assert not automatic({"auto-submitted": "no"}, "priya@acme.example")


def test_a_sender_is_trusted_only_with_an_aligned_pass() -> None:
    passed = auth_results(
        "mx.example; dkim=pass header.d=acme.example; spf=fail smtp.mailfrom=bounce@other.example"
    )
    assert trusted_sender("Priya <Priya@Acme.Example>", passed) == "priya@acme.example"
    subdomain = auth_results("mx.example; dkim=pass header.d=mail.acme.example")
    assert trusted_sender("priya@acme.example", subdomain) == "priya@acme.example"
    other = auth_results("mx.example; dkim=pass header.d=attacker.example")
    assert trusted_sender("priya@acme.example", other) is None  # signed, but not by acme
    failed = auth_results("mx.example; dkim=fail header.d=acme.example; spf=softfail")
    assert trusted_sender("priya@acme.example", failed) is None
    assert trusted_sender("a@acme.example, b@acme.example", passed) is None
    assert trusted_sender("", passed) is None
