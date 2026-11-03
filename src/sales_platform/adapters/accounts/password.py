"""Argon2id password hashing (argon2-cffi) and the bundled common-password list.

Argon2id is a deliberately slow, memory-hard hash: guessing passwords from a stolen database
costs an attacker a lot of time and memory per guess. Parameters (user answer, 2026-10-27):
64 MiB memory, 3 passes, 4 lanes (RFC 9106 "low memory" profile). The encoded hash stores its
own parameters and random salt, so stronger settings later only need needs_rehash on login.

Each hash needs 64 MiB, so a semaphore lets at most `max_concurrent` run at once: a flood of
logins waits instead of exhausting the server's memory (4 x 64 MiB = 256 MiB).
"""

import secrets
import threading
from importlib.resources import files

from argon2 import PasswordHasher as _Argon2
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError

TIME_COST = 3
MEMORY_COST_KIB = 64 * 1024
PARALLELISM = 4


class Argon2PasswordHasher:
    def __init__(
        self,
        *,
        time_cost: int = TIME_COST,
        memory_cost: int = MEMORY_COST_KIB,
        parallelism: int = PARALLELISM,
        max_concurrent: int = 4,
    ) -> None:
        self._argon2 = _Argon2(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._dummy = self.hash(secrets.token_urlsafe(32))

    def hash(self, password: str) -> str:
        with self._slots:
            return self._argon2.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        with self._slots:
            try:
                return self._argon2.verify(password_hash, password)
            except (VerificationError, InvalidHashError):
                return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._argon2.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True

    @property
    def dummy_hash(self) -> str:
        return self._dummy


def load_common_passwords() -> frozenset[str]:
    """SecLists' 10,000 most common passwords (MIT, see COMMON_PASSWORDS_LICENSE.txt), lower-case.

    Bundled and read offline (Q25): no password or hash prefix ever leaves the server.
    """
    text = (files(__package__) / "common_passwords.txt").read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())
