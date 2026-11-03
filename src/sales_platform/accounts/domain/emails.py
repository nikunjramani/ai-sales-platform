"""Account emails: which template each purpose uses, and their fixed wording (moved from the
outbox's templates, ADR 0026). Rendered here, sent by AccountEmailHandler."""

from collections.abc import Mapping

from sales_platform.accounts.domain.accounts import TOKEN_LIFETIME, AccountEmailPurpose
from sales_platform.integrations.domain.messages import Rendered, TemplateKey, single_line

ACCOUNT_TEMPLATES: Mapping[AccountEmailPurpose, TemplateKey] = {
    AccountEmailPurpose.VERIFY_EMAIL: TemplateKey.ACCOUNT_VERIFY_EMAIL,
    AccountEmailPurpose.PASSWORD_RESET: TemplateKey.ACCOUNT_PASSWORD_RESET,
    AccountEmailPurpose.INVITATION: TemplateKey.ACCOUNT_INVITATION,
    AccountEmailPurpose.ACCOUNT_EXISTS: TemplateKey.ACCOUNT_EXISTS,
    AccountEmailPurpose.PASSWORD_CHANGED: TemplateKey.ACCOUNT_PASSWORD_CHANGED,
}


def _lifetime(purpose: AccountEmailPurpose) -> str:
    seconds = int(TOKEN_LIFETIME[purpose].total_seconds())
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{seconds // 60} minutes"


# Subject and body per account email. {link} is the portal link (with the single-use token for
# purposes that have one), {business} the business name, {roles} the invited roles.
_ACCOUNT_WORDING: Mapping[TemplateKey, tuple[str, str]] = {
    TemplateKey.ACCOUNT_VERIFY_EMAIL: (
        "Confirm your email address",
        "Please confirm your email address by opening this link within "
        + _lifetime(AccountEmailPurpose.VERIFY_EMAIL)
        + ":\n{link}\n\nIf you did not sign up, you can ignore this email.",
    ),
    TemplateKey.ACCOUNT_PASSWORD_RESET: (
        "Reset your password",
        "Open this link within "
        + _lifetime(AccountEmailPurpose.PASSWORD_RESET)
        + " to choose a new password:\n{link}\n\n"
        "If you did not ask for this, you can ignore this email; your password stays the same.",
    ),
    TemplateKey.ACCOUNT_INVITATION: (
        "You are invited to {business}",
        "You are invited to join {business} as {roles}. Open this link within "
        + _lifetime(AccountEmailPurpose.INVITATION)
        + " to accept:\n{link}\n\n"
        "You need to sign in, or sign up, with this email address.",
    ),
    TemplateKey.ACCOUNT_EXISTS: (
        "Someone tried to sign up with your email address",
        "Someone tried to create an account with this email address, but it already has one. "
        "If it was you, sign in or reset your password here:\n{link}\n\n"
        "Otherwise you can ignore this email.",
    ),
    TemplateKey.ACCOUNT_PASSWORD_CHANGED: (
        "Your password was changed",
        "The password of your account was changed and your other sessions were signed out. "
        "If you did not do this, reset your password here:\n{link}",
    ),
}


def _account_email(key: TemplateKey, params: Mapping[str, object]) -> Rendered:
    values = {
        name: single_line(str(params.get(name, ""))) for name in ("link", "business", "roles")
    }
    subject, body = _ACCOUNT_WORDING[key]
    return Rendered(
        body.format(**values),
        {"subject": single_line(subject.format(**values))},
    )


def render_account_email(purpose: AccountEmailPurpose, params: Mapping[str, object]) -> Rendered:
    """Subject (metadata "subject") and body of one account email."""
    return _account_email(ACCOUNT_TEMPLATES[purpose], params)
