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
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..errors import VadeError
from .models import (
    Event, Message, Session, SessionGroup, SubagentRun, SubagentState,
    Turn, TurnState, now_ms,
)
from .search import LIMIT_DEFAULT, SearchHit, escape_like, find_ascii_ci, make_snippet
from .store import UNSET, SessionStore

__all__ = ["SQLiteStore"]

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
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            # RFC-0005 §5.3: two nullable columns, added idempotently so an
            # existing sessions.db keeps working (PRAGMA first, ALTER only when
            # the column is really missing). NULL = "no override".
            self._ensure_column("sessions", "model_id", "model_id TEXT")
            self._ensure_column("turns", "model_id", "model_id TEXT")
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
        with self._lock:
            self._conn.close()

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
                    "INSERT INTO events(session_id, seq, type, payload, ts) VALUES(?,?,?,?,?)",
                    [(e.session_id, e.seq, e.type, json.dumps(dict(e.payload), ensure_ascii=False),
                      e.ts) for e in events],
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
                "SELECT session_id, seq, type, payload, ts FROM events"
                " WHERE session_id=? AND seq>? ORDER BY seq",
                (sess_id, seq),
            ).fetchall()
        return [
            Event(
                session_id=r["session_id"], seq=r["seq"], type=r["type"],
                payload=json.loads(r["payload"]), ts=r["ts"],
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
