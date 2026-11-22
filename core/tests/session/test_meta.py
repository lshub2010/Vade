"""RFC-0010: session metadata — groups / icon / color / pin, the ten session/* methods.

Two mutation targets the task pins (§11):
  ① group deletion that deletes member sessions -> test_group_delete_keeps_sessions goes red;
  ② pin ordering that ignores pinned_at         -> test_pinned_ordering_dominates_updated_at goes red.

Conventions follow test_module_status.py: sync test functions wrap an
`asyncio.run(scenario())` because `SessionLog.emit` needs a running loop.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from vade.session.manager import SessionLog
from vade.session.models import Event, Message, Session, SessionGroup, Turn
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import RpcError
from .helpers import CollectSink, GateExecutor, wait_until

SCHEMA_EVENTS = Path(__file__).resolve().parents[3] / "schema" / "events.json"
SCHEMA_RPC = Path(__file__).resolve().parents[3] / "schema" / "rpc.json"

TEN_METHODS = {
    "session/list", "session/rename", "session/pin", "session/archive", "session/delete",
    "session/group.list", "session/group.create", "session/group.update",
    "session/group.delete", "session/group.assign",
}
ROW_KEYS = {"session_id", "created_at", "updated_at", "title", "icon", "color",
            "group_id", "pinned_at", "archived_at", "unread", "state"}


class Conn:
    connection_id = "conn-1"
    device_id = "dev-01"


def _log(tmp_path) -> SessionLog:
    return SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))


def _rpc(log) -> SessionRPC:
    return SessionRPC(log)


async def _call(log, method: str, params: dict | None = None):
    """One dispatch; returns the result or the RpcError (never raises)."""
    return await _rpc(log).dispatch(Conn(), method, params or {})


def _err(result) -> RpcError:
    assert isinstance(result, RpcError), f"expected RpcError, got {result!r}"
    return result


def _ok(result) -> dict:
    assert not isinstance(result, RpcError), f"{result.code}: {result.message} ({result.data})"
    return result


def _meta_events(log, session_id: str) -> list[Event]:
    return [e for e in log.store.read_since(session_id, 0) if e.type == "session.updated"]


def _list_changes(sink: CollectSink) -> list[dict]:
    return [e["payload"] for e in sink.events if e["type"] == "session.list_changed"]


# --------------------------------------------------------------------------- store

def test_migration_is_reentrant_and_backfills_updated_at(tmp_path):
    """§4.2: a pre-RFC database gains the columns and sorts by its creation."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        "CREATE TABLE sessions(id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, title TEXT,"
        " seq_counter INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'open');"
        "INSERT INTO sessions VALUES('legacy', 1234, 'old', 0, 'open');"
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(legacy)
    columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(sessions)")}
    assert {"group_id", "icon", "color", "pinned_at", "archived_at", "unread", "updated_at"} <= columns
    loaded = store.load("legacy")
    assert loaded.updated_at == 1234  # backfilled to its last-known activity
    store.close()

    reopened = SQLiteStore(legacy)  # reentrant: second open is a no-op
    assert reopened.load("legacy").updated_at == 1234
    assert reopened.load("legacy").group_id is None
    reopened.close()


def test_updated_at_follows_activity_not_bookkeeping(tmp_path):
    """§4.2: a message bumps the list key; attach/queue events must not."""
    store = SQLiteStore(tmp_path / "sessions.db")
    store.create_session(Session(id="s1", created_at=1000))
    assert store.load("s1").updated_at == 1000  # coalesced at insert

    seq = store.next_seq("s1")
    store.append("s1", [], [Event(session_id="s1", seq=seq, type="session.attached", payload={})])
    assert store.load("s1").updated_at == 1000  # bookkeeping is not activity

    store.append("s1", [Message(id="m1", session_id="s1", role="user", content="hi", seq=seq)], [])
    assert store.load("s1").updated_at > 1000


def test_store_update_session_meta_whitelist_and_not_found(tmp_path):
    store = SQLiteStore(tmp_path / "sessions.db")
    store.create_session(Session(id="s1", created_at=1))
    store.update_session_meta("s1", {"icon": "code", "color": "#112233", "unread": 1})
    loaded = store.load("s1")
    assert (loaded.icon, loaded.color, loaded.unread) == ("code", "#112233", 1)

    with pytest.raises(Exception):
        store.update_session_meta("s1", {"seq_counter": 99})  # not a metadata column
    with pytest.raises(Exception):
        store.update_session_meta("ghost", {"title": "x"})


def test_store_delete_session_removes_every_row(tmp_path):
    store = SQLiteStore(tmp_path / "sessions.db")
    store.create_session(Session(id="s1", created_at=1))
    store.append("s1", [Message(id="m1", session_id="s1", role="user", content="hi")],
                 [Event(session_id="s1", seq=store.next_seq("s1"), type="turn.queued",
                        payload={"turn_id": "t1"})])
    store.update_turn("s1", Turn(id="t1", session_id="s1"))

    store.delete_session("s1")
    assert store.load("s1") is None
    for table in ("sessions", "turns", "messages", "events"):
        n = store._conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {'id' if table == 'sessions' else 'session_id'}=?", ("s1",)).fetchone()[0]
        assert n == 0, table


def test_store_group_roundtrip_across_reopen(tmp_path):
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path)
    group = SessionGroup(group_id="g1", name="work", icon="folder", color="#abcdef",
                         sort_order=3, created_at=5000)
    store.create_group(group)
    store.update_group_fields("g1", {"name": "deep work"})
    store.close()

    reopened = SQLiteStore(path)
    loaded = reopened.get_group("g1")
    assert (loaded.name, loaded.icon, loaded.color, loaded.sort_order, loaded.created_at) == \
        ("deep work", "folder", "#abcdef", 3, 5000)
    assert reopened.list_groups()[0].group_id == "g1"
    reopened.close()


