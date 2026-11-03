"""Development login: choose one of the stored synthetic members, with no password.

It proves nothing about who is at the keyboard, so configuration refuses it in production.
It still uses stored memberships, so only existing members can sign in and permissions apply
exactly as they will with a real identity provider.
"""

from sales_platform.ports.identity import IdentityRejected, VerifiedIdentity
from sales_platform.ports.tenancy import MembershipDirectory

PROVIDER = "dev"


class DevIdentityProvider:
    def __init__(self, directory: MembershipDirectory) -> None:
        self._directory = directory

    def verify(self, subject: str) -> VerifiedIdentity:
        subject = subject.strip()
        if not subject or len(subject) > 200 or not self._directory.tenants_for(subject):
            raise IdentityRejected("Sign-in failed")
        return VerifiedIdentity(subject=subject, provider=PROVIDER)
