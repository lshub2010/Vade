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
from .models import Event, Message, Session, Turn, TurnState
from .store import SessionStore

__all__ = ["SQLiteStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    title TEXT,
    seq_counter INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open'
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
    tool_calls TEXT NOT NULL DEFAULT '[]'
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
    created_at INTEGER NOT NULL
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
}

_SUPPORTS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


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
            self._ensure_columns()

    def _ensure_columns(self) -> None:
        for table, columns in _APPENDED_COLUMNS.items():
            present = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns:
                if name not in present:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ writes
    def create_session(self, session: Session) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions(id, created_at, title, seq_counter, state)"
                " VALUES(?,?,?,?,?)",
                (session.id, session.created_at, session.title, session.seq_counter, session.state),
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
            for turn in turns:
                self._upsert_turn(conn, sess_id, turn)
            if msgs:
                conn.executemany(
                    "INSERT INTO messages(id, session_id, turn_id, role, content, seq, name,"
                    " tool_call_id, created_at, partial, superseded_by)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (m.id, m.session_id, m.turn_id, m.role, m.content, m.seq,
                         m.name, m.tool_call_id, m.created_at, int(bool(m.partial)),
                         m.superseded_by)
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
            " user_message_id, partial_message_id, user_parts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
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
            " user_parts=excluded.user_parts",
            (
                turn.id, sess_id, turn.state.value, turn.started_at, turn.ended_at,
                turn.client_msg_id, turn.user_text, turn.stopped_reason, turn.error_code,
                turn.error_message, json.dumps(dict(turn.usage), ensure_ascii=False),
                json.dumps(list(turn.tool_calls), ensure_ascii=False),
                turn.root_turn_id, turn.parent_turn_id, int(turn.version or 1),
                turn.superseded_by, turn.superseded_reason, turn.user_message_id,
                turn.partial_message_id,
                json.dumps(list(turn.user_parts), ensure_ascii=False),
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
        return Turn(
            id=row["id"], session_id=row["session_id"], state=TurnState(row["state"]),
            client_msg_id=row["client_msg_id"], user_text=row["user_text"] or "",
            started_at=row["started_at"], ended_at=row["ended_at"],
            stopped_reason=row["stopped_reason"], error_code=row["error_code"],
            error_message=row["error_message"],
            usage=json.loads(row["usage"] or "{}"),
            tool_calls=tuple(json.loads(row["tool_calls"] or "[]")),
            root_turn_id=row["root_turn_id"], parent_turn_id=row["parent_turn_id"],
            version=int(row["version"] or 1), superseded_by=row["superseded_by"],
            superseded_reason=row["superseded_reason"],
            user_message_id=row["user_message_id"],
            partial_message_id=row["partial_message_id"],
            user_parts=tuple(json.loads(row["user_parts"] or "[]")),
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
            seq_counter=row["seq_counter"], state=row["state"],
            messages=tuple(
                Message(
                    id=r["id"], session_id=r["session_id"], role=r["role"],
                    content=r["content"], turn_id=r["turn_id"], seq=r["seq"],
                    name=r["name"], tool_call_id=r["tool_call_id"], created_at=r["created_at"],
                    partial=bool(r["partial"]), superseded_by=r["superseded_by"],
                )
                for r in msg_rows
            ),
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
