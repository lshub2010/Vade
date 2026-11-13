"""JSON-lines logging (01-contracts §六 log fields).

One object per line, to **stderr**: `--standalone` writes its JSON result to
stdout, so anything else on stdout would corrupt the answer. Fields:

    ts / level / module_id / instance_id / tool / duration_ms / code / msg

`tool`, `duration_ms` and `code` are omitted when not applicable rather than
sent as null — a line that always carries the same keys is easier to grep but
hides which field the event was actually about.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, IO

__all__ = ["JsonLogger", "get_logger"]


class JsonLogger:
    def __init__(
        self,
        module_id: str,
        instance_id: str = "main",
        *,
        stream: IO[str] | None = None,
        min_level: str = "info",
    ) -> None:
        self.module_id = module_id
        self.instance_id = instance_id
        self._stream = stream if stream is not None else sys.stderr
        self._levels = {"debug": 10, "info": 20, "warning": 30, "error": 40}
        self._min = self._levels.get(min_level, 20)

    def emit(self, level: str, message: str, **fields: Any) -> None:
        if self._levels.get(level, 20) < self._min:
            return
        detail = fields.pop("msg", None)
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z",
            "level": level,
            "module_id": self.module_id,
            "instance_id": self.instance_id,
            "msg": message if detail is None else f"{message}: {detail}",
        }
        for key, value in fields.items():
            if value is None:
                continue
            record[key] = value
        try:
            self._stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._stream.flush()
        except (OSError, ValueError):  # a closed stderr must not kill the module
            pass

    def debug(self, message: str, **fields: Any) -> None:
        self.emit("debug", message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self.emit("info", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self.emit("warning", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.emit("error", message, **fields)


def get_logger(module_id: str, instance_id: str = "main") -> JsonLogger:
    return JsonLogger(module_id, instance_id)
