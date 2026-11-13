"""SessionStore contract: atomic seq, batch append, recovery, idempotency."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from vade.session.models import Event, Message, Session, Turn, TurnState
from vade.session.sqlite_store import SQLiteStore


def _store(tmp_path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "sessions.db")


def test_next_seq_is_strictly_increasing(tmp_path):
    store = _store(tmp_path)
    store.create_session(Session(id="s1", created_at=1))
    assert [store.next_seq("s1") for _ in range(5)] == [1, 2, 3, 4, 5]
    assert store.last_seq("s1") == 5


def test_next_seq_is_atomic_under_threads(tmp_path):
    store = _store(tmp_path)
    store.create_session(Session(id="s1", created_at=1))

    def allocate(_i):
        return store.next_seq("s1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(allocate, range(200)))
    assert sorted(values) == list(range(1, 201))
    assert len(set(values)) == 200


def test_append_batch_persists_messages_and_events_together(tmp_path):
    store = _store(tmp_path)
    store.create_session(Session(id="s1", created_at=1))
    seq = store.next_seq("s1")
    message = Message(id="m1", session_id="s1", role="user", content="hi", seq=seq)
    event = Event(session_id="s1", seq=seq, type="turn.queued", payload={"turn_id": "t1"})
    store.append("s1", [message], [event])

    loaded = store.load("s1")
    assert loaded is not None
    assert [m.content for m in loaded.messages] == ["hi"]
    assert [e.type for e in store.read_since("s1", 0)] == ["turn.queued"]
    assert store.read_since("s1", seq) == []


def test_turn_roundtrip_and_recovery_across_reopen(tmp_path):
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path)
    store.create_session(Session(id="s1", created_at=1, title="t"))
    turn = Turn(id="t1", session_id="s1", state=TurnState.RUNNING, client_msg_id="c1",
                user_text="hello", started_at=5)
    store.update_turn("s1", turn)
    store.close()

    reopened = SQLiteStore(path)
    loaded = reopened.load("s1")
    assert loaded is not None
    assert loaded.title == "t"
    assert loaded.turns[0].state is TurnState.RUNNING
    assert loaded.turns[0].user_text == "hello"
    assert reopened.find_turn_by_client_msg("s1", "c1").id == "t1"


def test_client_msg_id_is_unique_per_session(tmp_path):
    store = _store(tmp_path)
    store.create_session(Session(id="s1", created_at=1))
    store.update_turn("s1", Turn(id="t1", session_id="s1", client_msg_id="dup"))
    with pytest.raises(sqlite3.IntegrityError):
        store.update_turn("s1", Turn(id="t2", session_id="s1", client_msg_id="dup"))


def test_unknown_session_raises_not_found(tmp_path):
    from vade.errors import VadeError

    store = _store(tmp_path)
    with pytest.raises(VadeError) as exc:
        store.next_seq("nope")
    assert exc.value.code == "NOT_FOUND"


def test_callers_contain_no_sql_literals():
    """D36: only the store implementation may know SQL."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "core" / "vade"
    offenders = []
    for name in ("session/manager.py", "session/queue.py", "loop/engine.py", "router/dispatch.py"):
        text = (root / name).read_text(encoding="utf-8").upper()
        if any(token in text for token in ("SELECT ", "INSERT INTO", "UPDATE ", "CREATE TABLE")):
            offenders.append(name)
    assert offenders == []
