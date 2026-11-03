"""Local encrypted secret store: round trip, tenant binding, tampering, wrong key."""

from pathlib import Path
from uuid import UUID

import pytest

from sales_platform.adapters.secrets.local import LocalSecretStore, build, decode_key, new_key
from sales_platform.adapters.secrets.none import NoSecretStore
from sales_platform.ports.secrets import (
    SecretNotFound,
    SecretStoreEnvironment,
    SecretStoreUnavailable,
)

A = UUID("00000000-0000-4000-8000-00000000000a")
B = UUID("00000000-0000-4000-8000-00000000000b")


@pytest.fixture
def store(tmp_path: Path) -> LocalSecretStore:
    return LocalSecretStore(tmp_path, decode_key(new_key()))


def test_round_trip_and_nothing_readable_on_disk(store: LocalSecretStore, tmp_path: Path) -> None:
    reference = store.put(A, "signing_secret", "synthetic-signing-secret")
    assert reference.startswith("local:")
    assert store.get(A, reference) == "synthetic-signing-secret"
    [file] = (tmp_path / "secrets" / str(A)).iterdir()
    assert b"synthetic-signing-secret" not in file.read_bytes()
    assert "key" not in repr(store).replace("<key hidden>", "")


def test_another_tenant_cannot_read_it_even_with_the_file(
    store: LocalSecretStore, tmp_path: Path
) -> None:
    reference = store.put(A, "token", "value")
    with pytest.raises(SecretNotFound):
        store.get(B, reference)
    folder = tmp_path / "secrets"
    (folder / str(B)).mkdir()
    name = reference.removeprefix("local:") + ".bin"
    (folder / str(B) / name).write_bytes((folder / str(A) / name).read_bytes())
    with pytest.raises(SecretNotFound):
        store.get(B, reference)  # copied file: bound to tenant A, does not decrypt


def test_tampered_file_wrong_key_and_bad_reference(store: LocalSecretStore, tmp_path: Path) -> None:
    reference = store.put(A, "token", "value")
    path = tmp_path / "secrets" / str(A) / (reference.removeprefix("local:") + ".bin")
    with pytest.raises(SecretNotFound):
        LocalSecretStore(tmp_path, decode_key(new_key())).get(A, reference)
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(bytes(data))
    with pytest.raises(SecretNotFound):
        store.get(A, reference)
    for bad in ("local:../../etc", "cloud:abc", "local:not-a-uuid"):
        with pytest.raises(SecretNotFound):
            store.get(A, bad)


def test_delete_is_idempotent(store: LocalSecretStore) -> None:
    reference = store.put(A, "token", "value")
    store.delete(A, reference)
    store.delete(A, reference)
    with pytest.raises(SecretNotFound):
        store.get(A, reference)


def test_build_needs_a_valid_key_and_refuses_production(tmp_path: Path) -> None:
    def environment(name: str, key: str | None) -> SecretStoreEnvironment:
        return SecretStoreEnvironment(name, tmp_path, key)

    assert isinstance(build(environment("development", new_key())), LocalSecretStore)
    with pytest.raises(SecretStoreUnavailable, match="SECRET_STORE_KEY"):
        build(environment("development", None))
    with pytest.raises(SecretStoreUnavailable, match="production"):
        build(environment("production", new_key()))
    with pytest.raises(ValueError, match="32 bytes"):
        build(environment("development", "c2hvcnQ="))


def test_none_store_fails_clearly() -> None:
    with pytest.raises(SecretStoreUnavailable, match="D24"):
        NoSecretStore().get(A, "local:x")
