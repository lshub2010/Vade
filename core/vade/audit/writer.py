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
  the row. A batch that fails to land is retried once and then parked in
  ``audit.deadletter.jsonl`` (``audit_id`` per line), so that id still resolves.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..logging import get_logger
from .redact import digest_args, redact_args

__all__ = [
    "AuditWriter", "COLUMNS", "day_key", "new_audit_id", "read_dead_letter",
    "DEAD_LETTER_SUFFIX",
]

log = get_logger("vade.audit")

#: Rows that survive neither the write nor the retry land here:
#: `<audit.db>` -> `audit.deadletter.jsonl`, one JSON object per row.
DEAD_LETTER_SUFFIX = ".deadletter.jsonl"
#: How many lost `audit_id`s an error line carries before it summarises.
_DEAD_LETTER_LOG_IDS = 20

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
        #: Batches that ended up **not written** after the retry (i.e. parked in
        #: the dead-letter file). Never silent: the flusher survives the error,
        #: logs it, and resets the handle so the next batch can succeed.
        self.write_failures = 0
        #: Batches that failed once and landed on the retry — a healthy core
        #: under a racing `prune()` looks like this, not like `write_failures`.
        self.write_retries = 0
        #: Rows appended to `deadletter_path` (== sum of `write_failures`' rows).
        self.dead_letters = 0

    @property
    def deadletter_path(self) -> Path:
        """Where rows go when neither the write nor the retry worked.

        Sibling of the database (`audit.db` -> `audit.deadletter.jsonl`) so the
        file lands in the same volume and survives the process: an `audit_id`
        the UI already showed has to point *somewhere* (review P1-4).
        """
        return self.path.with_suffix(DEAD_LETTER_SUFFIX)

    # ------------------------------------------------------------------ public
    def record(self, event: Mapping[str, Any]) -> str:
        row = self._normalise(event)
        audit_id = row["extra"]["audit_id"]
        if self._sync:
            self._write_many([row])
            return audit_id
        if self._degraded:
            # Degraded means "the queue could not take it, so this write went
            # synchronous". It must end when the backlog does: the old flag was
            # never cleared, so one `queue.Full` pinned every later write onto
            # the caller's thread — the event loop — until process restart
            # (review P1-2).
            if self._queue.qsize() <= self._max_queue // 2:
                self._degraded = False
                log.info(
                    "audit queue drained (<=%s rows); leaving degraded mode",
                    self._max_queue // 2,
                    extra={"code": None, "msg": "audit_recovered"},
                )
            else:
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
        self._commit()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._drain()
        with self._lock:
            self._commit_locked()
            db = self._db
            self._db = None
            self._tables.clear()
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
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
            try:
                batch = self._collect()
                if not batch:
                    self._stop.wait(self._batch_ms / 1000.0)
                    continue
                self._write_batch(batch)
            except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the flusher
                # Write failures are handled inside `_write_batch` (retry, then
                # dead letter); this is the belt-and-braces for anything else,
                # so a single exception can never kill the thread the way the
                # un-guarded loop did (review P0-4).
                self.write_failures += 1
                log.error(
                    "audit flusher error (total=%s): %s",
                    self.write_failures, exc,
                    extra={"code": None, "msg": "audit_write_failed"},
                )
                self._reset_db()
                self._stop.wait(self._batch_ms / 1000.0)
                continue
            time.sleep(self._batch_ms / 1000.0)

    def _write_batch(self, rows: list[dict[str, Any]], *, source: str = "batch") -> None:
        """Write one batch: one retry on a fresh handle, then the dead-letter file.

        The old body logged "batch dropped" and moved on, so one `SQLITE_BUSY`
        or a disk hiccup silently destroyed up to 500 rows **after** the caller
        had already been handed their `audit_id`s — an id pointing at nothing is
        worse than no id at all (review P1-4). The retry is what a transient
        error actually needs (`_reset_db` drops the poisoned handle first); a
        permanent one ends in `_dead_letter`, which keeps the rows enumerable.
        """
        try:
            self._write_many(rows)
            return
        except Exception as exc:  # noqa: BLE001 - retried immediately below
            log.warning(
                "audit flush failed, retrying once (%s rows): %s", len(rows), exc,
                extra={"code": None, "msg": "audit_write_retry"},
            )
            self._reset_db()
        try:
            self._write_many(rows)
        except Exception as exc:  # noqa: BLE001 - nothing left to try
            # Park first, count second: a monitor that sees `write_failures`
            # must not catch the counter before the rows exist in the file.
            self._dead_letter(rows, exc, source=source)
            self.write_failures += 1
            self._reset_db()
            return
        self.write_retries += 1
        log.info(
            "audit batch recovered on retry (%s rows, retries=%s)",
            len(rows), self.write_retries,
            extra={"code": None, "msg": "audit_retry_ok"},
        )

    def _dead_letter(
        self, rows: list[dict[str, Any]], exc: BaseException, *, source: str
    ) -> int:
        """Park un-writable rows in `deadletter_path`; return how many landed.

        One JSON object per row, carrying the row itself, its `audit_id` and the
        sqlite error: the file is the audit trail for the audit trail, and
        `read_dead_letter()` / `/admin/api/health` make the loss enumerable
        instead of a single "batch dropped" line.
        """
        ids = [str((row.get("extra") or {}).get("audit_id") or "") for row in rows]
        batch_id = new_audit_id()
        now = self._clock()
        path = self.deadletter_path
        written = 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                for row, audit_id in zip(rows, ids):
                    handle.write(json.dumps({
                        "at": now, "batch_id": batch_id, "source": source,
                        "audit_id": audit_id, "error": f"{type(exc).__name__}: {exc}",
                        "row": row,
                    }, ensure_ascii=False, default=str) + "\n")
                    written += 1
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as write_exc:
            # The last resort failed too: say so as loudly as possible. The ids
            # still go to the log, which is all that is left.
            log.error(
                "audit dead-letter unwritable, %s rows lost (%s): %s",
                len(rows), path, write_exc,
                extra={"code": None, "msg": "audit_dead_letter_failed"},
            )
        self.dead_letters += written
        shown = ids[:_DEAD_LETTER_LOG_IDS]
        log.error(
            "audit batch of %s rows dead-lettered to %s (batch=%s source=%s ids=%s%s): %s",
            len(rows), path, batch_id, source, shown,
            "" if len(ids) <= _DEAD_LETTER_LOG_IDS else f" (+{len(ids) - len(shown)} more)",
            exc,
            extra={"code": None, "msg": "audit_dead_letter"},
        )
        return written

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
                return
            # Same policy as the flusher: one retry, then the row is parked
            # where an operator can find it. `flush()`/`close()` are the paths
            # a shutdown takes, so this is the *last* chance to keep the row.
            self._write_batch([row], source="drain")

    def _commit(self) -> None:
        with self._lock:
            self._commit_locked()

    def _commit_locked(self) -> None:
        if self._db is None:
            return
        try:
            self._db.commit()
        except sqlite3.Error as exc:
            self.write_failures += 1
            log.error(
                "audit commit failed (total=%s): %s", self.write_failures, exc,
                extra={"code": None, "msg": "audit_write_failed"},
            )
            self._reset_db()

    def _reset_db(self) -> None:
        """Drop the handle after an error so the next batch reconnects.

        A `sqlite3` connection that failed mid-transaction stays unusable for
        some errors ("database is locked" from a racing prune, a full disk);
        keeping it would turn one transient failure into a permanent outage.
        """
        with self._lock:
            if self._db is None:
                return
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None
            self._tables.clear()

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

    def dead_letter_rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        """This writer's dead-letter file, oldest first — the recovery input."""
        return read_dead_letter(self.deadletter_path, limit)


def read_dead_letter(path: Path | str, limit: int | None = None) -> list[dict[str, Any]]:
    """Rows parked in a dead-letter file, oldest first (bad lines skipped).

    Shared by `AuditWriter.dead_letter_rows()`, the health endpoint and an
    operator: `len()` is how many rows are waiting, `entry["audit_id"]` is the id
    the UI already showed, and `entry["row"]` is the original row, so a recovery
    pass can feed it back through `record()`.
    """
    file = Path(path)
    if not file.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(file, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line (power loss mid-append) must not make the
                # whole file unreadable.
                continue
            if isinstance(item, dict):
                out.append(item)
    return out[-limit:] if limit else out
