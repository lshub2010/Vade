"""Read side of the audit store: day-table union, cursor pagination, replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["AuditReader", "decode_cursor", "encode_cursor"]


def encode_cursor(ts: float, row_id: int) -> str:
    return f"{ts:.6f}:{row_id}"


def decode_cursor(cursor: str | None) -> tuple[float, int] | None:
    if not cursor:
        return None
    try:
        ts, row_id = cursor.split(":", 1)
        return float(ts), int(row_id)
    except (TypeError, ValueError):
        return None


class AuditReader:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _tables(self, db: sqlite3.Connection) -> list[str]:
        names = [
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'audit\\_%' ESCAPE '\\'"
            )
        ]
        return sorted((n for n in names if len(n) == 14 and n[6:].isdigit()), reverse=True)

    def query(
        self,
        *,
        from_ts: float | None = None,
        to_ts: float | None = None,
        module_id: str | None = None,
        kind: str | None = None,
        turn_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": [], "next_cursor": None}
        limit = max(1, min(int(limit), 500))
        after = decode_cursor(cursor)
        where: list[str] = []
        params: list[Any] = []
        if from_ts is not None:
            where.append("ts >= ?")
            params.append(from_ts)
        if to_ts is not None:
            where.append("ts <= ?")
            params.append(to_ts)
        if module_id:
            where.append("module_id = ?")
            params.append(module_id)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if turn_id:
            where.append("turn_id = ?")
            params.append(turn_id)
        if after is not None:
            where.append("(ts < ? OR (ts = ? AND id < ?))")
            params.extend([after[0], after[0], after[1]])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows: list[dict[str, Any]] = []
        db = sqlite3.connect(self.path)
        try:
            for table in self._tables(db):
                sql = (
                    f'SELECT id, ts, kind, session_id, turn_id, module_id, instance_id, method, tool,'
                    f' duration_ms, status, code, request_id, actor, args_digest, args_redacted, extra'
                    f' FROM "{table}"{clause} ORDER BY ts DESC, id DESC LIMIT ?'
                )
                try:
                    fetched = db.execute(sql, (*params, limit + 1)).fetchall()
                except sqlite3.OperationalError:
                    continue
                for row in fetched:
                    rows.append(self._row(row))
        finally:
            db.close()
        rows.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1]["ts"], page[-1]["id"]) if len(rows) > limit and page else None
        return {"items": page, "next_cursor": next_cursor}

    @staticmethod
    def _row(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = (
            "id", "ts", "kind", "session_id", "turn_id", "module_id", "instance_id", "method",
            "tool", "duration_ms", "status", "code", "request_id", "actor", "args_digest",
            "args_redacted", "extra",
        )
        item = dict(zip(keys, row))
        for field in ("args_redacted", "extra"):
            raw = item.get(field)
            if isinstance(raw, str):
                try:
                    item[field] = json.loads(raw)
                except json.JSONDecodeError:
                    item[field] = {"_raw": raw}
        return item
