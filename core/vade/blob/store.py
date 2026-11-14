"""SQLite index for uploads, blobs, device tokens and the GC lock (RFC-0004 §4.7).

One file (`<data_dir>/blob.sqlite3`) next to the bytes it describes. All
mutations run inside `BEGIN IMMEDIATE`: parts can be written by several
requests at once, and "read ranges, append, write ranges" must not interleave.

The row/byte ordering rule lives in `service.complete`, not here: write the
file first, then the row. A crash between the two leaves an orphan file (the
sweeper collects it) instead of a row pointing at nothing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..errors import VadeError

__all__ = ["BlobStore", "UploadRecord", "BlobRecord", "TokenRecord"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blob_uploads (
    upload_id  TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mime       TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    width      INTEGER,
    height     INTEGER,
    part_size  INTEGER NOT NULL,
    ranges     TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state      TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS blob_uploads_content
    ON blob_uploads (device_id, sha256, size, state);
CREATE TABLE IF NOT EXISTS blob_parts (
    upload_id TEXT NOT NULL,
    offset    INTEGER NOT NULL,
    length    INTEGER NOT NULL,
    sha256    TEXT NOT NULL,
    PRIMARY KEY (upload_id, offset)
);
CREATE TABLE IF NOT EXISTS blobs (
    file_id    TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mime       TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    width      INTEGER,
    height     INTEGER,
    pinned     INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    created_at REAL NOT NULL,
    scan       TEXT NOT NULL DEFAULT 'clean',
    state      TEXT NOT NULL DEFAULT 'ok'
);
CREATE UNIQUE INDEX IF NOT EXISTS blobs_content
    ON blobs (device_id, sha256, size);
CREATE TABLE IF NOT EXISTS blob_bytes (
    sha256    TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blob_tokens (
    token_hash TEXT PRIMARY KEY,
    device_id  TEXT NOT NULL,
    expires_at REAL NOT NULL,
    read_only  INTEGER NOT NULL DEFAULT 0,
    max_bytes  INTEGER
);
CREATE TABLE IF NOT EXISTS blob_gc (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    locked_until REAL NOT NULL DEFAULT 0
);
"""


@dataclass(slots=True)
class UploadRecord:
    upload_id: str
    device_id: str
    sha256: str
    size: int
    mime: str
    name: str
    kind: str
    width: int | None
    height: int | None
    part_size: int
    ranges: list[list[int]] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    state: str = "open"

    @property
    def expires_at(self) -> float:
        return self.updated_at

    def public(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "size": self.size,
            "part_size": self.part_size,
            "mime": self.mime,
            "kind": self.kind,
            "received": [list(map(int, pair)) for pair in self.ranges],
            "state": self.state,
        }


@dataclass(slots=True)
class BlobRecord:
    file_id: str
    device_id: str
    sha256: str
    size: int
    mime: str
    name: str
    kind: str
    width: int | None
    height: int | None
    pinned: bool
    expires_at: float | None
    created_at: float
    scan: str = "clean"
    state: str = "ok"


@dataclass(frozen=True, slots=True)
class TokenRecord:
    device_id: str
    expires_at: float
    read_only: bool
    max_bytes: int | None


