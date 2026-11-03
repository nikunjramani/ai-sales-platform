"""The simulated phone's outbox: one JSON file per message under the storage folder.

The demo page and scripts/simulated_channel.py read these files, so names and keys are fixed:
- review: <tenant>/<external user>/<task_id>.json with tenant_id, connection_id, to, task_id,
  quotation, version, text, actions, action_reference, expires_at;
- notice: <tenant>/<external user>/notice-<notice_id>.json with tenant_id, connection_id, to,
  notice_id, text. A repeated notice replaces the file instead of adding one.
The files contain live action references, so treat the folder like a mailbox: development only.
"""

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.@-]")


def write_message(
    outbox: Path, tenant_id: UUID, recipient: str, name: str, message: Mapping[str, object]
) -> Path:
    folder = outbox / str(tenant_id) / _SAFE_NAME.sub("_", recipient)
    folder.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".message-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(message, stream, indent=2)
        os.replace(temporary, folder / name)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return folder / name
