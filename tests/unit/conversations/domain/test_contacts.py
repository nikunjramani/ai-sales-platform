"""Contact details, masking and consent words (phase 4: D12, D15, Q2, Q12)."""

import pytest

from sales_platform.conversations.domain.contacts import (
    ClaimedDetails,
    ConsentStatus,
    InvalidContact,
    consent_command,
    mask_email,
    mask_phone,
    may_receive,
    normalise_phone,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("STOP", ConsentStatus.OPTED_OUT),
        ("  stop ", ConsentStatus.OPTED_OUT),
        ("Unsubscribe", ConsentStatus.OPTED_OUT),
        ("opt   OUT", ConsentStatus.OPTED_OUT),
        ("START", ConsentStatus.OPTED_IN),
        ("stop the pumps", None),
        ("please stop", None),
        ("STOP.", None),
    ],
)
def test_consent_words_only_as_the_whole_message(text: str, expected: ConsentStatus | None) -> None:
    assert consent_command(text) is expected


def test_opted_out_identities_only_get_the_confirmation() -> None:
    assert not may_receive(ConsentStatus.OPTED_OUT, is_opt_out_confirmation=False)
    assert may_receive(ConsentStatus.OPTED_OUT, is_opt_out_confirmation=True)
    assert may_receive(ConsentStatus.IMPLIED, is_opt_out_confirmation=False)
    assert may_receive(ConsentStatus.OPTED_IN, is_opt_out_confirmation=False)


def test_chat_visitors_need_an_email_or_a_phone() -> None:
    with pytest.raises(InvalidContact):
        ClaimedDetails.parse("Asha", None, "  ")
    details = ClaimedDetails.parse("  Asha  Rao ", " Asha@Example.COM ", None)
    assert details == ClaimedDetails("Asha Rao", "asha@example.com", None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+91 98765-43210", "+919876543210"), ("+44 (20) 7946.0958", "+442079460958")],
)
def test_phone_numbers_are_kept_in_e164_shape(raw: str, expected: str) -> None:
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", ["9876543210", "+12", "+91 98765 4321O", "+1234567890123456"])
def test_phone_numbers_without_country_code_or_wrong_length_are_refused(raw: str) -> None:
    with pytest.raises(InvalidContact):
        normalise_phone(raw)


def test_masking_for_viewers() -> None:
    assert mask_email("asha@example.com") == "a***@example.com"
    assert mask_phone("+919876543210") == "+91*******210"
    assert mask_email(None) is None and mask_phone(None) is None
