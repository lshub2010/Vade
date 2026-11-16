"""Append-only audit store: per-day tables, batched writes, RAISE(ABORT) triggers.

Invariants live here, not in the caller:

* Every write is checked against ``kind``/column normalisation, so a caller
  cannot smuggle a password into ``extra`` by accident (``redact_args`` runs on
  the way in).
* ``BEFORE UPDATE`` / ``BEFORE DELETE`` triggers abort. There is no update or
  delete method anywhere in this package — dropping a row has to go through
  ``sqlite3`` by hand, which is the point of an audit log.
* ``record`` returns an ``audit_id`` even when the write is queued; the admin
  API echoes it (``impact.audit_id``) so the UI can correlate the toast with
  the row.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..logging import get_logger
from .redact import digest_args, redact_args

__all__ = ["AuditWriter", "COLUMNS", "day_key", "new_audit_id"]

log = get_logger("vade.audit")

#: Column order; ``extra`` is the JSON escape hatch for kind-specific fields.
COLUMNS = (
    "ts", "kind", "session_id", "turn_id", "module_id", "instance_id", "method",
    "tool", "duration_ms", "status", "code", "request_id", "actor",
    "args_digest", "args_redacted", "extra",
)

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_LOCK = threading.Lock()
_ID_SEQ = 0


def day_key(ts: float | None = None) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts if ts is not None else time.time()))


def new_audit_id(ts: float | None = None) -> str:
    """ULID-shaped 26 chars: 48-bit ms + 80 random bits (sortable, no deps)."""
    global _ID_SEQ
    millis = int((ts if ts is not None else time.time()) * 1000)
    with _ID_LOCK:
        _ID_SEQ = (_ID_SEQ + 1) & 0xFFFF
        seq = _ID_SEQ
    entropy = ((int.from_bytes(bytes(__import__("os").urandom(8)), "big") << 16) | seq) & ((1 << 80) - 1)
    value = (millis << 80) | entropy
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


class AuditWriter:
    """Queue + background flusher. ``queue > max_queue`` degrades to sync writes."""

    def __init__(
        self,
        path: Path | str,
        *,
        batch_ms: int = 200,
        max_queue: int = 10_000,
        keep_days: int = 30,
        clock: Callable[[], float] = time.time,
        sync: bool = False,
    ) -> None:
        self.path = Path(path)
        self._batch_ms = batch_ms
        self._max_queue = max_queue
        self.keep_days = keep_days
        self._clock = clock
        self._sync = sync
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._tables: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._degraded = False

    # ------------------------------------------------------------------ public
    def record(self, event: Mapping[str, Any]) -> str:
        row = self._normalise(event)
        audit_id = row["extra"]["audit_id"]
        if self._sync or self._degraded:
            self._write_many([row])
            return audit_id
        try:
            self._ensure_worker()
            self._queue.put_nowait(row)
        except queue.Full:
            # Never drop an audit row: fall back to a synchronous commit and
            # stay degraded until the queue drains (23 §T3).
            self._degraded = True
            log.warning(
                "audit queue full (>%s); writing synchronously", self._max_queue,
                extra={"code": None, "msg": "audit_degraded"},
            )
            self._write_many([row])
        return audit_id

    def flush(self) -> None:
        self._drain()
        with self._lock:
            if self._db is not None:
                self._db.commit()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._drain()
        with self._lock:
            if self._db is not None:
                self._db.commit()
                self._db.close()
                self._db = None
                self._tables.clear()
        self._thread = None

    @property
    def degraded(self) -> bool:
        return self._degraded

    def prune(self, now: float | None = None, keep_days: int | None = None) -> list[str]:
        """Drop whole day tables older than the window; the drop is itself audited."""
        keep = self.keep_days if keep_days is None else keep_days
        now = now if now is not None else self._clock()
        cutoff = day_key(now - keep * 86_400)
        dropped: list[str] = []
        with self._lock:
            db = self._connect()
            names = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'audit_%'"
            )]
            for name in names:
                suffix = name.split("_", 1)[1]
                if len(suffix) == 8 and suffix.isdigit() and suffix < cutoff:
                    db.execute(f'DROP TABLE "{name}"')
                    db.execute(f'DROP TRIGGER IF EXISTS "{name}_no_update"')
                    db.execute(f'DROP TRIGGER IF EXISTS "{name}_no_delete"')
                    dropped.append(name)
                    self._tables.discard(name)
            db.commit()
        if dropped:
            self.record({
                "kind": "audit.pruned", "status": "ok",
                "extra": {"tables": dropped, "keep_days": keep},
            })
        return dropped

    # ----------------------------------------------------------------- internal
    def _normalise(self, event: Mapping[str, Any]) -> dict[str, Any]:
        ts = float(event.get("ts") or self._clock())
        # `extra` is the free-form escape hatch, so it is also the easiest
        # place to leak a credential (review P1-1: a setup/bearer token in
        # `extra` was persisted verbatim and readable by read-only sessions).
        # `redact_args` recurses, so nested dicts and bare keys both match.
        raw_extra = event.get("extra")
        if isinstance(raw_extra, Mapping):
            extra = dict(redact_args(raw_extra))
        elif raw_extra is None:
            extra = {}
        else:
            extra = {"value": redact_args({"value": raw_extra})["value"]}
        row: dict[str, Any] = {
            "ts": ts,
            "kind": str(event.get("kind") or "unknown")[:64],
            "session_id": event.get("session_id"),
            "turn_id": event.get("turn_id"),
            "module_id": event.get("module_id"),
            "instance_id": event.get("instance_id"),
            "method": event.get("method"),
            "tool": event.get("tool"),
            "duration_ms": event.get("duration_ms"),
            "status": event.get("status"),
            "code": event.get("code"),
            "request_id": event.get("request_id"),
            "actor": event.get("actor"),
            "args_digest": event.get("args_digest"),
            "args_redacted": None,
            "extra": extra,
        }
        # Anything the caller did not put in a known column lands in `extra`.
        # Redact the batch as a whole so a bare top-level `token=...` keyword is
        # caught exactly like one nested inside `extra`.
        stray = {
            str(key): value for key, value in event.items()
            if key not in COLUMNS and key != "args" and value is not None
            and str(key) not in row["extra"]
        }
        for key, value in redact_args(stray).items():
            row["extra"].setdefault(key, value)
        args = event.get("args")
        if args is not None:
            row["args_redacted"] = redact_args(args) if isinstance(args, Mapping) else {"value": str(args)[:200]}
            row["args_digest"] = row["args_digest"] or digest_args(args if isinstance(args, Mapping) else {})
        elif event.get("args_redacted") is not None:
            row["args_redacted"] = redact_args(event["args_redacted"]) if isinstance(event["args_redacted"], Mapping) else None
        if row["args_digest"] is None and row["args_redacted"]:
            row["args_digest"] = digest_args(row["args_redacted"])
        row["extra"].setdefault("audit_id", new_audit_id(ts))
        return row

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(target=self._run, name="vade-audit-writer", daemon=True)
            self._thread = thread
            thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._collect()
            if batch:
                self._write_many(batch)
            else:
                self._stop.wait(self._batch_ms / 1000.0)
                continue
            time.sleep(self._batch_ms / 1000.0)

    def _collect(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + max(self._batch_ms / 1000.0, 0.05)
        while len(batch) < 500:
            timeout = max(0.0, deadline - time.monotonic())
            try:
                batch.append(self._queue.get(timeout=timeout if not batch else 0.0))
            except queue.Empty:
                break
        return batch

    def _drain(self) -> None:
        while True:
            try:
                row = self._queue.get_nowait()
            except queue.Empty:
                break
            self._write_many([row])

    def _connect(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        return self._db

    def _table_for(self, day: str) -> str:
        name = f"audit_{day}"
        if name in self._tables:
            return name
        db = self._connect()
        db.execute(
            f'CREATE TABLE IF NOT EXISTS "{name}" ('
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL NOT NULL, kind TEXT NOT NULL, session_id TEXT, turn_id TEXT,"
            " module_id TEXT, instance_id TEXT, method TEXT, tool TEXT,"
            " duration_ms REAL, status TEXT, code TEXT, request_id TEXT, actor TEXT,"
            " args_digest TEXT, args_redacted TEXT, extra TEXT)"
        )
        db.execute(f'CREATE INDEX IF NOT EXISTS "{name}_ts" ON "{name}"(ts)')
        db.execute(f'CREATE INDEX IF NOT EXISTS "{name}_turn" ON "{name}"(turn_id)')
        # Append-only, enforced by SQLite itself (23 §T3, AC "audit UPDATE").
        db.execute(
            f'CREATE TRIGGER IF NOT EXISTS "{name}_no_update" BEFORE UPDATE ON "{name}" '
            "BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END"
        )
        db.execute(
            f'CREATE TRIGGER IF NOT EXISTS "{name}_no_delete" BEFORE DELETE ON "{name}" '
            "BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END"
        )
        db.commit()
        self._tables.add(name)
        return name

    def _write_many(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self._lock:
            db = self._connect()
            for row in rows:
                table = self._table_for(day_key(row["ts"]))
                db.execute(
                    f'INSERT INTO "{table}" (ts, kind, session_id, turn_id, module_id, instance_id,'
                    " method, tool, duration_ms, status, code, request_id, actor,"
                    " args_digest, args_redacted, extra)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["ts"], row["kind"], row["session_id"], row["turn_id"],
                        row["module_id"], row["instance_id"], row["method"], row["tool"],
                        row["duration_ms"], row["status"], row["code"], row["request_id"],
                        row["actor"], row["args_digest"],
                        json.dumps(row["args_redacted"], ensure_ascii=False) if row["args_redacted"] is not None else None,
                        json.dumps(row["extra"], ensure_ascii=False, default=str),
                    ),
                )
            db.commit()