# --------------------------------------------------------------------------- session/list

def test_session_list_pagination_walks_every_row_exactly_once(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        for i in range(1, 8):  # created_at 1000..7000: order is newest first
            log.store.create_session(Session(id=f"s{i}", created_at=i * 1000))

        seen: list[str] = []
        cursor: int | None = 0
        pages = 0
        while cursor is not None:
            page = _ok(await _call(log, "session/list", {"limit": 3, "cursor": cursor}))
            seen.extend(row["session_id"] for row in page["sessions"])
            cursor = page["next_cursor"]
            pages += 1
        assert pages == 3
        assert len(seen) == len(set(seen)) == 7
        assert seen == [f"s{i}" for i in range(7, 0, -1)]  # updated_at DESC (§7.4)
        assert _ok(await _call(log, "session/list", {"limit": 3, "cursor": 6}))["next_cursor"] is None

        for bad in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"cursor": -1}, {"cursor": "1"}):
            assert _err(await _call(log, "session/list", bad)).code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_session_list_row_shape_is_the_contract(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1, title="t", icon="code",
                                         color="#112233", group_id="g", pinned_at=9,
                                         archived_at=None, unread=1))
        page = _ok(await _call(log, "session/list", {}))
        assert set(page["sessions"][0]) == ROW_KEYS
        assert page["sessions"][0]["updated_at"] == 1  # coalesced to created_at

    asyncio.run(scenario())


