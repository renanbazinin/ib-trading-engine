from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveStateStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)

    def read(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as file_handle:
                payload = json.load(file_handle)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, payload: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        serializable = dict(payload)
        serializable["saved_at"] = utc_now_iso()

        fd, tmp_path = tempfile.mkstemp(
            prefix=".live_state_",
            suffix=".tmp",
            dir=os.path.dirname(self.path),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
                json.dump(serializable, file_handle, indent=2, sort_keys=True, default=str)
                file_handle.write("\n")
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
