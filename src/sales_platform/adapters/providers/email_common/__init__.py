"""Shared email helpers for any future email provider package (phase 5, user answer 2026-10-29).

No email provider exists yet; these pure functions (standard library only) are what every one
needs, so a package only adds its HTTP client:
- threading: which of our messages a reply answers (In-Reply-To / References), and the
  Message-ID we give our own messages;
- new_text: only the new words of a reply (quoted history, "On ... wrote:", signatures and
  forwarded blocks removed);
- automatic: auto-replies, bounces and bulk mail, which are never read as people;
- trusted_sender: the From address counts only when DKIM or SPF passed for a domain aligned
  with it (DMARC-style). A bare From header is never enough to enroll or approve.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import getaddresses, parseaddr

_MESSAGE_ID = re.compile(r"<([^<>\s]+@[^<>\s]+)>")
_WROTE = re.compile(r"^\s*(On\s.+wrote:|Am\s.+schrieb.*:|Le\s.+a écrit\s*:)\s*$", re.IGNORECASE)
_FORWARD = re.compile(
    r"^\s*(-{2,}\s*(Original Message|Forwarded message)\s*-{2,}|From:\s.+|Sent from my \w+)",
    re.IGNORECASE,
)
_RESULT = re.compile(
    r"\b(dkim|spf)=(\w+)(?:[^;]*?\b(?:header\.d|header\.i|smtp\.mailfrom)=([^\s;]+))?"
)


def our_message_id(outbox_id: str, domain: str) -> str:
    """The Message-ID header of a message we send: "<outbox-id@domain>"."""
    return f"<{outbox_id}@{domain}>"


def threading(headers: Mapping[str, str]) -> list[str]:
    """Message IDs (without brackets) a message answers, newest first: In-Reply-To, then
    References from last to first. A provider maps them to our provider message IDs."""
    found: list[str] = []
    for name in ("in-reply-to", "references"):
        ids = _MESSAGE_ID.findall(headers.get(name, ""))
        found += ids if name == "in-reply-to" else list(reversed(ids))
    return list(dict.fromkeys(found))


def new_text(body: str) -> str:
    """The new part of a reply: everything before quoted history or a signature."""
    kept: list[str] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith(">") or _WROTE.match(line) or _FORWARD.match(line):
            break
        if line.rstrip() == "--":  # the standard signature delimiter "-- "
            break
        kept.append(line)
    return "\n".join(kept).strip()


def automatic(headers: Mapping[str, str], sender: str) -> bool:
    """Auto-replies, bounces and bulk mail (RFC 3834 and common practice)."""
    submitted = headers.get("auto-submitted", "no").strip().lower()
    precedence = headers.get("precedence", "").strip().lower()
    local = parseaddr(sender)[1].split("@", 1)[0].lower()
    return (
        submitted != "no"
        or precedence in {"bulk", "junk", "list", "auto_reply"}
        or "x-autoreply" in headers
        or "x-autorespond" in headers
        or local in {"mailer-daemon", "postmaster", "noreply", "no-reply"}
    )


@dataclass(frozen=True)
class AuthResult:
    method: str  # "dkim" | "spf"
    result: str  # "pass", "fail", ...
    domain: str | None


def auth_results(header: str) -> list[AuthResult]:
    """The dkim/spf results of an Authentication-Results header added by our mail host."""
    results = []
    for method, result, domain in _RESULT.findall(header):
        if domain and "@" in domain:
            domain = domain.rsplit("@", 1)[1]
        results.append(AuthResult(method.lower(), result.lower(), domain.lower() or None))
    return results


def aligned(domain: str, from_domain: str) -> bool:
    """Relaxed alignment: the same domain, or one a subdomain of the other."""
    domain, from_domain = domain.lower().rstrip("."), from_domain.lower().rstrip(".")
    return (
        domain == from_domain
        or domain.endswith("." + from_domain)
        or from_domain.endswith("." + domain)
    )


def trusted_sender(from_header: str, results: Sequence[AuthResult]) -> str | None:
    """The sender's address (lower case) if DKIM or SPF passed for an aligned domain."""
    addresses = [address for _, address in getaddresses([from_header]) if "@" in address]
    if len(addresses) != 1:
        return None  # no From, or several: never trusted
    address = addresses[0].lower()
    from_domain = address.rsplit("@", 1)[1]
    for item in results:
        if item.result == "pass" and item.domain and aligned(item.domain, from_domain):
            return address
    return None
