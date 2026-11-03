from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from sales_platform.accounts.domain.accounts import (
    PASSWORD_POLICY,
    REQUIRED_FOR_GO_LIVE,
    TOKEN_LIFETIME,
    AccountEmailPurpose,
    InvalidEmail,
    Invitation,
    InvitationStatus,
    LastAdministrator,
    PasswordProblem,
    SetupItem,
    SetupItemKey,
    check_administrator_remains,
    clean_display_name,
    email_hint,
    missing_for_go_live,
    normalize_email,
    password_problems,
    user_subject,
    validate_people_roles,
)
from sales_platform.platform.domain.tenancy import Role

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ana@Shop.Example", "ana@shop.example"),
        ("  ana@shop.example \n", "ana@shop.example"),
        ("ANA+quotes@shop.example", "ana+quotes@shop.example"),  # no plus folding
        ("a.na@gmail.com", "a.na@gmail.com"),  # no dot folding
        ("ana@Bücher.example", "ana@xn--bcher-kva.example"),  # IDN as punycode
        ("ａｎａ@shop.example", "ana@shop.example"),  # NFKC: full-width letters
    ],
)
def test_normalize_email(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "no-at-sign",
        "two@@shop.example",
        "a@b@shop.example",
        "@shop.example",
        "ana@",
        "an a@shop.example",
        "anä@shop.example",  # non-ASCII local part
        "ana@shop..example",
        "a" * 65 + "@shop.example",
        "a@" + "b" * 250 + ".example",
    ],
)
def test_normalize_email_refuses(raw: str) -> None:
    with pytest.raises(InvalidEmail):
        normalize_email(raw)


def test_email_hint_and_subject() -> None:
    assert email_hint("ana@shop.example") == "a***@shop.example"
    user_id = uuid4()
    assert user_subject(user_id) == f"user:{user_id}"


def test_display_name_is_one_line() -> None:
    assert clean_display_name("  Ana \r\n Lopez ") == "Ana Lopez"
    assert clean_display_name("   ") is None
    assert clean_display_name(None) is None
    with pytest.raises(ValueError):
        clean_display_name("x" * 101)


def problems(password: str, email: str = "ana.lopez@shop.example") -> list[PasswordProblem]:
    return password_problems(
        password, email=email, policy=PASSWORD_POLICY, common=frozenset({"correcthorse12"})
    )


def test_password_rules() -> None:
    assert PASSWORD_POLICY.min_length == 12 and PASSWORD_POLICY.max_length == 128
    assert problems("a long passphrase") == []
    assert problems("short") == [PasswordProblem.TOO_SHORT]
    assert problems("x" * 129) == [PasswordProblem.TOO_LONG]
    assert problems("CorrectHorse12") == [PasswordProblem.TOO_COMMON]
    assert problems("my Ana.Lopez password") == [PasswordProblem.CONTAINS_EMAIL]
    # A very short email name is not checked (it would refuse almost everything).
    assert problems("a long passphrase", email="al@shop.example") == []


def test_token_lifetimes() -> None:
    assert TOKEN_LIFETIME[AccountEmailPurpose.VERIFY_EMAIL] == timedelta(hours=24)
    assert TOKEN_LIFETIME[AccountEmailPurpose.PASSWORD_RESET] == timedelta(minutes=30)
    assert TOKEN_LIFETIME[AccountEmailPurpose.INVITATION] == timedelta(days=7)
    assert AccountEmailPurpose.ACCOUNT_EXISTS not in TOKEN_LIFETIME


def invitation(**changes: object) -> Invitation:
    values: dict[str, object] = {
        "invitation_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "bo@shop.example",
        "roles": frozenset({Role.APPROVER}),
        "invited_by": "user:x",
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "accepted_at": None,
        "revoked_at": None,
        "email_status": "sent",
    }
    values.update(changes)
    return Invitation(**values)  # type: ignore[arg-type]


def test_invitation_status() -> None:
    assert invitation().status(NOW) is InvitationStatus.PENDING
    assert invitation(expires_at=None).status(NOW) is InvitationStatus.PENDING  # not sent yet
    assert invitation().status(NOW + timedelta(days=7)) is InvitationStatus.EXPIRED
    assert invitation(accepted_at=NOW).status(NOW) is InvitationStatus.ACCEPTED
    assert invitation(revoked_at=NOW).status(NOW) is InvitationStatus.REVOKED


def test_people_roles() -> None:
    assert validate_people_roles(["approver", Role.SALES_OPERATOR]) == {
        Role.APPROVER,
        Role.SALES_OPERATOR,
    }
    with pytest.raises(ValueError, match="at least one"):
        validate_people_roles([])
    with pytest.raises(ValueError, match="cannot be given"):
        validate_people_roles([Role.INTAKE_SERVICE])
    with pytest.raises(ValueError, match="Unknown"):
        validate_people_roles(["owner"])


def test_last_administrator_rule() -> None:
    admin = frozenset({Role.ADMINISTRATOR})
    one = {"a": admin, "b": frozenset({Role.APPROVER})}
    with pytest.raises(LastAdministrator):
        check_administrator_remains(one, "a", frozenset())  # removal
    with pytest.raises(LastAdministrator):
        check_administrator_remains(one, "a", frozenset({Role.VIEWER}))  # demotion
    check_administrator_remains(one, "b", frozenset())
    check_administrator_remains({"a": admin, "c": admin}, "a", frozenset())


def test_go_live_needs_everything_but_the_product_source() -> None:
    assert SetupItemKey.PRODUCT_SOURCE not in REQUIRED_FOR_GO_LIVE
    items = [SetupItem(key, done=True) for key in SetupItemKey]
    assert missing_for_go_live(items) == []
    items = [SetupItem(key, done=key is SetupItemKey.PRICING) for key in SetupItemKey]
    assert missing_for_go_live(items) == [
        SetupItemKey.BUSINESS_DETAILS,
        SetupItemKey.PDF_DETAILS,
        SetupItemKey.APPROVER,
        SetupItemKey.CUSTOMER_CHANNEL,
        SetupItemKey.TEST_ENQUIRY,
    ]
    assert SetupItem(SetupItemKey.PRODUCT_SOURCE, done=False).required is False
    assert missing_for_go_live([]) == sorted(REQUIRED_FOR_GO_LIVE, key=list(SetupItemKey).index)
