"""Private object storage on local disk (development and tests): attachments and quotation PDFs.

Only keys of the exact form tenants/<uuid>/<quarantine|clean|documents>/<uuid> are accepted, so
no key can point outside the storage root. Writes go to a temporary file first and are then
renamed, so a crash never leaves a half-written file under its real name.
"""

import os
import re
import tempfile
from pathlib import Path

from sales_platform.platform.ports.storage import ObjectNotFound

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_KEY = re.compile(rf"tenants/{_UUID}/(quarantine|clean|documents)/{_UUID}")


class LocalObjectStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise ValueError("Invalid storage key")
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError("Invalid storage key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise ObjectNotFound(key) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def move(self, source: str, destination: str) -> None:
        source_path, destination_path = self._path(source), self._path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source_path, destination_path)
        except FileNotFoundError:
            raise ObjectNotFound(source) from None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