def test_session_list_filters_groups_and_archived(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_group(SessionGroup(group_id="g1", name="work", created_at=1))
        log.store.create_session(Session(id="s1", created_at=2000, group_id="g1"))
        log.store.create_session(Session(id="s2", created_at=1000))
        log.store.create_session(Session(id="s3", created_at=3000, group_id="g1"))
        log.store.update_session_meta("s3", {"archived_at": 9999})

        default = _ok(await _call(log, "session/list", {}))["sessions"]
        assert [r["session_id"] for r in default] == ["s1", "s2"]  # archived hidden

        archived = _ok(await _call(log, "session/list", {"archived": True}))["sessions"]
        assert [r["session_id"] for r in archived] == ["s3"]

        in_group = _ok(await _call(log, "session/list", {"group_id": "g1"}))["sessions"]
        assert [r["session_id"] for r in in_group] == ["s1"]
        ungrouped = _ok(await _call(log, "session/list", {"group_id": None}))["sessions"]
        assert [r["session_id"] for r in ungrouped] == ["s2"]

        err = _err(await _call(log, "session/list", {"group_id": "ghost"}))
        assert err.code == "NOT_FOUND" and err.data["reason"] == "unknown_group"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/rename

def test_session_rename_trims_persists_and_emits(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1, title="old"))
        result = _ok(await _call(log, "session/rename", {"session_id": "s1", "title": "  new name  "}))
        assert result["ok"] is True
        assert result["session"]["title"] == "new name"
        assert log.store.load("s1").title == "new name"
        events = _meta_events(log, "s1")
        assert len(events) == 1
        assert events[0].payload["title"] == "new name"
        assert events[0].payload["by"] == "dev-01"  # §6.1: attributed
        # metadata must not float the session in the FR-8.26 order
        assert log.store.load("s1").updated_at == 1

    asyncio.run(scenario())


def test_session_rename_rejects_blank_overlong_and_unknown(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1))
        for bad in ("", "   ", "x" * 201, None, 42):
            err = _err(await _call(log, "session/rename", {"session_id": "s1", "title": bad}))
            assert err.code == "INVALID_ARGUMENT" and err.data["field"] == "title"
        assert _err(await _call(log, "session/rename", {"session_id": "ghost", "title": "x"})).code == "NOT_FOUND"
        assert _err(await _call(log, "session/rename", {"session_id": "s1"})).code == "INVALID_ARGUMENT"
        assert log.store.load("s1").title is None  # nothing written on refusal

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/pin

def test_session_pin_sets_and_clears_pinned_at(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1))
        result = _ok(await _call(log, "session/pin", {"session_id": "s1", "pinned": True}))
        assert isinstance(result["pinned_at"], int) and result["pinned_at"] > 0
        assert log.store.load("s1").pinned_at == result["pinned_at"]
        assert _meta_events(log, "s1")[0].payload["pinned_at"] == result["pinned_at"]

        result = _ok(await _call(log, "session/pin", {"session_id": "s1", "pinned": False}))
        assert result["pinned_at"] is None
        assert log.store.load("s1").pinned_at is None

    asyncio.run(scenario())


def test_session_pin_requires_a_bool_and_a_real_session(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1))
        for pinned in ("yes", 1, None):
            err = _err(await _call(log, "session/pin", {"session_id": "s1", "pinned": pinned}))
            assert err.code == "INVALID_ARGUMENT" and err.data["field"] == "pinned"
        assert _err(await _call(log, "session/pin", {"session_id": "s1"})).code == "INVALID_ARGUMENT"
        assert _err(await _call(log, "session/pin", {"session_id": "ghost", "pinned": True})).code == "NOT_FOUND"

    asyncio.run(scenario())


def test_pinned_ordering_dominates_updated_at(tmp_path):
    """MUTATION TARGET ②: deleting the `(pinned_at IS NULL)` sort clause must
    turn this red — pinned first is the FR-8.26 primary key, even when older."""
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s_old", created_at=1000))
        log.store.create_session(Session(id="s_new", created_at=9000))

        order = [r["session_id"] for r in _ok(await _call(log, "session/list", {}))["sessions"]]
        assert order == ["s_new", "s_old"]

        await _call(log, "session/pin", {"session_id": "s_old", "pinned": True})
        order = [r["session_id"] for r in _ok(await _call(log, "session/list", {}))["sessions"]]
        assert order == ["s_old", "s_new"], "pinned must beat a newer updated_at"

        await _call(log, "session/pin", {"session_id": "s_old", "pinned": False})
        order = [r["session_id"] for r in _ok(await _call(log, "session/list", {}))["sessions"]]
        assert order == ["s_new", "s_old"]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/archive

