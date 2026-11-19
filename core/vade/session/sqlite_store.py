"""SQLite SessionStore (v1 default).

Concurrency: one connection guarded by an RLock, `isolation_level=None`, every
multi-statement write wrapped in `BEGIN IMMEDIATE`. `next_seq` uses
`UPDATE ... RETURNING` when the bundled SQLite supports it (>= 3.35); the
fallback keeps the same guarantee inside an IMMEDIATE transaction.

The turn table carries extra columns beyond the task skeleton (`client_msg_id`,
`user_text`, `stopped_reason`, `error_code`, `usage`) because idempotency and
replay need them; the skeleton's four columns are all present and unchanged.

RFC-0006 appends columns (never rewrites the skeleton): `messages.partial`,
`messages.superseded_by`, and the turn version-chain/partial pointers. Existing
databases get them through `ALTER TABLE ... ADD COLUMN` on open, and old rows
read as the documented defaults (`partial=0`, `version=1`, `root_turn_id=id`).

WAL checkpoint policy (why the write path never checkpoints)
-----------------------------------------------------------
The connection runs `PRAGMA wal_autocheckpoint=0`. SQLite's auto-checkpoint
happens *inside COMMIT*, on the thread that commits — for this store that is
the event loop thread — and copying a 4 MiB / 1000-frame WAL there is a real
stall, not a theoretical one (probe: one append in 1000 cost 260-480 ms, and
the same workload with `wal_autocheckpoint=0` never exceeded 61 ms).

`PRAGMA wal_autocheckpoint=0` alone would let the WAL grow without bound, so
the store replaces it with an explicit, off-thread policy:

* `schedule_checkpoint()` is the hot-path *probe*: one `os.stat()` of the
  `-wal` sidecar. When the file is over `checkpoint_bytes` (16 MiB default) it
  hands the work to `asyncio.to_thread(checkpoint_passive)` and returns. It
  never touches the database, so the append that crossed the threshold pays
  nothing.
* `checkpoint_passive()` runs `PRAGMA wal_checkpoint(PASSIVE)` on a **private
  connection**, opened and closed on the worker thread. That is what keeps
  `_lock` out of the checkpoint path: the shared connection is only ever used
  by the loop thread's short transactions, so a checkpoint can neither make
  the loop wait behind a page copy nor hold up a commit. PASSIVE never waits
  for readers or writers, so it cannot deadlock against an open transaction
  and cannot fail a commit with `SQLITE_BUSY`.
* `PRAGMA journal_size_limit=0` is set on the writer connection: after a
  completed PASSIVE checkpoint the next writer restarts the log, and the limit
  is what makes the file actually give its space back instead of staying at
  the largest burst it ever saw. "Bounded" then means bounded on disk, not
  just logically.
* `close()` runs one `wal_checkpoint(TRUNCATE)` — shutdown is the one place a
  blocking checkpoint is free, and it leaves no sidecar behind.

Known limit, inherited from SQLite: a long-lived read snapshot pins the WAL, so
a checkpoint cannot backfill while it is open (`busy=1`); the file then keeps
growing until that reader ends. The hot-path probe is rate-limited
(`checkpoint_min_interval_s`) so a stuck checkpoint is retried, not attempted
per append.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..errors import VadeError
from ..logging import get_logger
from .models import (
    TERMINAL_STATES, Event, Message, Session, SessionGroup, SubagentRun, SubagentState,
    Turn, TurnState, now_ms,
)
from .search import LIMIT_DEFAULT, SearchHit, escape_like, find_ascii_ci, make_snippet
from .store import UNSET, SessionStore

__all__ = ["SQLiteStore", "WAL_CHECKPOINT_BYTES"]

log = get_logger("vade.session.sqlite")

#: WAL size that triggers a background checkpoint. 16 MiB is ~4x SQLite's own
#: 1000-frame (~4 MiB at 4 KiB pages) auto-checkpoint: a burst pays at most one
#: page copy instead of one per 4 MiB, while the file stays bounded.
WAL_CHECKPOINT_BYTES = 16 * 1024 * 1024

#: Floor between two *opportunistic* checkpoint attempts (`schedule_checkpoint`).
#: A checkpoint that cannot backfill (a reader pins the WAL) must not be retried
#: once per append; explicit `checkpoint_if_needed()` calls ignore this.
WAL_CHECKPOINT_MIN_INTERVAL_S = 1.0

#: How long `close()` waits for an in-flight background checkpoint before it
#: gives up on truncating. A checkpoint copies the WAL into the database file
#: and syncs it — on a slow disk that is seconds, and shutdown must not hang on
#: an *optimisation*: the WAL is durable either way, and the next process to
#: open the file recovers it.
WAL_CLOSE_TRUNCATE_TIMEOUT_S = 2.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    title TEXT,
    seq_counter INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open',
    model_id TEXT
);
CREATE TABLE IF NOT EXISTS session_groups(
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT,
    color TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS turns(
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    client_msg_id TEXT,
    user_text TEXT NOT NULL DEFAULT '',
    stopped_reason TEXT,
    error_code TEXT,
    error_message TEXT,
    usage TEXT NOT NULL DEFAULT '{}',
    tool_calls TEXT NOT NULL DEFAULT '[]',
    model_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_client
    ON turns(session_id, client_msg_id) WHERE client_msg_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, started_at);
CREATE TABLE IF NOT EXISTS messages(
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    name TEXT,
    tool_call_id TEXT,
    created_at INTEGER NOT NULL,
    parts TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE TABLE IF NOT EXISTS events(
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY(session_id, seq)
);
CREATE TABLE IF NOT EXISTS subagent_runs(
    run_id TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    parent_turn_id TEXT,
    child_session_id TEXT,
    label TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    finished_at INTEGER,
    result_digest TEXT,
    result_chars INTEGER,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_subagent_runs_parent
    ON subagent_runs(parent_session_id, created_at);
"""