def _upload(row: sqlite3.Row) -> UploadRecord:
    return UploadRecord(
        upload_id=row["upload_id"],
        device_id=row["device_id"],
        sha256=row["sha256"],
        size=int(row["size"]),
        mime=row["mime"],
        name=row["name"],
        kind=row["kind"],
        width=row["width"],
        height=row["height"],
        part_size=int(row["part_size"]),
        ranges=json.loads(row["ranges"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        state=row["state"],
    )


def _blob(row: sqlite3.Row) -> BlobRecord:
    return BlobRecord(
        file_id=row["file_id"],
        device_id=row["device_id"],
        sha256=row["sha256"],
        size=int(row["size"]),
        mime=row["mime"],
        name=row["name"],
        kind=row["kind"],
        width=row["width"],
        height=row["height"],
        pinned=bool(row["pinned"]),
        expires_at=row["expires_at"],
        created_at=float(row["created_at"]),
        scan=row["scan"],
        state=row["state"],
    )


class BlobStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("INSERT OR IGNORE INTO blob_gc (id, locked_until) VALUES (1, 0)")
            self._rebuild_refcounts()

    def _rebuild_refcounts(self) -> None:
        """Migration + self-heal: `blob_bytes` is derived from `blobs`.

        Byte ownership is per digest, row ownership is per (device, digest).
        Recomputing at open means an upgrade from the pre-refcount schema (or a
        hand-edited database) starts from truth instead of from a guess.
        """
        with self._tx() as db:
            db.execute("DELETE FROM blob_bytes")
            db.execute(
                "INSERT INTO blob_bytes (sha256, size, ref_count)"
                " SELECT sha256, MAX(size), COUNT(*) FROM blobs GROUP BY sha256"
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    # ------------------------------------------------------------------ uploads
    def add_upload(self, record: UploadRecord) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO blob_uploads (upload_id, device_id, sha256, size, mime, name, kind, width, height,"
                " part_size, ranges, created_at, updated_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.upload_id, record.device_id, record.sha256, record.size, record.mime, record.name,
                    record.kind, record.width, record.height, record.part_size, json.dumps(record.ranges),
                    record.created_at, record.updated_at, record.state,
                ),
            )

    def get_upload(self, upload_id: str) -> UploadRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM blob_uploads WHERE upload_id=?", (upload_id,)).fetchone()
        return _upload(row) if row else None

    def find_open_upload(self, device_id: str, sha256: str, size: int) -> UploadRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blob_uploads WHERE device_id=? AND sha256=? AND size=? AND state='open'"
                " ORDER BY created_at DESC LIMIT 1",
                (device_id, sha256, size),
            ).fetchone()
        return _upload(row) if row else None

    def part_digest(self, upload_id: str, offset: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT sha256 FROM blob_parts WHERE upload_id=? AND offset=?", (upload_id, offset)
            ).fetchone()
        return str(row["sha256"]) if row else None

    def commit_part(self, upload_id: str, *, offset: int, length: int, digest: str,
                    ranges: Sequence[Sequence[int]], now: float) -> None:
        """Record a part and its ranges atomically (digest + interval table)."""
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO blob_parts (upload_id, offset, length, sha256) VALUES (?,?,?,?)",
                (upload_id, offset, length, digest),
            )
            db.execute(
                "UPDATE blob_uploads SET ranges=?, updated_at=? WHERE upload_id=?",
                (json.dumps([list(map(int, pair)) for pair in ranges]), now, upload_id),
            )

    def touch_upload(self, upload_id: str, now: float) -> None:
        with self._lock:
            self._db.execute("UPDATE blob_uploads SET updated_at=? WHERE upload_id=?", (now, upload_id))

    def delete_upload(self, upload_id: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM blob_parts WHERE upload_id=?", (upload_id,))
            db.execute("DELETE FROM blob_uploads WHERE upload_id=?", (upload_id,))

    def open_uploads(self) -> list[UploadRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM blob_uploads WHERE state='open'").fetchall()
        return [_upload(row) for row in rows]

    # -------------------------------------------------------------------- blobs
    def add_blob(self, record: BlobRecord) -> BlobRecord:
        """Insert, or return the existing row for (device, sha256, size).

        A new row takes one reference on the shared bytes. The dedupe branch
        (unique index hit) takes none: it returns a row that already holds one,
        and double-counting there would leak the file forever.
        """
        with self._tx() as db:
            try:
                db.execute(
                    "INSERT INTO blobs (file_id, device_id, sha256, size, mime, name, kind, width, height,"
                    " pinned, expires_at, created_at, scan, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.file_id, record.device_id, record.sha256, record.size, record.mime, record.name,
                        record.kind, record.width, record.height, int(record.pinned), record.expires_at,
                        record.created_at, record.scan, record.state,
                    ),
                )
                db.execute(
                    "INSERT INTO blob_bytes (sha256, size, ref_count) VALUES (?,?,1)"
                    " ON CONFLICT(sha256) DO UPDATE SET ref_count = ref_count + 1",
                    (record.sha256, record.size),
                )
                return record
            except sqlite3.IntegrityError:
                row = db.execute(
                    "SELECT * FROM blobs WHERE device_id=? AND sha256=? AND size=?",
                    (record.device_id, record.sha256, record.size),
                ).fetchone()
        if row is None:  # pragma: no cover - unique index says otherwise
            raise VadeError("INTERNAL", "blob row disappeared during insert")
        return _blob(row)

    def find_blob_by_content(self, device_id: str, sha256: str, size: int) -> BlobRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM blobs WHERE device_id=? AND sha256=? AND size=?", (device_id, sha256, size)
            ).fetchone()
        return _blob(row) if row else None

    def get_blob(self, file_id: str) -> BlobRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM blobs WHERE file_id=?", (file_id,)).fetchone()
        return _blob(row) if row else None

    def release_blob(self, file_id: str) -> bool:
        """Delete the row and drop one reference on its bytes.

        Returns True only when that was the last reference — the caller may
        delete the file then, and must not before: two devices may own the same
        digest, and one of them expiring says nothing about the other.
        """
        with self._tx() as db:
            row = db.execute("SELECT sha256 FROM blobs WHERE file_id=?", (file_id,)).fetchone()
            if row is None:
                return False
            sha = str(row["sha256"])
            db.execute("DELETE FROM blobs WHERE file_id=?", (file_id,))
            db.execute("UPDATE blob_bytes SET ref_count = ref_count - 1 WHERE sha256=?", (sha,))
            left = db.execute("SELECT ref_count FROM blob_bytes WHERE sha256=?", (sha,)).fetchone()
            if left is None or int(left["ref_count"]) <= 0:
                db.execute("DELETE FROM blob_bytes WHERE sha256=?", (sha,))
                return True
            return False

    def delete_blob(self, file_id: str) -> bool:
        """Back-compat name for `release_blob` (refcounted, never blind)."""
        return self.release_blob(file_id)

    def ref_count(self, sha256: str) -> int:
        """Live row references for one digest (0 when no row points at it)."""
        with self._lock:
            row = self._db.execute(
                "SELECT ref_count FROM blob_bytes WHERE sha256=?", (sha256,)
            ).fetchone()
        return int(row["ref_count"]) if row else 0

    def set_pinned(self, file_id: str, pinned: bool, *, expires_at: float | None = None) -> None:
        """Pin clears the deadline; unpin starts a fresh TTL from `expires_at`."""
        with self._lock:
            self._db.execute(
                "UPDATE blobs SET pinned=?, expires_at=? WHERE file_id=?",
                (int(pinned), None if pinned else expires_at, file_id),
            )

    def set_scan(self, file_id: str, status: str) -> None:
        with self._lock:
            self._db.execute("UPDATE blobs SET scan=? WHERE file_id=?", (status, file_id))

    def set_state(self, file_id: str, state: str) -> None:
        with self._lock:
            self._db.execute("UPDATE blobs SET state=? WHERE file_id=?", (state, file_id))

    def all_blobs(self) -> list[BlobRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM blobs").fetchall()
        return [_blob(row) for row in rows]

    def pending_scans(self) -> list[BlobRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM blobs WHERE scan='pending'").fetchall()
        return [_blob(row) for row in rows]

    def expired_blobs(self, now: float) -> list[BlobRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM blobs WHERE pinned=0 AND expires_at IS NOT NULL AND expires_at <= ?", (now,)
            ).fetchall()
        return [_blob(row) for row in rows]

    # ------------------------------------------------------------------- quotas
    def usage(self, device_id: str, now: float, *, idle_s: float | None = None) -> int:
        """Committed bytes that still count + bytes reserved by live uploads (§4.6).

        A row past its deadline is *already gone* as far as the device is
        concerned — the sweeper just has not reached it yet. Counting it would
        turn "quota full" into a state only a manual `sweep()` can leave, so the
        query applies the same predicate the sweeper uses. Pinned rows have no
        deadline and always count.
        """
        upload_sql = (
            "SELECT COALESCE(SUM(size), 0) AS total FROM blob_uploads"
            " WHERE device_id=? AND state='open'"
        )
        upload_args: tuple[Any, ...] = (device_id,)
        if idle_s is not None:
            upload_sql += " AND updated_at > ?"
            upload_args += (now - float(idle_s),)
        with self._lock:
            blob_bytes = self._db.execute(
                "SELECT COALESCE(SUM(size), 0) AS total FROM blobs WHERE device_id=?"
                " AND (pinned=1 OR expires_at IS NULL OR expires_at > ?)",
                (device_id, now),
            ).fetchone()["total"]
            upload_bytes = self._db.execute(upload_sql, upload_args).fetchone()["total"]
        return int(blob_bytes) + int(upload_bytes)

    def tmp_usage(self, device_id: str, now: float | None = None, *, idle_s: float | None = None) -> int:
        """Bytes reserved in `blobs/tmp/`; idle-expired sessions do not count."""
        sql = "SELECT COALESCE(SUM(size), 0) AS total FROM blob_uploads WHERE device_id=? AND state='open'"
        args: tuple[Any, ...] = (device_id,)
        if idle_s is not None and now is not None:
            sql += " AND updated_at > ?"
            args += (float(now) - float(idle_s),)
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return int(row["total"])

    def upload_ids(self) -> set[str]:
        """Every upload id with a row (used to spot temp files nobody owns)."""
        with self._lock:
            rows = self._db.execute("SELECT upload_id FROM blob_uploads").fetchall()
        return {str(row["upload_id"]) for row in rows}


    # ------------------------------------------------------------------- tokens
    def add_token(self, token_hash: str, record: TokenRecord) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO blob_tokens (token_hash, device_id, expires_at, read_only, max_bytes)"
                " VALUES (?,?,?,?,?)",
                (token_hash, record.device_id, record.expires_at, int(record.read_only), record.max_bytes),
            )

    def get_token(self, token_hash: str, now: float) -> TokenRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM blob_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= now:
                self._db.execute("DELETE FROM blob_tokens WHERE token_hash=?", (token_hash,))
                return None
        return TokenRecord(
            device_id=str(row["device_id"]),
            expires_at=float(row["expires_at"]),
            read_only=bool(row["read_only"]),
            max_bytes=row["max_bytes"],
        )

    def prune_tokens(self, now: float) -> int:
        with self._lock:
            cursor = self._db.execute("DELETE FROM blob_tokens WHERE expires_at <= ?", (now,))
        return int(cursor.rowcount or 0)

    def revoke_device_tokens(self, device_id: str) -> int:
        with self._lock:
            cursor = self._db.execute("DELETE FROM blob_tokens WHERE device_id=?", (device_id,))
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------- gc lock
    def acquire_gc(self, now: float, lease_s: float) -> bool:
        """Single-writer sweeper: a lease row, not distributed coordination."""
        with self._tx() as db:
            row = db.execute("SELECT locked_until FROM blob_gc WHERE id=1").fetchone()
            if row is not None and float(row["locked_until"]) > now:
                return False
            db.execute("UPDATE blob_gc SET locked_until=? WHERE id=1", (now + lease_s,))
            return True

    def release_gc(self) -> None:
        with self._lock:
            self._db.execute("UPDATE blob_gc SET locked_until=0 WHERE id=1")


def now_seconds() -> float:
    return time.time()