def test_session_archive_moves_in_and_out_of_the_default_list(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1))
        result = _ok(await _call(log, "session/archive", {"session_id": "s1", "archived": True}))
        assert isinstance(result["archived_at"], int)
        assert log.store.load("s1").archived_at == result["archived_at"]
        ids = [r["session_id"] for r in _ok(await _call(log, "session/list", {}))["sessions"]]
        assert ids == []  # out of the default list (FR-8.26 已归档筛选)
        ids = [r["session_id"] for r in
               _ok(await _call(log, "session/list", {"archived": True}))["sessions"]]
        assert ids == ["s1"]

        result = _ok(await _call(log, "session/archive", {"session_id": "s1", "archived": False}))
        assert result["archived_at"] is None
        ids = [r["session_id"] for r in _ok(await _call(log, "session/list", {}))["sessions"]]
        assert ids == ["s1"]

    asyncio.run(scenario())


def test_session_archive_requires_a_bool_and_a_real_session(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1))
        for archived in ("yes", 0, None):
            err = _err(await _call(log, "session/archive", {"session_id": "s1", "archived": archived}))
            assert err.code == "INVALID_ARGUMENT" and err.data["field"] == "archived"
        assert _err(await _call(log, "session/archive", {"session_id": "s1"})).code == "INVALID_ARGUMENT"
        assert _err(await _call(log, "session/archive",
                                {"session_id": "ghost", "archived": True})).code == "NOT_FOUND"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/delete

def test_session_delete_refuses_active_turns_then_hard_deletes(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.set_executor(GateExecutor())
        log.store.create_session(Session(id="s1", created_at=1))
        sink = CollectSink()
        await log.attach("s1", None, sink)
        turn, _ = await log.submit("s1", [{"type": "text", "text": "hi"}], "c1")

        err = _err(await _call(log, "session/delete", {"session_id": "s1"}))
        assert err.code == "INVALID_ARGUMENT" and err.data["reason"] == "turn_active"
        assert log.store.load("s1") is not None  # nothing deleted behind the guard

        stopped = await log.interrupt("s1", turn.id) or await log.cancel("s1", turn.id)
        assert stopped
        await log.wait_idle("s1")
        assert _ok(await _call(log, "session/delete", {"session_id": "s1"})) == {"ok": True}
        assert log.store.load("s1") is None
        for table in ("sessions", "turns", "messages", "events"):
            key = "id" if table == "sessions" else "session_id"
            n = log.store._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {key}=?", ("s1",)).fetchone()[0]
            assert n == 0, table

    asyncio.run(scenario())


def test_session_delete_announces_removal_to_other_devices(tmp_path):
    """§6.2: the removed trigger reaches every OTHER attached stream, never the
    dying one (its events table is about to be wiped)."""
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="victim", created_at=1))
        log.store.create_session(Session(id="watcher", created_at=2))
        victim_sink = CollectSink()
        watcher_sink = CollectSink()
        await log.attach("victim", None, victim_sink)
        await log.attach("watcher", None, watcher_sink)

        assert _ok(await _call(log, "session/delete", {"session_id": "victim"})) == {"ok": True}
        assert await wait_until(lambda: _list_changes(watcher_sink), 1.0)
        change = _list_changes(watcher_sink)[-1]
        assert change == {"session_id": "victim", "change": "removed", "by": "dev-01"}
        assert _list_changes(victim_sink) == []
        assert _err(await _call(log, "session/delete", {"session_id": "ghost"})).code == "NOT_FOUND"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/group.list

