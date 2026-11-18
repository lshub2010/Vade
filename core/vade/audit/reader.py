"""Read side of the audit store: day-table union, cursor pagination, replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["AuditReader", "day_table", "decode_cursor", "encode_cursor"]


def _json1_available() -> bool:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("SELECT json_extract('{\"a\": 1}', '$.a')")
        finally:
            connection.close()
        return True
    except sqlite3.Error:
        return False


#: JSON1 is compiled into every Python >= 3.9 build we ship on; the probe keeps
#: `config_usage` correct (just slower) on a SQLite that predates it.
_JSON_OK = _json1_available()


def day_table(day: str) -> str:
    """`2026-11-18` / `20261118` -> `audit_20261118`."""
    return f"audit_{str(day).replace('-', '')}"


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

    def _tables(self, db: sqlite3.Connection, day_keys: Sequence[str] | None = None) -> list[str]:
        names = [
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'audit\\_%' ESCAPE '\\'"
            )
        ]
        tables = [n for n in names if len(n) == 14 and n[6:].isdigit()]
        if day_keys is not None:
            # Narrow the fan-out to the days the caller actually needs: the
            # reader otherwise sends one SELECT per retained day table (30 by
            # default) for *every* page (review P0-2).
            allowed = {day_table(day) for day in day_keys}
            tables = [n for n in tables if n in allowed]
        return sorted(tables, reverse=True)

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
        day_keys: Sequence[str] | None = None,
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
            for table in self._tables(db, day_keys):
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

    def sum_extra_int(
        self,
        *,
        day: str,
        kind: str | None = None,
        field: str = "tokens",
        exclude: Mapping[str, Any] | None = None,
    ) -> int:
        """Sum one integer field of `extra` over a single day table.

        One aggregate SELECT, no pagination and no union across the retention
        window: the evolve budget only ever needs *today*, and walking all 30
        day tables for it is what turned `GET /admin/api/config/usage` into a
        27 s event-loop stall on 200k rows (review P0-2).
        """
        if not self.path.exists():
            return 0
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        for key, value in (exclude or {}).items():
            where.append("IFNULL(json_extract(extra, ?), '') <> ?")
            params.extend([f'$."{key}"', value])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        db = sqlite3.connect(self.path)
        try:
            # Resolve through `_tables` so this obeys the same day narrowing as
            # `query` (and so a caller can see, and test, the fan-out).
            tables = self._tables(db, [day])
            if not tables:
                return 0
            table = tables[0]
            if _JSON_OK:
                try:
                    row = db.execute(
                        f'SELECT COALESCE(SUM(CAST(json_extract(extra, ?) AS INTEGER)), 0)'
                        f' FROM "{table}"{clause}',
                        (f'$."{field}"', *params),
                    ).fetchone()
                except sqlite3.OperationalError:  # no table for that day yet
                    return 0
                return int(row[0] or 0) if row else 0
            return self._sum_extra_int_python(db, table, kind, field, exclude or {})
        finally:
            db.close()

    @staticmethod
    def _sum_extra_int_python(db: sqlite3.Connection, table: str, kind: str | None,
                              field: str, exclude: Mapping[str, Any]) -> int:
        """Fallback for a SQLite without JSON1: one scan, sum in Python."""
        where = " WHERE kind = ?" if kind else ""
        try:
            rows = db.execute(f'SELECT extra FROM "{table}"{where}',
                              ((kind,) if kind else ())).fetchall()
        except sqlite3.OperationalError:
            return 0
        total = 0
        for (raw,) in rows:
            try:
                extra = json.loads(raw) if raw else {}
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(extra, dict):
                continue
            if any(extra.get(key) == value for key, value in exclude.items()):
                continue
            try:
                total += int(extra.get(field) or 0)
            except (TypeError, ValueError):
                continue
        return total

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
