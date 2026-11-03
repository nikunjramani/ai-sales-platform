import threading
import time

from sales_platform.accounts.adapters.accounts.password import (
    Argon2PasswordHasher,
    load_common_passwords,
)


def light() -> Argon2PasswordHasher:
    """Small parameters keep the tests fast; production uses the defaults."""
    return Argon2PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def test_hash_verifies_and_keeps_its_parameters() -> None:
    hasher = light()
    encoded = hasher.hash("a long passphrase")
    assert encoded.startswith("$argon2id$") and "m=8,t=1,p=1" in encoded
    assert hasher.verify(encoded, "a long passphrase")
    assert not hasher.verify(encoded, "a long passphrasE")
    assert not hasher.verify("not a hash", "a long passphrase")  # no exception
    assert hasher.verify(hasher.dummy_hash, "anything") is False


def test_default_parameters_are_the_agreed_profile() -> None:
    encoded = Argon2PasswordHasher().hash("a long passphrase")
    assert "m=65536,t=3,p=4" in encoded


def test_old_parameters_need_a_rehash() -> None:
    old = light().hash("a long passphrase")
    assert Argon2PasswordHasher().needs_rehash(old)
    assert light().needs_rehash(old) is False
    assert light().needs_rehash("garbage") is True


def test_at_most_max_concurrent_hashes_run_together() -> None:
    hasher = Argon2PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, max_concurrent=1)
    active = 0
    peak = 0
    lock = threading.Lock()

    class SlowArgon2:
        def hash(self, password: str) -> str:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return "$argon2id$fake"

    hasher._argon2 = SlowArgon2()  # type: ignore[assignment]
    threads = [threading.Thread(target=hasher.hash, args=("x" * 12,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


def test_common_password_list_is_bundled_and_lower_case() -> None:
    common = load_common_passwords()
    assert len(common) >= 9_000
    assert "password" in common and "123456" in common
    assert all(entry == entry.lower() for entry in common)
