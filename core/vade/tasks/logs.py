"""Bounded in-memory log ring for tasks (RFC-0007 §6.2, FR-8.60).

Two limits, both contractual: `tasks.log_lines` (200) rows and
`tasks.log_bytes` (65 536) bytes per response, whichever is reached first; and
`tasks.log_ring_lines` (2000) rows retained in memory. Nothing is written to
disk in v1 — a restart loses the tail and the response says `lost=true` rather
than pretending the log is complete (Q3).

Cursors are opaque base64 of a monotonically increasing line index. They
survive the ring evicting a line (the reader then gets `lost=true` instead of
a silent jump), which is the whole point of not returning a bare offset.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import deque
from typing import Any, Iterable

from .models import now_ms

__all__ = ["RingLog", "encode_cursor", "decode_cursor"]


def encode_cursor(index: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{int(index)}".encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> int:
    """Opaque to clients, decoded only here. Garbage is a caller error."""
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("empty cursor")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:  # pragma: no cover - shape guard
        raise ValueError("undecodable cursor") from exc
    if not raw.startswith("v1:"):
        raise ValueError("unknown cursor version")
    try:
        index = int(raw[3:])
    except ValueError as exc:
        raise ValueError("non-numeric cursor") from exc
    if index < 0:
        raise ValueError("negative cursor")
    return index


def _payload_size(rows: Iterable[tuple[int, int, str, str, bool]]) -> int:
    rows_json = [
        {"ts": row[1], "stream": row[2], "text": row[3]} for row in rows
    ]
    return len(json.dumps(rows_json, ensure_ascii=False).encode("utf-8"))


class RingLog:
    """`deque(maxlen=...)` of `(index, ts, stream, text, line_truncated)`."""

    def __init__(
        self,
        *,
        max_lines: int = 2000,
        line_chars: int = 4096,
        clock=now_ms,
    ) -> None:
        self.max_lines = max(1, int(max_lines))
        self.line_chars = max(1, int(line_chars))
        self._clock = clock
        self._lines: deque[tuple[int, int, str, str, bool]] = deque(maxlen=self.max_lines)
        self._next = 0

    # ------------------------------------------------------------------ writes
    def append(self, text: str, *, stream: str = "core") -> int:
        raw = str(text if text is not None else "")
        clipped = len(raw) > self.line_chars
        if clipped:
            # §6.2 `truncated:"line"`: one long line is cut, the reader is told.
            raw = raw[: self.line_chars]
        index = self._next
        self._next += 1
        self._lines.append((index, int(self._clock()), str(stream), raw, clipped))
        return index

    def extend(self, rows: Iterable[str], *, stream: str = "core") -> None:
        for row in rows:
            self.append(row, stream=stream)

    # ------------------------------------------------------------------- reads
    @property
    def next_index(self) -> int:
        """Index a follow reader polls for: `next_index > seen` means new lines."""
        return self._next

    @property
    def first_index(self) -> int:
        return self._lines[0][0] if self._lines else self._next

    def __len__(self) -> int:
        return len(self._lines)

    def tail(
        self,
        *,
        lines: int,
        max_bytes: int,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        """Return the tail after `cursor` (or the newest `lines` when None).

        Precedence matches §6.2: `lines` and bytes both cap the result and the
        first limit reached wins. `truncated="head"` means older rows exist but
        did not fit; `truncated="line"` means the newest line itself was cut at
        `tasks.log_line_chars`. Head wins when both happen, because "you are
        missing earlier output" is the more important thing to tell a reader.
        """
        limit = max(1, int(lines))
        budget = max(1, int(max_bytes))
        lost = cursor is not None and cursor < self.first_index

        if cursor is None:
            # Keep the whole window so `head_dropped` can tell "older rows did not
            # fit the limit" apart from "this is the beginning of the log". The
            # `limit` is applied below via `available[-limit:]`.
            start = self.first_index
        else:
            start = max(cursor, self.first_index)

        available = [row for row in self._lines if row[0] >= start]
        selected: list[tuple[int, int, str, str, bool]] = list(available[-limit:])
        # The byte cap is measured on the JSON the caller will actually send,
        # not on an estimate: a 200-row window is cheap to re-serialize and an
        # estimate that is 8 bytes/row off blows the 64 KiB contract silently.
        while len(selected) > 1 and _payload_size(selected) > budget:
            selected.pop(0)

        # "head" means rows that *were* available from `start` did not fit the
        # line/byte budget — not merely that older rows exist outside the ring
        # (that case is `lost`, which is a different lie to tell the reader).
        head_dropped = bool(selected) and bool(available) and selected[0][0] > available[0][0]
        line_clipped = bool(selected) and selected[-1][4]
        truncated = "head" if head_dropped else ("line" if line_clipped else None)
        next_cursor = (selected[-1][0] + 1) if selected else start
        return {
            "rows": [
                {"ts": row[1], "stream": row[2], "text": row[3]} for row in selected
            ],
            "cursor": encode_cursor(next_cursor),
            "cursor_index": next_cursor,
            "truncated": truncated,
            "lost": bool(lost),
        }