def test_group_list_orders_by_sort_order_then_creation(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        assert _ok(await _call(log, "session/group.list", {}))["groups"] == []

        log.store.create_group(SessionGroup(group_id="g-b", name="b", sort_order=1, created_at=20))
        log.store.create_group(SessionGroup(group_id="g-a", name="a", sort_order=1, created_at=10))
        log.store.create_group(SessionGroup(group_id="g-z", name="z", sort_order=5, created_at=1))
        log.store.create_session(Session(id="s1", created_at=1, group_id="g-a"))
        log.store.create_session(Session(id="s2", created_at=2, group_id="g-a"))

        groups = _ok(await _call(log, "session/group.list", {}))["groups"]
        assert [g["group_id"] for g in groups] == ["g-a", "g-b", "g-z"]
        assert [g["session_count"] for g in groups] == [2, 0, 0]
        assert set(groups[0]) == {"group_id", "name", "icon", "color", "sort_order",
                                  "created_at", "session_count"}

    asyncio.run(scenario())


def test_group_list_needs_no_params_and_survives_unknown_keys(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_group(SessionGroup(group_id="g1", name="w", created_at=1))
        assert _ok(await _call(log, "session/group.list", {}))["groups"][0]["group_id"] == "g1"
        assert _ok(await _call(log, "session/group.list", {"unexpected": 1}))["groups"][0]["name"] == "w"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/group.create

def test_group_create_accepts_full_shape_and_allocates_id(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        result = _ok(await _call(log, "session/group.create",
                                 {"name": "  work  ", "icon": "folder", "color": "#112233",
                                  "sort_order": 2}))
        group = result["group"]
        assert group["name"] == "work"  # trimmed (§7.3)
        assert (group["icon"], group["color"], group["sort_order"]) == ("folder", "#112233", 2)
        assert isinstance(group["group_id"], str) and len(group["group_id"]) == 32
        stored = log.store.get_group(group["group_id"])
        assert stored is not None and stored.name == "work"

    asyncio.run(scenario())


def test_group_create_rejects_bad_name_icon_color_order_and_nesting(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        for name in ("", "   ", "x" * 65, None, 5):
            err = _err(await _call(log, "session/group.create", {"name": name}))
            assert err.code == "INVALID_ARGUMENT" and err.data["field"] == "name"

        # MUTATION-adjacent guard: shape-valid but unknown icon is refused (§7.1)
        for icon in ("zz-not-an-icon", "1bad", "Chat", ""):
            err = _err(await _call(log, "session/group.create", {"name": "w", "icon": icon}))
            assert err.code == "INVALID_ARGUMENT" and err.data["field"] == "icon"
        err = _err(await _call(log, "session/group.create", {"name": "w", "icon": "a" * 65}))
        assert err.code == "INVALID_ARGUMENT"

        for color in ("#fff", "#GGHHII", "red", "4a90d9", "#4a90d90"):
            err = _err(await _call(log, "session/group.create", {"name": "w", "color": color}))
            assert err.code == "INVALID_ARGUMENT" and err.data["field"] == "color"

        for order in (-1, "1", 1.5, True):
            err = _err(await _call(log, "session/group.create", {"name": "w", "sort_order": order}))
            assert err.code == "INVALID_ARGUMENT"

        err = _err(await _call(log, "session/group.create",
                               {"name": "sub", "parent_id": "g1"}))
        assert err.code == "INVALID_ARGUMENT" and err.data["reason"] == "nesting_forbidden"  # §7.3

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/group.update

def test_group_update_is_partial_and_can_clear_decorations(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        group = log.create_group("work", icon="folder", color="#112233", sort_order=1)

        result = _ok(await _call(log, "session/group.update",
                                 {"group_id": group.group_id, "name": "renamed",
                                  "icon": None, "sort_order": 7}))
        # name + sort_order changed; icon cleared; color UNTOUCHED (omitted = keep)
        assert (result["group"]["name"], result["group"]["icon"],
                result["group"]["color"], result["group"]["sort_order"]) == \
            ("renamed", None, "#112233", 7)

    asyncio.run(scenario())


def test_group_update_rejects_empty_name_null_unknown_group_and_empty_fields(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        group = log.create_group("work")
        for fields in ({"name": ""}, {"name": None}, {"name": "x" * 65}):
            err = _err(await _call(log, "session/group.update",
                                   {"group_id": group.group_id, **fields}))
            assert err.code == "INVALID_ARGUMENT"
        assert _err(await _call(log, "session/group.update",
                                {"group_id": group.group_id})).code == "INVALID_ARGUMENT"
        assert _err(await _call(log, "session/group.update",
                                {"group_id": "ghost", "name": "x"})).code == "NOT_FOUND"
        assert log.store.get_group(group.group_id).name == "work"  # nothing applied

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/group.delete

def test_group_delete_keeps_sessions_and_unassigns_them(tmp_path):
    """MUTATION TARGET ①: a delete_group that deletes member sessions (or skips
    the unassign UPDATE) must turn this red — §8.1 is the contract."""
    async def scenario():
        log = _log(tmp_path)
        group = log.create_group("work")
        log.store.create_session(Session(id="s1", created_at=1, group_id=group.group_id))
        log.store.create_session(Session(id="s2", created_at=2, group_id=group.group_id))
        log.store.create_session(Session(id="s3", created_at=3))  # never a member

        result = _ok(await _call(log, "session/group.delete", {"group_id": group.group_id}))
        assert result == {"ok": True, "unassigned": 2}
        assert log.store.get_group(group.group_id) is None

        rows = {r["session_id"]: r for r in _ok(await _call(log, "session/list", {}))["sessions"]}
        assert set(rows) == {"s1", "s2", "s3"}, "deleting a folder must never delete conversations"
        assert rows["s1"]["group_id"] is None and rows["s2"]["group_id"] is None
        assert log.store.load("s1") is not None and log.store.load("s2") is not None
        assert log.store.load("s1").messages == () and log.store.load("s1").turns == ()

        assert _err(await _call(log, "session/group.delete",
                                {"group_id": group.group_id})).code == "NOT_FOUND"
        assert _err(await _call(log, "session/group.delete", {"group_id": ""})).code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_group_delete_unknown_group_is_not_found(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        assert _err(await _call(log, "session/group.delete", {"group_id": "ghost"})).code == "NOT_FOUND"
        assert _err(await _call(log, "session/group.delete", {})).code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- session/group.assign

def test_group_assign_moves_sessions_both_ways_and_emits(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        group = log.create_group("work")
        log.store.create_session(Session(id="s1", created_at=1))

        result = _ok(await _call(log, "session/group.assign",
                                 {"session_id": "s1", "group_id": group.group_id}))
        assert result == {"ok": True, "group_id": group.group_id}
        assert log.store.load("s1").group_id == group.group_id
        events = _meta_events(log, "s1")
        assert events and events[0].payload["group_id"] == group.group_id

        result = _ok(await _call(log, "session/group.assign",
                                 {"session_id": "s1", "group_id": None}))
        assert result["group_id"] is None
        assert log.store.load("s1").group_id is None
        assert len(_meta_events(log, "s1")) == 2

    asyncio.run(scenario())


def test_group_assign_validates_group_and_session(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1))
        group = log.create_group("work")
        for bad in ("ghost", "", 5):
            err = _err(await _call(log, "session/group.assign",
                                   {"session_id": "s1", "group_id": bad}))
            assert err.code in ("NOT_FOUND", "INVALID_ARGUMENT")
        assert _err(await _call(log, "session/group.assign",
                                {"session_id": "ghost", "group_id": group.group_id})).code == "NOT_FOUND"
        assert _err(await _call(log, "session/group.assign", {"session_id": "s1"})).code == "INVALID_ARGUMENT"
        assert log.store.load("s1").group_id is None  # nothing applied

    asyncio.run(scenario())


# --------------------------------------------------------------------------- events + wiring

def test_attach_auto_create_fans_out_added_to_other_devices(tmp_path):
    """§6.2: a new session reaches the lists of devices that are attached."""
    async def scenario():
        log = _log(tmp_path)
        log.store.create_session(Session(id="first", created_at=1))
        first_sink = CollectSink()
        await log.attach("first", None, first_sink)

        second_sink = CollectSink()
        await log.attach(None, None, second_sink)  # auto-create
        assert await wait_until(lambda: _list_changes(first_sink), 1.0)
        change = _list_changes(first_sink)[-1]
        assert change["change"] == "added" and change["by"] == "system"
        assert isinstance(change["session_id"], str) and change["session_id"]
        assert _list_changes(second_sink) == []  # its own stream is skipped

    asyncio.run(scenario())


def test_events_schema_declares_both_metadata_events():
    data = json.loads(SCHEMA_EVENTS.read_text(encoding="utf-8"))
    assert data["version"] == "2.0.0"  # §6: no enum bump, top-level untouched
    updated = data["events"]["session.updated"]
    changed = data["events"]["session.list_changed"]
    assert updated["required"] == ["session_id", "by"]
    assert changed["required"] == ["session_id", "change", "by"]
    for spec in (updated, changed):
        assert "state" not in spec["payload"]  # metadata edge, not a turn state
        assert (spec.get("x-vade") or {}).get("push") is None  # not a C-14 topic
    assert updated["payload"]["pinned_at"] == "int|null (unix ms)"
    assert changed["payload"]["change"] == "added|removed"
    blocks = [b for b in data["x-vade"]["appends"] if "RFC-0010" in b["doc"]]
    assert len(blocks) == 1 and set(blocks[0]["adds"]) == {"session.updated", "session.list_changed"}


def test_rpc_schema_and_handlers_agree_on_the_ten_methods():
    rpc_data = json.loads(SCHEMA_RPC.read_text(encoding="utf-8"))
    # The pin tracks the current C-05 head: 1.3.0 was RFC-0010's append;
    # RFC-0012 (subagent/list|get) owned 1.4.0; RFC-0014 (session/search)
    # owned 1.5.0; RFC-0015 (ui/modules) owned 1.6.0; RFC-0016 (ui/blob.token)
    # owned 1.7.0; the f15 snapshot contract owns 1.8.0. The TEN_METHODS
    # assertions below are the real subject of this test and did not move.
    assert rpc_data["version"] == "1.8.0"
    ui = rpc_data["directions"]["ui_to_core"]
    registered = set(SessionRPC(None).handlers())
    for name in TEN_METHODS:
        assert name in ui, name
        assert name in registered, name
        for field in ("params", "result", "errors"):
            assert field in ui[name], (name, field)
    blocks = [b for b in rpc_data["x-vade"]["appends"] if "RFC-0010" in b["doc"]]
    assert len(blocks) == 1 and set(blocks[0]["adds"]) == TEN_METHODS


def test_session_updated_emission_survives_reconnect_replay(tmp_path):
    """§6.1 + persist-before-broadcast: the metadata event is on the seq bus."""
    async def scenario():
        path = tmp_path / "sessions.db"
        log = _log(tmp_path)
        log.store.create_session(Session(id="s1", created_at=1))
        await log.rename_session("s1", "renamed", by="dev-01")

        reopened = SessionLog(SQLiteStore(path), replay=ReplayBuffer(maxlen=500))
        events = reopened.store.read_since("s1", 0)
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        updated = [e for e in events if e.type == "session.updated"]
        assert len(updated) == 1
        assert updated[0].payload["title"] == "renamed"

    asyncio.run(scenario())


def test_group_wire_icons_and_colors_share_the_session_whitelist(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        group = _ok(await _call(log, "session/group.create",
                                {"name": "w", "icon": "terminal-2", "color": "#AbCdEf"}))["group"]
        assert group["icon"] == "terminal-2" and group["color"] == "#AbCdEf"
        # the same whitelist gates session icons through group decoration paths
        err = _err(await _call(log, "session/group.create",
                               {"name": "w2", "icon": "folder ", "color": None}))
        assert err.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())
