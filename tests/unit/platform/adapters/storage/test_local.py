from pathlib import Path
from uuid import uuid4

import pytest

from sales_platform.enquiries.domain.attachments import StorageArea, attachment_key
from sales_platform.platform.adapters.storage.local import LocalObjectStorage
from sales_platform.platform.ports.storage import ObjectNotFound


@pytest.fixture
def local(tmp_path: Path) -> LocalObjectStorage:
    return LocalObjectStorage(tmp_path)


def keys() -> tuple[str, str]:
    tenant, attachment = uuid4(), uuid4()
    return (
        attachment_key(tenant, attachment, StorageArea.QUARANTINE),
        attachment_key(tenant, attachment, StorageArea.CLEAN),
    )


def test_put_get_move_delete(local: LocalObjectStorage, tmp_path: Path) -> None:
    quarantine, clean = keys()
    local.put(quarantine, b"data")
    local.put(quarantine, b"data")  # identical re-put is allowed
    assert local.get(quarantine) == b"data"
    local.move(quarantine, clean)
    assert not local.exists(quarantine) and local.get(clean) == b"data"
    local.delete(clean)
    local.delete(clean)  # deleting twice is fine
    assert not local.exists(clean)
    assert not any(path.name.startswith(".upload-") for path in tmp_path.rglob("*"))


def test_missing_objects_raise(local: LocalObjectStorage) -> None:
    quarantine, clean = keys()
    with pytest.raises(ObjectNotFound):
        local.get(quarantine)
    with pytest.raises(ObjectNotFound):
        local.move(quarantine, clean)


@pytest.mark.parametrize(
    "key",
    [
        "../outside",
        "tenants/../../etc/passwd",
        f"tenants/{uuid4()}/quarantine/../../{uuid4()}",
        f"tenants/{uuid4()}/public/{uuid4()}",
        f"tenants/{uuid4()}/quarantine/invoice.pdf",
    ],
)
def test_only_server_generated_keys_are_accepted(local: LocalObjectStorage, key: str) -> None:
    with pytest.raises(ValueError, match="storage key"):
        local.put(key, b"x")