#: RFC-0006 §9.1 rows 12: appended columns, applied to pre-existing databases.
#: The three ALTERs per table are additive; SQLite has no `ADD COLUMN IF NOT
#: EXISTS`, so the presence check happens in `_ensure_columns`.
_APPENDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "messages": (
        ("partial", "INTEGER NOT NULL DEFAULT 0"),
        ("superseded_by", "TEXT"),
    ),
    "turns": (
        ("root_turn_id", "TEXT"),
        ("parent_turn_id", "TEXT"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("superseded_by", "TEXT"),
        ("superseded_reason", "TEXT"),
        ("user_message_id", "TEXT"),
        ("partial_message_id", "TEXT"),
        ("user_parts", "TEXT"),
    ),
    #: RFC-0010 §4.2: user-facing metadata. All columns are additive with safe
    #: defaults; `updated_at` is backfilled to `created_at` right after the
    #: ALTERs (below), so pre-RFC sessions sort by their last-known activity.
    "sessions": (
        ("group_id", "TEXT"),
        ("icon", "TEXT"),
        ("color", "TEXT"),
        ("pinned_at", "INTEGER"),
        ("archived_at", "INTEGER"),
        ("unread", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
    ),
}

_SUPPORTS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


def _parts_json(message: Message) -> str | None:
    """`parts` column: JSON array, or NULL for a legacy text-only message."""
    if not message.parts:
        return None
    return json.dumps([dict(p) for p in message.parts], ensure_ascii=False)


def _message_from_row(row: sqlite3.Row) -> Message:
    """Decode one row, tolerating a database that predates `parts`.

    `row.keys()` instead of `row["parts"]`: a store opened read-only against an
    old file (or a caller poking a hand-built fixture) must not raise KeyError
    just because the migration had nothing to write. The same probe covers the
    RFC-0006 columns (`partial`, `superseded_by`): an old row reads as the
    documented defaults instead of blowing up the load.
    """
    columns = row.keys()
    parts: tuple[dict[str, Any], ...] = ()
    raw = row["parts"] if "parts" in columns else None
    if raw:
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if isinstance(decoded, list):
            parts = tuple(dict(p) for p in decoded if isinstance(p, dict))
    return Message(
        id=row["id"], session_id=row["session_id"], role=row["role"],
        content=row["content"], turn_id=row["turn_id"], seq=row["seq"],
        name=row["name"], tool_call_id=row["tool_call_id"], created_at=row["created_at"],
        partial=bool(row["partial"]) if "partial" in columns else False,
        superseded_by=row["superseded_by"] if "superseded_by" in columns else None,
        parts=parts,
    )



def _session_from_row(row: sqlite3.Row) -> Session:
    """Decode one sessions row, tolerating a database that predates a column.

    Same probe discipline as `_message_from_row` / `_turn_from_row`: a store
    opened against a file the migration never reached must read the documented
    defaults (RFC-0010 §4.2) instead of raising KeyError.
    """
    columns = row.keys()

    def optional(name: str, default: Any = None) -> Any:
        return row[name] if name in columns else default

    return Session(
        id=row["id"], created_at=row["created_at"], title=row["title"],
        seq_counter=row["seq_counter"], state=row["state"],
        model_id=optional("model_id"),
        group_id=optional("group_id"), icon=optional("icon"), color=optional("color"),
        pinned_at=optional("pinned_at"), archived_at=optional("archived_at"),
        unread=int(optional("unread", 0) or 0),
        updated_at=int(optional("updated_at", 0) or 0),
    )


def _group_from_row(row: sqlite3.Row) -> SessionGroup:
    columns = row.keys()

    def optional(name: str, default: Any = None) -> Any:
        return row[name] if name in columns else default

    return SessionGroup(
        group_id=row["group_id"], name=row["name"],
        icon=optional("icon"), color=optional("color"),
        sort_order=int(optional("sort_order", 0) or 0),
        created_at=int(row["created_at"]),
    )


#: RFC-0010 §5: metadata columns `update_session_meta` may write. Anything
#: else (seq_counter, state, model_id) has its own protocol method.
_META_COLUMNS = frozenset({
    "title", "icon", "color", "group_id", "pinned_at", "archived_at", "unread", "updated_at",
})

#: RFC-0010 §7.4: pinned first, then last activity descending, `id` breaks
#: ties so the same page set is reproducible across clients.
_LIST_ORDER = "(pinned_at IS NULL) ASC, updated_at DESC, created_at DESC, id ASC"


class SQLiteStore(SessionStore):
    def __init__(
        self,
        path: str | Path,
        *,
        checkpoint_bytes: int | None = WAL_CHECKPOINT_BYTES,
        checkpoint_min_interval_s: float = WAL_CHECKPOINT_MIN_INTERVAL_S,
    ) -> None:
        """`checkpoint_bytes`: WAL size that triggers a background checkpoint.

        `None` (or <= 0) disables it — the store then behaves like plain
        `wal_autocheckpoint=0`: fastest appends, unbounded WAL, reachable only
        by an operator who runs their own maintenance. `checkpoint_min_interval_s`
        rate-limits the opportunistic probe (see `schedule_checkpoint`).
        """
        self.path = Path(path)
        self._memory = str(path) == ":memory:"
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        #: Serializes checkpoint runs **between worker threads**. Deliberately
        #: not `_lock`: the event loop thread takes `_lock` for every write, and
        #: must never end up queued behind a multi-MiB page copy.
        self._ckpt_lock = threading.Lock()
        self._ckpt_inflight = False          # owned by the loop thread
        self._ckpt_task: asyncio.Task[None] | None = None
        self._ckpt_after = 0.0               # monotonic; probe gate
        self._ckpt_runs = 0                  # successful checkpoints (observability)
        self._closed = False
        self._checkpoint_bytes = (
            int(checkpoint_bytes) if checkpoint_bytes is not None and checkpoint_bytes > 0 else None
        )
        self._checkpoint_min_interval_s = max(0.0, float(checkpoint_min_interval_s))
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # The write path must not checkpoint: SQLite's auto-checkpoint runs
            # inside COMMIT on the committing thread, which here is the event
            # loop. `schedule_checkpoint()` replaces it (module docstring).
            self._conn.execute("PRAGMA wal_autocheckpoint=0")
            # WAL-only: on log restart (after a completed checkpoint) the writer
            # truncates the sidecar instead of leaving it at its high-water mark.
            self._conn.execute("PRAGMA journal_size_limit=0")
            self._conn.executescript(_SCHEMA)
            # RFC-0005 §5.3: two nullable columns, added idempotently so an
            # existing sessions.db keeps working (PRAGMA first, ALTER only when
            # the column is really missing). NULL = "no override".
            self._ensure_column("sessions", "model_id", "model_id TEXT")
            self._ensure_column("turns", "model_id", "model_id TEXT")
            # RFC-0007 §5.5 R5: the `coalesce` flag must survive a reload, or
            # replay coverage (and attach sizing) cannot tell a droppable
            # progress row from a real event.
            self._ensure_column("events", "coalesce", "coalesce INTEGER NOT NULL DEFAULT 0")
            # RFC-0006: appended columns (`messages.partial`,
            # `messages.superseded_by`, the turn version chain).
            self._ensure_columns()
            # RFC-0004 §6.2: `messages.parts` on a pre-parts database.
            self._migrate()
            # RFC-0010 §4.2: the list-order index needs the appended columns,
            # so it is created AFTER the migration (not in `_SCHEMA`, where a
            # fresh file would not have `pinned_at`/`updated_at` yet).
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_order"
                " ON sessions(archived_at, pinned_at, updated_at)"
            )
            # §4.2 backfill: a session that predates `updated_at` has never
            # shown activity in the new column — its last-known activity is
            # its creation. The predicate keeps this reentrant: rows written
            # after the migration always carry a real value (create coalesces,
            # append stamps now_ms), so this UPDATE converges to no-op.
            self._conn.execute("UPDATE sessions SET updated_at = created_at WHERE updated_at = 0")

    def _ensure_columns(self) -> None:
        for table, columns in _APPENDED_COLUMNS.items():
            present = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns:
                if name not in present:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------- migration
    def _migrate(self) -> None:
        """RFC-0004 §6.2: add `messages.parts` to a pre-parts database.

        Reentrant by construction: the `PRAGMA table_info` probe decides, so a
        second open of an already-migrated file is a no-op, and a fresh file
        (created by `_SCHEMA`) is a no-op too. The `except OperationalError`
        straddles the one race the probe cannot close — two processes opening
        the same file at once, where both see no column and the loser's ALTER
        fails with "duplicate column name". If the column is there afterwards,
        that is success, not an error.

        Nothing is rewritten: `parts` stays NULL for old rows and reads
        synthesise the text part (§6.3). Old binaries read the new file just
        fine — they select the columns they know, and `content` is still there.
        """
        if "parts" in self._message_columns():
            return
        try:
            self._conn.execute("ALTER TABLE messages ADD COLUMN parts TEXT")
        except sqlite3.OperationalError:
            if "parts" not in self._message_columns():
                raise

    def _message_columns(self) -> set[str]:
        rows = self._conn.execute("PRAGMA table_info(messages)").fetchall()
        return {str(row[1]) for row in rows}

    # ------------------------------------------------------------------ txn
    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def close(self) -> None:
        """Truncate the WAL, then close the connection.

        `TRUNCATE` (not the `PASSIVE` the background path uses) is right exactly
        once, at shutdown: it backfills every frame *and* resets the `-wal` file
        to zero bytes, so a clean exit leaves no 16 MiB sidecar behind. Shutdown
        is also the one place a blocking checkpoint costs nothing.

        A failing checkpoint must not turn a clean shutdown into a crash, and
        must not mask the error the caller is already unwinding — so it is
        logged and the close proceeds. `sqlite3.Connection.close()` errors are
        the caller's to see and are left to propagate.

        Idempotent (`SessionLog.close` racing a transport teardown): the second
        call is a no-op, not a `ProgrammingError` on a closed connection.

        If a background checkpoint is already running, the wait is bounded by
        `WAL_CLOSE_TRUNCATE_TIMEOUT_S` and the truncate is skipped when it
        expires: shutdown must not hang behind an optimisation. The WAL is
        durable regardless — the next process to open the file recovers it.
        """
        with self._lock:
            if self._closed:
                return
            # Held across the flag flip *and* the truncate: a worker that already
            # entered `checkpoint_passive` finishes first, and one that has not
            # yet will see `_closed` and do nothing.
            acquired = self._ckpt_lock.acquire(timeout=WAL_CLOSE_TRUNCATE_TIMEOUT_S)
            try:
                self._closed = True
                if acquired and not self._memory:
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error as exc:
                        log.warning("wal checkpoint(TRUNCATE) on close failed: %s", exc)
                elif not acquired:
                    log.warning("wal checkpoint(TRUNCATE) skipped: a checkpoint is still running")
            finally:
                if acquired:
                    self._ckpt_lock.release()
            self._conn.close()

    # --------------------------------------------------------- wal checkpoint
    def wal_bytes(self) -> int:
        """Size of the `-wal` sidecar in bytes (0 when absent, or for `:memory:`).

        One `stat()` — this is the hot-path probe, so it must stay allocation-
        and lock-free. A missing file is the normal "WAL was reset" answer.
        """
        if self._memory:
            return 0
        try:
            return os.stat(f"{self.path}-wal").st_size
        except OSError:
            return 0

    def checkpoint_passive(self) -> tuple[int, int, int] | None:
        """`PRAGMA wal_checkpoint(PASSIVE)` on a private connection.

        Blocking, on purpose: the caller is a worker thread
        (`asyncio.to_thread`) or a background thread, never the event loop.
        A *private* connection is what makes that safe — the shared connection
        is only ever driven by the loop thread's short transactions, so a
        checkpoint cannot make `_lock` contended and cannot stretch a commit.

        PASSIVE is the only mode that is safe off-thread: it never waits for
        readers or writers, so it cannot deadlock against an open transaction
        on the main connection and cannot fail a commit with `SQLITE_BUSY`. Its
        cost is that it may backfill nothing while a reader pins an old
        snapshot (`busy=1`); the next probe retries.

        Returns the `PRAGMA` triple `(busy, log_frames, checkpointed_frames)`,
        or `None` for an in-memory/closed store or a SQLite error.
        """
        if self._memory:
            return None
        with self._ckpt_lock:
            if self._closed:
                return None
            # Fresh connection each run: checkpoints are rare (once per
            # `checkpoint_bytes`), and a per-run connection has no lifecycle to
            # leak — nothing survives a crash, an exception, or `close()`.
            try:
                conn = sqlite3.connect(str(self.path), isolation_level=None)
            except sqlite3.Error as exc:
                log.warning("wal checkpoint connection failed: %s", exc)
                return None
            try:
                row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            except sqlite3.Error as exc:
                log.warning("wal checkpoint(PASSIVE) failed: %s", exc)
                return None
            finally:
                conn.close()
            self._ckpt_runs += 1
            if row is None:  # pragma: no cover - PRAGMA always returns a row
                return None
            return (int(row[0]), int(row[1]), int(row[2]))

    def checkpoint_if_needed(self) -> bool:
        """Checkpoint now when the WAL is over the threshold. Blocking.

        The explicit entry point (sync callers, tests, an operator's shutdown
        hook). Unlike `schedule_checkpoint` it ignores the retry gate: a caller
        that asks has already decided the cost is acceptable.
        """
        if self._checkpoint_bytes is None or self._closed:
            return False
        if self.wal_bytes() <= self._checkpoint_bytes:
            return False
        return self.checkpoint_passive() is not None

    def schedule_checkpoint(self) -> bool:
        """Hot-path probe: one `stat()`; the page copy happens in a thread.

        Called from the async write path after a commit. Cheap by contract —
        no database access, no lock, no `await` — so the append that crossed
        the threshold does not pay for the checkpoint. Returns True when a
        background checkpoint was scheduled.
        """
        if self._checkpoint_bytes is None or self._closed or self._ckpt_inflight:
            return False
        now = time.monotonic()
        if now < self._ckpt_after:
            return False
        if self.wal_bytes() <= self._checkpoint_bytes:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # a sync caller: use `checkpoint_if_needed()`
            return False
        self._ckpt_inflight = True
        self._ckpt_after = now + self._checkpoint_min_interval_s
        self._ckpt_task = loop.create_task(self._background_checkpoint())
        return True

    async def _background_checkpoint(self) -> None:
        """`checkpoint_passive` on the default executor, never on the loop."""
        try:
            await asyncio.to_thread(self.checkpoint_passive)
        except Exception as exc:  # pragma: no cover - maintenance never kills a turn
            # Includes the shutdown race (`to_thread` after the executor closed)
            # and anything sqlite raises outside the guarded PRAGMA: a
            # checkpoint is maintenance, never a reason to fail a write.
            log.warning("background wal checkpoint failed: %s", exc)
        finally:
            self._ckpt_inflight = False
            self._ckpt_task = None

    # --------------------------------------------------------------- reconcile
    def reconcile_unfinished_turns(self, *, reason: str = "restart") -> int:
        """Startup: turn rows a dead process left non-terminal become interrupted.

        The turn queue lives in memory only, so `queued`/`running`/
        `waiting_*` rows from a previous process can never finish: every
        snapshot would advertise a turn that runs forever, and `regenerate`/
        `edit` would refuse the version chain with `turn_active` forever.

        One transaction for the whole repair: the turn row and the terminal
        `turn.interrupted` event must land together, or a crash in between
        leaves a state no client can explain (the same rule `append()` follows
        for every other terminal write). The seq comes from `seq_counter`
        under `BEGIN IMMEDIATE`, so it can never collide with an event a
        replaying client already has.

        `ended_at` is stamped only when the dead process did not stamp one,
        and `updated_at` is deliberately NOT touched: a crash is not activity,
        so a repaired session must not jump to the top of `session/list`.

        Idempotent: a second call (dev hot reload installing twice) finds no
        non-terminal rows and writes nothing.
        """
        terminal = tuple(sorted(state.value for state in TERMINAL_STATES))
        placeholders = ",".join("?" for _ in terminal)
        now = now_ms()
        reconciled = 0
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, session_id FROM turns"
                f" WHERE state NOT IN ({placeholders}) ORDER BY session_id, rowid",
                terminal,
            ).fetchall()
            if not rows:
                return 0
            by_session: dict[str, list[str]] = {}
            for row in rows:
                by_session.setdefault(str(row["session_id"]), []).append(str(row["id"]))

            for session_id, turn_ids in by_session.items():
                counter = conn.execute(
                    "SELECT seq_counter FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                seq = int(counter["seq_counter"]) if counter is not None else 0
                for turn_id in turn_ids:
                    cursor = conn.execute(
                        "UPDATE turns SET state=?, ended_at=COALESCE(ended_at, ?),"
                        f" stopped_reason=? WHERE id=? AND state NOT IN ({placeholders})",
                        (TurnState.INTERRUPTED.value, now, reason, turn_id, *terminal),
                    )
                    # Another process reconciled it between our SELECT and this
                    # UPDATE (or it finished for real): count only our writes.
                    if cursor.rowcount == 0:
                        continue
                    reconciled += 1
                    if counter is None:
                        # Orphan turn row (no `sessions` row): repair the state,
                        # but there is no seq space to write an event into.
                        continue
                    seq += 1
                    payload = {
                        "turn_id": turn_id,
                        "state": TurnState.INTERRUPTED.value,
                        "by": "system",
                        # A dead process streamed nothing we could persist; the
                        # fields stay present so clients that read them see the
                        # documented "no partial text" value, not a missing key.
                        "partial_message_id": None,
                        "partial_chars": 0,
                        "data": {"reason": reason},
                    }
                    conn.execute(
                        "INSERT INTO events(session_id, seq, type, payload, ts, coalesce)"
                        " VALUES(?,?,?,?,?,0)",
                        (session_id, seq, "turn.interrupted",
                         json.dumps(payload, ensure_ascii=False), now),
                    )
                if counter is not None and seq > int(counter["seq_counter"]):
                    conn.execute(
                        "UPDATE sessions SET seq_counter=? WHERE id=?", (seq, session_id)
                    )
        return reconciled

    # ------------------------------------------------------------------ writes
    def create_session(self, session: Session) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions(id, created_at, title, seq_counter, state, model_id,"
                " group_id, icon, color, pinned_at, archived_at, unread, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session.id, session.created_at, session.title, session.seq_counter, session.state,
                 session.model_id, session.group_id, session.icon, session.color,
                 session.pinned_at, session.archived_at, session.unread,
                 # RFC-0010 §4.2: a session with no recorded activity is as old
                 # as itself — never let 0 sink it below every migrated row.
                 session.updated_at or session.created_at),
            )

    def append(
        self,
        sess_id: str,
        msgs: Sequence[Message],
        events: Sequence[Event],
        *,
        turns: Sequence[Turn] = (),
        message_updates: Sequence[tuple[str, str | None]] = (),
    ) -> None:
        """Persist one batch atomically, before broadcast.

        RFC-0006 §4.2/§6.4 widen "one batch" to the turn row and the
        `superseded_by` pointers as well: an interrupt must commit
        `state=interrupted` + the partial message + `turn.interrupted` together,
        and a regenerate must commit the old turn's pointer + the new turn + its
        events together, or a crash in between leaves a state no client can
        explain. `message_updates` is `[(message_id, superseded_by)]` — the only
        mutation a message row ever accepts (its content stays immutable).
        """
        if not msgs and not events and not turns and not message_updates:
            return
        with self._transaction() as conn:
            # RFC-0010 §4.2: real ACTIVITY (a message or turn row landed) is
            # what advances `updated_at` — the FR-8.26 list-order key. Attach /
            # queue bookkeeping events carry neither and must not float a
            # session to the top of "today" on every reconnect.
            if msgs or turns:
                conn.execute(
                    "UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), sess_id)
                )
            for turn in turns:
                self._upsert_turn(conn, sess_id, turn)
            if msgs:
                conn.executemany(
                    "INSERT INTO messages(id, session_id, turn_id, role, content, seq, name,"
                    " tool_call_id, created_at, partial, superseded_by, parts)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (m.id, m.session_id, m.turn_id, m.role, m.content, m.seq,
                         m.name, m.tool_call_id, m.created_at, int(bool(m.partial)),
                         m.superseded_by, _parts_json(m))
                        for m in msgs
                    ],
                )
            for message_id, superseded_by in message_updates:
                conn.execute(
                    "UPDATE messages SET superseded_by=? WHERE session_id=? AND id=?",
                    (superseded_by, sess_id, message_id),
                )
            if events:
                conn.executemany(
                    "INSERT INTO events(session_id, seq, type, payload, ts, coalesce) VALUES(?,?,?,?,?,?)",
                    [(e.session_id, e.seq, e.type, json.dumps(dict(e.payload), ensure_ascii=False),
                      e.ts, 1 if e.coalesce else 0) for e in events],
                )

    def _upsert_turn(self, conn: sqlite3.Connection, sess_id: str, turn: Turn) -> None:
        conn.execute(
            "INSERT INTO turns(id, session_id, state, started_at, ended_at, client_msg_id,"
            " user_text, stopped_reason, error_code, error_message, usage, tool_calls,"
            " root_turn_id, parent_turn_id, version, superseded_by, superseded_reason,"
            " user_message_id, partial_message_id, user_parts, model_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
            " started_at=excluded.started_at, ended_at=excluded.ended_at,"
            " stopped_reason=excluded.stopped_reason, error_code=excluded.error_code,"
            " error_message=excluded.error_message, usage=excluded.usage,"
            " tool_calls=excluded.tool_calls, root_turn_id=excluded.root_turn_id,"
            " parent_turn_id=excluded.parent_turn_id, version=excluded.version,"
            " superseded_by=excluded.superseded_by,"
            " superseded_reason=excluded.superseded_reason,"
            " user_message_id=excluded.user_message_id,"
            " partial_message_id=excluded.partial_message_id,"
            " user_parts=excluded.user_parts, model_id=excluded.model_id",
            (
                turn.id, sess_id, turn.state.value, turn.started_at, turn.ended_at,
                turn.client_msg_id, turn.user_text, turn.stopped_reason, turn.error_code,
                turn.error_message, json.dumps(dict(turn.usage), ensure_ascii=False),
                json.dumps(list(turn.tool_calls), ensure_ascii=False),
                turn.root_turn_id, turn.parent_turn_id, int(turn.version or 1),
                turn.superseded_by, turn.superseded_reason, turn.user_message_id,
                turn.partial_message_id,
                json.dumps(list(turn.user_parts), ensure_ascii=False),
                turn.model_id,
            ),
        )

    def update_turn(self, sess_id: str, turn: Turn) -> None:
        with self._lock:
            self._upsert_turn(self._conn, sess_id, turn)

    def next_seq(self, sess_id: str) -> int:
        with self._lock:
            if _SUPPORTS_RETURNING:
                row = self._conn.execute(
                    "UPDATE sessions SET seq_counter = seq_counter + 1 WHERE id=?"
                    " RETURNING seq_counter",
                    (sess_id,),
                ).fetchone()
            else:  # pragma: no cover - exercised only on ancient SQLite
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    row = self._conn.execute(
                        "SELECT seq_counter FROM sessions WHERE id=?", (sess_id,)
                    ).fetchone()
                    if row is not None:
                        self._conn.execute(
                            "UPDATE sessions SET seq_counter=? WHERE id=?",
                            (row[0] + 1, sess_id),
                        )
                        row = (row[0] + 1,)
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
            if row is None:
                raise VadeError("NOT_FOUND", f"session {sess_id!r} does not exist")
            return int(row[0])

    def set_session_model(self, sess_id: str, model_id: str | None) -> None:
        """Persist (or clear, with None) the session-level model override."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE sessions SET model_id=? WHERE id=?", (model_id, sess_id)
            )
            if cursor.rowcount == 0:
                raise VadeError("NOT_FOUND", f"session {sess_id!r} does not exist")

    def close_session(self, sess_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET state='closed' WHERE id=?", (sess_id,))

    # ------------------------------------------------------------------ reads
    def read_since(self, sess_id: str, seq: int) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, seq, type, payload, ts, coalesce FROM events"
                " WHERE session_id=? AND seq>? ORDER BY seq",
                (sess_id, seq),
            ).fetchall()
        return [
            Event(
                session_id=r["session_id"], seq=r["seq"], type=r["type"],
                payload=json.loads(r["payload"]), ts=r["ts"],
                coalesce=bool(r["coalesce"]),
            )
            for r in rows
        ]

    def last_seq(self, sess_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT seq_counter FROM sessions WHERE id=?", (sess_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def _turn_from_row(self, row: sqlite3.Row) -> Turn:
        """Decode one turn row, tolerating a database that predates an append.

        `row.keys()` probes instead of `row[...]`: a store opened read-only
        against an older file must read the documented default (`NULL` /
        `1` / `()`) rather than raise KeyError on a column the migration never
        had a chance to add. RFC-0005's `model_id` and RFC-0006's version chain
        are both append-only, so "missing" always has a safe default.
        """
        columns = row.keys()

        def optional(name: str, default: Any = None) -> Any:
            return row[name] if name in columns else default

        return Turn(
            id=row["id"], session_id=row["session_id"], state=TurnState(row["state"]),
            client_msg_id=row["client_msg_id"], user_text=row["user_text"] or "",
            started_at=row["started_at"], ended_at=row["ended_at"],
            stopped_reason=row["stopped_reason"], error_code=row["error_code"],
            error_message=row["error_message"],
            usage=json.loads(row["usage"] or "{}"),
            tool_calls=tuple(json.loads(row["tool_calls"] or "[]")),
            model_id=optional("model_id"),
            root_turn_id=optional("root_turn_id"), parent_turn_id=optional("parent_turn_id"),
            version=int(optional("version", 1) or 1), superseded_by=optional("superseded_by"),
            superseded_reason=optional("superseded_reason"),
            user_message_id=optional("user_message_id"),
            partial_message_id=optional("partial_message_id"),
            user_parts=tuple(json.loads(optional("user_parts") or "[]")),
        )

    def find_turn_by_client_msg(self, sess_id: str, client_msg_id: str) -> Turn | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE session_id=? AND client_msg_id=?",
                (sess_id, client_msg_id),
            ).fetchone()
        return self._turn_from_row(row) if row else None

    def find_turn(self, sess_id: str, turn_id: str) -> Turn | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE session_id=? AND id=?", (sess_id, turn_id)
            ).fetchone()
        return self._turn_from_row(row) if row else None

    def load(self, sess_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (sess_id,)).fetchone()
            if row is None:
                return None
            msg_rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY rowid", (sess_id,)
            ).fetchall()
            turn_rows = self._conn.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY rowid", (sess_id,)
            ).fetchall()
        return Session(
            id=row["id"], created_at=row["created_at"], title=row["title"],
            seq_counter=row["seq_counter"], state=row["state"], model_id=row["model_id"],
            # RFC-0010 §4.2: always present here — `load` only runs after the
            # reentrant migration added the metadata columns.
            group_id=row["group_id"], icon=row["icon"], color=row["color"],
            pinned_at=row["pinned_at"], archived_at=row["archived_at"],
            unread=row["unread"], updated_at=row["updated_at"],
            messages=tuple(_message_from_row(r) for r in msg_rows),
            turns=tuple(self._turn_from_row(r) for r in turn_rows),
        )

    def list_sessions(self, limit: int = 50) -> list[Session]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out: list[Session] = []
        for row in rows:
            session = self.load(row["id"])
            if session is not None:
                out.append(session)
        return out

    # ------------------------------------------------------------------ RFC-0010
    def list_session_rows(
        self, *, limit: int = 50, offset: int = 0,
        group_id: Any = UNSET, archived: bool = False,
    ) -> list[Session]:
        """RFC-0010 §5: one page of the LIGHT list (metadata only).

        `group_id` follows the `UNSET` protocol: omitted = every group,
        `None` = ungrouped only, a string = that group. The caller has already
        validated group existence; this method only filters.
        """
        where = ["archived_at IS NULL" if not archived else "archived_at IS NOT NULL"]
        params: list[Any] = []
        if group_id is not UNSET:
            if group_id is None:
                where.append("group_id IS NULL")
            else:
                where.append("group_id = ?")
                params.append(group_id)
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM sessions WHERE {' AND '.join(where)}"
                f" ORDER BY {_LIST_ORDER} LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [_session_from_row(r) for r in rows]

    def update_session_meta(self, sess_id: str, fields: dict) -> None:
        """RFC-0010 §5: write a whitelist of metadata columns in one UPDATE.

        Raises NOT_FOUND when the session row is gone; unknown keys are a
        programming error at the caller (manager), refused here as well so a
        future field cannot silently bypass the column set.
        """
        unknown = set(fields) - _META_COLUMNS
        if unknown:
            raise VadeError("INTERNAL", f"metadata fields not writable: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{name}=?" for name in fields)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE sessions SET {sets} WHERE id=?",
                (*fields.values(), sess_id),
            )
            if cursor.rowcount == 0:
                raise VadeError("NOT_FOUND", f"session {sess_id!r} does not exist")

    def delete_session(self, sess_id: str) -> None:
        """RFC-0010 §8.2: hard-delete every row of one session, atomically."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=?", (sess_id,))
            conn.execute("DELETE FROM turns WHERE session_id=?", (sess_id,))
            conn.execute("DELETE FROM events WHERE session_id=?", (sess_id,))
            conn.execute("DELETE FROM sessions WHERE id=?", (sess_id,))

    def create_group(self, group: SessionGroup) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO session_groups(group_id, name, icon, color, sort_order, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (group.group_id, group.name, group.icon, group.color,
                 group.sort_order, group.created_at),
            )

    def get_group(self, group_id: str) -> SessionGroup | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_groups WHERE group_id=?", (group_id,)
            ).fetchone()
        return _group_from_row(row) if row else None

    def list_groups(self) -> list[SessionGroup]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM session_groups ORDER BY sort_order ASC, created_at ASC, group_id ASC"
            ).fetchall()
        return [_group_from_row(r) for r in rows]

    def update_group_fields(self, group_id: str, fields: dict) -> None:
        allowed = frozenset({"name", "icon", "color", "sort_order"})
        unknown = set(fields) - allowed
        if unknown:
            raise VadeError("INTERNAL", f"group fields not writable: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{name}=?" for name in fields)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE session_groups SET {sets} WHERE group_id=?",
                (*fields.values(), group_id),
            )
            if cursor.rowcount == 0:
                raise VadeError("NOT_FOUND", f"group {group_id!r} does not exist")

    def delete_group(self, group_id: str) -> int:
        """RFC-0010 §8.1: members go to ungrouped, NEVER deleted (one txn)."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET group_id=NULL WHERE group_id=?", (group_id,)
            )
            unassigned = cursor.rowcount
            conn.execute("DELETE FROM session_groups WHERE group_id=?", (group_id,))
        return unassigned

    def group_session_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id, COUNT(*) AS n FROM sessions"
                " WHERE group_id IS NOT NULL GROUP BY group_id"
            ).fetchall()
        return {row["group_id"]: int(row["n"]) for row in rows}

    # --------------------------------------------------------------- RFC-0014
    def search_sessions(
        self, query: str, *, limit: int = LIMIT_DEFAULT, offset: int = 0,
        session_id: str | None = None,
    ) -> tuple[list[SearchHit], int]:
        """RFC-0014 §4: parameterized LIKE scan over titles + live bodies.

        Injection posture (§4.10): the pattern (`%` + `escape_like(query)` +
        `%`) and every scope value travel as `?` parameters — no query text is
        ever concatenated into SQL. `%`, `_` and the backslash are escaped so
        they match literally; a quote or `--` stays a plain bound value.

        The two tier queries and the per-page snippet lookups share ONE
        `_lock` hold, so a concurrent `append()` cannot split a title row
        from its body rows. Ordering is §4.4: title tier first (SQL-side
        `updated_at DESC, id ASC`), then the body tier (Python-side sort,
        same keys), pages sliced across the concatenation.
        """
        like = "LIKE ? ESCAPE '\\'"
        pattern = f"%{escape_like(query)}%"
        title_scope, body_scope, scope_params = "", "", []
        if session_id is not None:
            title_scope = " AND id = ?"
            body_scope = " AND session_id = ?"
            scope_params = [session_id]
        with self._lock:
            title_rows = self._conn.execute(
                "SELECT id AS session_id, title, updated_at, archived_at FROM sessions"
                f" WHERE title {like}{title_scope}"
                " ORDER BY updated_at DESC, id ASC",
                (pattern, *scope_params),
            ).fetchall()
            # §4.1: `superseded_by IS NULL` keeps edit history out of the scan;
            # §4.7: MIN(rowid) = the FIRST matching message (earliest landing).
            body_rows = self._conn.execute(
                "SELECT f.session_id AS session_id, s.title AS title,"
                " s.updated_at AS updated_at, s.archived_at AS archived_at"
                " FROM (SELECT session_id, MIN(rowid) AS first_row FROM messages"
                f"       WHERE content {like} AND superseded_by IS NULL{body_scope}"
                "       GROUP BY session_id) f"
                " JOIN sessions s ON s.id = f.session_id",
                (pattern, *scope_params),
            ).fetchall()

            title_ids = {row["session_id"] for row in title_rows}
            body_only = [row for row in body_rows if row["session_id"] not in title_ids]
            body_only.sort(key=lambda row: (-row["updated_at"], row["session_id"]))
            total = len(title_rows) + len(body_only)
            page = [*title_rows, *body_only][offset:offset + limit]

            hits: list[SearchHit] = []
            for tier, row in enumerate(page):
                is_title = tier < len(title_rows)
                common = {
                    "session_id": row["session_id"],
                    "title": row["title"],
                    "updated_at": int(row["updated_at"]),
                    "archived": bool(row["archived_at"]),
                }
                if is_title:
                    hits.append(SearchHit(matched_in="title", **common))
                    continue
                message = self._conn.execute(
                    "SELECT id, content FROM messages WHERE session_id=?"
                    f" AND content {like} AND superseded_by IS NULL"
                    " ORDER BY rowid LIMIT 1",
                    (row["session_id"], pattern),
                ).fetchone()
                message_id = snippet = snippet_offset = None
                if message is not None:
                    message_id = message["id"]
                    at = find_ascii_ci(message["content"], query)
                    if at >= 0:  # LIKE and the locator fold ASCII identically
                        snippet_offset = at
                        snippet = make_snippet(message["content"], at)
                hits.append(SearchHit(
                    matched_in="body", message_id=message_id,
                    snippet=snippet, snippet_offset=snippet_offset, **common,
                ))
        return hits, total

    # --------------------------------------------------------------- RFC-0012
    def upsert_subagent_run(self, run: SubagentRun) -> None:
        """RFC-0012 §4.1: INSERT ... ON CONFLICT DO UPDATE — the manager swaps
        frozen records, so every state transition is one full-row write. The
        `CREATE TABLE IF NOT EXISTS` in `_SCHEMA` makes this reentrant for a
        database that predates the table."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO subagent_runs("
                "run_id, parent_session_id, parent_turn_id, child_session_id,"
                " label, state, created_at, finished_at, result_digest,"
                " result_chars, error_code)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                " parent_turn_id=excluded.parent_turn_id,"
                " child_session_id=excluded.child_session_id,"
                " label=excluded.label, state=excluded.state,"
                " created_at=excluded.created_at,"
                " finished_at=excluded.finished_at,"
                " result_digest=excluded.result_digest,"
                " result_chars=excluded.result_chars,"
                " error_code=excluded.error_code",
                (
                    run.run_id, run.parent_session_id, run.parent_turn_id,
                    run.child_session_id, run.label, run.state.value,
                    run.created_at, run.finished_at, run.result_digest,
                    run.result_chars, run.error_code,
                ),
            )

    def get_subagent_run(self, run_id: str) -> SubagentRun | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subagent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._subagent_run_from_row(row) if row is not None else None

    def list_subagent_runs(
        self, *, session_id: str | None = None, states: Sequence[str] | None = None,
    ) -> list[SubagentRun]:
        """RFC-0012 §6.1: fixed order (created_at asc, run_id tiebreak). The
        manager slices pages and builds the opaque cursor on top of this."""
        where: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            where.append("parent_session_id=?")
            params.append(session_id)
        if states:
            where.append(
                f"state IN ({','.join('?' for _ in states)})"
            )
            params.extend(states)
        sql = "SELECT * FROM subagent_runs"
        if where:
            sql += f" WHERE {' AND '.join(where)}"
        sql += " ORDER BY created_at ASC, run_id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._subagent_run_from_row(r) for r in rows]

    @staticmethod
    def _subagent_run_from_row(row: sqlite3.Row) -> SubagentRun:
        try:
            state = SubagentState(row["state"])
        except ValueError:  # pragma: no cover - a corrupted row is not a crash
            state = SubagentState.SPAWNED
        return SubagentRun(
            run_id=row["run_id"],
            parent_session_id=row["parent_session_id"],
            label=row["label"],
            parent_turn_id=row["parent_turn_id"],
            child_session_id=row["child_session_id"],
            state=state,
            created_at=int(row["created_at"]),
            finished_at=row["finished_at"],
            result_digest=row["result_digest"],
            result_chars=row["result_chars"],
            error_code=row["error_code"],
        )
