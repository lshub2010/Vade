"""RFC-0013 (R9 / FR-8.35): the interrupted answer is PERSISTED and visible to
a fresh attach on ANOTHER device.

`test_interrupt_partial.py` locks the storage path on the same log; this file
locks the contract surface RFC-0013 freezes:

* A1 — a brand-new `SessionLog` (another device, snapshot path, no delta
  replay) sees the partial text. This is the FR-8.35 acceptance sentence.
* A2 — the production `SessionRPC.ui_attach` result carries `partial` on every
  message row, true only for the persisted partial answer.
* A3 — `turn.interrupted.partial_chars == len(partial message content) == len(
  joined deltas)`, three-way.
* A4 — completed / failed turns never produce a partial message.
* A5 — the partial message survives a store reopen (a column read-back, not a
  memory object).
* A6 — event payload, turn row and snapshot agree on the `msg:<hex>` pointer.
* A7 — §7: regenerating the interrupted turn supersedes it and keeps the
  partial message visible on the old (now greyed) turn.
* A8 — the contract text: C-06 no longer claims "only from turn.delta", C-05
  documents `snapshot.messages[].partial`, both via RFC-0013 append blocks.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.models import Message
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore

from .helpers import CollectSink, wait_until

SCHEMA_EVENTS = Path(__file__).resolve().parents[3] / "schema" / "events.json"
SCHEMA_RPC = Path(__file__).resolve().parents[3] / "schema" / "rpc.json"


class _Conn:
    device_id = "dev-other"
    connection_id = "conn-other"


def _store(path) -> SQLiteStore:
    return SQLiteStore(path)


def _log(store: SQLiteStore, executor) -> SessionLog:
    log = SessionLog(store, replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    return log


def _interrupted(events):
    return [e for e in events if e.type == "turn.interrupted"]


def _partial_messages(snapshot) -> list[dict]:
    return [m for m in snapshot["messages"] if m.get("partial") is True]


def test_fr835_a_fresh_attach_on_a_second_log_sees_the_partial_text(tmp_path):
    """A1 — THE FR-8.35 acceptance sentence: interrupt here, attach there.

    Device A streams and is interrupted; device B is a brand-new SessionLog
    over the same database that attaches with last_seq=None (the snapshot
    path — it never replays a single turn.delta) and must still see the N
    characters that were already generated.
    """

    async def scenario():
        started = asyncio.Event()
        streamed = ["你好，", "世界！", "还没写完。"]

        async def executor(turn, content):
            started.set()
            for chunk in streamed:
                await log_a.emit(turn.session_id, "turn.delta",
                                 {"turn_id": turn.id, "text": chunk})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="".join(streamed))

        path = tmp_path / "sessions.db"
        log_a = _log(_store(path), executor)
        sess = log_a.create_session()
        turn, _ = await log_a.submit(sess.id, [{"type": "text", "text": "写个故事"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        assert await wait_until(lambda: log_a.partial_text(turn.id) == "".join(streamed), 2.0)
        assert await log_a.interrupt(sess.id, turn.id) is True

        # Device B: another process worth of state — new SQLiteStore connection,
        # new SessionLog, fresh attach (snapshot, zero delta replay).
        log_b = SessionLog(_store(path), replay=ReplayBuffer(maxlen=500))
        result = await log_b.attach(sess.id, None, CollectSink())
        assert result.snapshot is not None
        partials = _partial_messages(result.snapshot)
        assert len(partials) == 1
        bubble = partials[0]
        assert bubble["role"] == "assistant"
        assert bubble["partial"] is True
        assert bubble["content"] == "你好，世界！还没写完。"  # N 字仍可见，逐字一致
        assert bubble["turn_id"] == turn.id

        row = next(t for t in result.snapshot["turns"] if t["id"] == turn.id)
        assert row["state"] == "interrupted"
        assert row["partial_message_id"] == "msg:" + bubble["id"]

        await log_b.close()
        await log_a.close()

    asyncio.run(scenario())


def test_rpc_ui_attach_snapshot_carries_the_partial_flag(tmp_path):
    """A2 — the production RPC layer: every snapshot message carries `partial`;
    exactly the persisted partial answer is true."""

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "ab"})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="ab")

        log = _log(_store(tmp_path / "sessions.db"), executor)
        rpc = SessionRPC(log)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)

        result = await rpc.dispatch(_Conn(), "ui/attach",
                                    {"session_id": sess.id, "last_seq": None})
        assert isinstance(result, dict)
        assert set(result) == {"session_id", "from_seq", "snapshot"}
        messages = result["snapshot"]["messages"]
        assert messages, "snapshot must carry the conversation"
        # C-05 RFC-0013 append: `partial` is a documented key on EVERY row.
        assert all("partial" in m for m in messages)
        partials = [m for m in messages if m["partial"] is True]
        assert len(partials) == 1
        assert partials[0]["role"] == "assistant"
        assert partials[0]["content"] == "ab"
        assert [m["partial"] for m in messages if m["role"] == "user"] == [False]
        await log.close()

    asyncio.run(scenario())


def test_partial_chars_equals_the_persisted_content_length(tmp_path):
    """A3 — event, stored message and joined deltas agree on the character count."""

    async def scenario():
        started = asyncio.Event()
        chunks = ["Hel", "lo, ", "世界", "！"]

        async def executor(turn, content):
            started.set()
            for chunk in chunks:
                await log.emit(turn.session_id, "turn.delta",
                               {"turn_id": turn.id, "text": chunk})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="".join(chunks))

        log = _log(_store(tmp_path / "sessions.db"), executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)

        payload = _interrupted(log.store.read_since(sess.id, 0))[0].payload
        snapshot = log.snapshot(sess.id)
        bubble = _partial_messages(snapshot)[0]
        joined = "".join(chunks)
        assert payload["partial_chars"] == len(bubble["content"]) == len(joined)
        assert bubble["content"] == joined
        await log.close()

    asyncio.run(scenario())


def test_a_completed_turn_never_produces_a_partial_message(tmp_path):
    """A4a — a turn that finishes normally writes no partial row, no pointer."""

    async def scenario():
        log = _log(_store(tmp_path / "sessions.db"), None)

        async def executor(turn, content):
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "全文"})
            return TurnOutcome(assistant_text="全文", stopped_reason="stop")

        log.set_executor(executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        await log.wait_idle(sess.id)

        snapshot = log.snapshot(sess.id)
        assert [m["role"] for m in snapshot["messages"] if m["role"] == "assistant"] == ["assistant"]
        assert _partial_messages(snapshot) == []
        assert all(m["partial"] is False for m in snapshot["messages"])
        row = next(t for t in snapshot["turns"] if t["id"] == turn.id)
        assert row["state"] == "completed"
        assert row["partial_message_id"] is None
        await log.close()

    asyncio.run(scenario())


def test_a_failed_turn_never_produces_a_partial_message(tmp_path):
    """A4b — deltas streamed then the executor raises: turn.failed, no bubble.

    Only the interrupt path writes partial text (RFC-0006 §4.2); a failure must
    not silently promote half an answer into a message.
    """

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "半截"})
            raise RuntimeError("module exploded")

        log = _log(_store(tmp_path / "sessions.db"), executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.wait_idle(sess.id)

        types = [e.type for e in log.store.read_since(sess.id, 0)]
        assert "turn.failed" in types and "turn.interrupted" not in types
        snapshot = log.snapshot(sess.id)
        assert _partial_messages(snapshot) == []
        row = next(t for t in snapshot["turns"] if t["id"] == turn.id)
        assert row["state"] == "failed"
        assert row["partial_message_id"] is None
        await log.close()

    asyncio.run(scenario())


def test_the_partial_message_survives_a_store_reopen(tmp_path):
    """A5 — persisted, not memory: close the database, reopen it, read it back."""

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "落库"})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="落库")

        path = tmp_path / "sessions.db"
        log = _log(_store(path), executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)
        pointer = _interrupted(log.store.read_since(sess.id, 0))[0].payload["partial_message_id"]
        await log.close()

        reopened = SQLiteStore(path)  # fresh connection, same file
        session = reopened.load(sess.id)
        partials = [m for m in session.messages if m.partial]
        assert len(partials) == 1
        assert partials[0].content == "落库"
        assert partials[0].role == "assistant"
        assert "msg:" + partials[0].id == pointer
        reopened.close()

    asyncio.run(scenario())


def test_the_turn_row_pointer_matches_the_partial_message(tmp_path):
    """A6 — event payload, turn row and snapshot name the same `msg:<hex>` id."""

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "xyz"})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="xyz")

        log = _log(_store(tmp_path / "sessions.db"), executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)

        from_event = _interrupted(log.store.read_since(sess.id, 0))[0].payload["partial_message_id"]
        session = log.store.load(sess.id)
        from_row = next(t for t in session.turns if t.id == turn.id).partial_message_id
        snapshot = log.snapshot(sess.id)
        from_snapshot = next(t for t in snapshot["turns"] if t["id"] == turn.id)["partial_message_id"]
        assert from_event == from_row == from_snapshot
        assert from_event is not None and from_event.startswith("msg:")
        bubble = _partial_messages(snapshot)[0]
        assert from_event == "msg:" + bubble["id"]
        await log.close()

    asyncio.run(scenario())


def test_regenerating_an_interrupted_turn_keeps_the_partial_visible(tmp_path):
    """A7 — §7: regenerate supersedes the interrupted turn; the partial answer
    stays on the greyed-out old turn and is not copied to the new one."""

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            if turn.parent_turn_id is not None:
                return TurnOutcome(assistant_text="重新生成的回答")  # the v2 turn
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "半截"})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="半截")

        log = _log(_store(tmp_path / "sessions.db"), executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        assert await log.interrupt(sess.id, turn.id) is True

        result = await log.regenerate(sess.id, turn.id, "c1-regen")
        await log.wait_idle(sess.id)

        snapshot = log.snapshot(sess.id)
        old = next(t for t in snapshot["turns"] if t["id"] == turn.id)
        new = next(t for t in snapshot["turns"] if t["id"] == result["turn_id"])
        assert old["superseded_by"] == result["turn_id"]
        assert old["state"] == "interrupted"
        assert old["partial_message_id"] is not None
        assert new["parent_turn_id"] == turn.id and new["state"] == "completed"

        # Exactly ONE partial bubble, still bound to the superseded turn.
        partials = _partial_messages(snapshot)
        assert len(partials) == 1
        assert partials[0]["turn_id"] == turn.id
        assert partials[0]["content"] == "半截"
        v2_assistant = [m for m in snapshot["messages"]
                        if m["role"] == "assistant" and m["turn_id"] == new["id"]]
        assert v2_assistant and all(m["partial"] is False for m in v2_assistant)
        await log.close()

    asyncio.run(scenario())


def test_a_partial_bubble_lands_only_on_the_interrupted_turn(tmp_path):
    """Regression guard: two turns — one completes, one is interrupted. The
    partial flag appears exactly once, on the interrupted turn only."""

    async def scenario():
        started = asyncio.Event()
        which = {"first": True}

        async def executor(turn, content):
            if which["first"]:
                which["first"] = False
                return TurnOutcome(assistant_text="第一回合")
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "part"})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="part")

        log = _log(_store(tmp_path / "sessions.db"), executor)
        sess = log.create_session()
        turn1, _ = await log.submit(sess.id, [{"type": "text", "text": "q1"}], "c1")
        await log.wait_idle(sess.id)
        turn2, _ = await log.submit(sess.id, [{"type": "text", "text": "q2"}], "c2")
        assert await wait_until(lambda: started.is_set(), 2.0)
        assert await log.interrupt(sess.id, turn2.id) is True

        snapshot = log.snapshot(sess.id)
        partials = _partial_messages(snapshot)
        assert len(partials) == 1 and partials[0]["turn_id"] == turn2.id
        by_turn = {t["id"]: t for t in snapshot["turns"]}
        assert by_turn[turn1.id]["partial_message_id"] is None
        assert by_turn[turn2.id]["partial_message_id"] is not None
        t1_assistant = [m for m in snapshot["messages"]
                        if m["role"] == "assistant" and m["turn_id"] == turn1.id]
        assert t1_assistant and all(m["partial"] is False for m in t1_assistant)
        await log.close()

    asyncio.run(scenario())


def test_ordinary_messages_default_partial_to_false():
    """A2's default half: the constructor and the wire projection agree that a
    message is not partial unless the interrupt path says so."""
    plain = Message(id="m1", session_id="s", role="assistant", content="hi")
    assert plain.partial is False
    assert plain.to_dict()["partial"] is False
    user = Message(id="m2", session_id="s", role="user", content="q")
    assert user.to_dict()["partial"] is False


def test_attach_replay_path_still_carries_the_pointer(tmp_path):
    """A2's replay half: a client inside the replay window gets the
    `turn.interrupted` pointer event exactly once — id-based dedupe (RFC-0006
    §4.4) has the same key on both attach paths."""

    async def scenario():
        started = asyncio.Event()

        async def executor(turn, content):
            started.set()
            await log.emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": "ab"})
            await asyncio.sleep(30)
            return TurnOutcome(assistant_text="ab")

        log = _log(_store(tmp_path / "sessions.db"), executor)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: started.is_set(), 2.0)
        await log.interrupt(sess.id, turn.id)

        sink = CollectSink()
        result = await log.attach(sess.id, 0, sink)
        assert result.snapshot is None  # replay path, not a second snapshot
        interrupted = [e for e in result.replay if e.type == "turn.interrupted"]
        assert len(interrupted) == 1
        payload = interrupted[0].payload
        assert payload["partial_message_id"] is not None
        assert payload["partial_chars"] == 2
        assert payload["partial_message_id"].startswith("msg:")
        # The stream never carries the text itself — the message row does.
        assert "text" not in payload
        await log.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------------- A8: contract text

def test_events_json_no_longer_claims_delta_is_the_only_source():
    """A8a — C-06 notes tell the storage truth now; the RFC-0013 append is
    registered; the RFC-0006 block and the frozen top-level version are intact."""
    data = json.loads(SCHEMA_EVENTS.read_text(encoding="utf-8"))
    notes = data["events"]["turn.interrupted"]["notes"]
    assert "only from turn.delta" not in notes
    assert "reconstructable only" not in notes
    assert "persisted as a `partial=true` assistant message" in notes
    assert "ui/attach" in notes and "FR-8.35" in notes
    # The pre-existing sentences survive byte-for-byte.
    assert notes.startswith("Emitted after tools/cancel is fired; core does not wait for the module.")
    assert 'usually `source="none"` with zero counts' in notes
    blocks = [b for b in data["x-vade"]["appends"] if "RFC-0013" in str(b.get("doc"))]
    assert len(blocks) == 1
    block = blocks[0]
    assert block["contract"] == "C-06" and block["version"] == "1.2.0"
    assert block["adds"] == [] and block["fields"] == {}
    assert any("only from turn.delta" in o for o in block["overrides"])
    # Frozen history untouched.
    assert data["version"] == "2.0.0"
    rfc0006 = data["x-vade"]["appends"][0]
    assert rfc0006["rewrites"] == "none" and rfc0006["version"] == "1.1.0"


def test_rpc_json_documents_the_partial_field():
    """A8b — C-05 declares `snapshot.messages[].partial`; the RFC-0013 C-05
    append carries 1.2.0; the frozen top-level version is intact."""
    data = json.loads(SCHEMA_RPC.read_text(encoding="utf-8"))
    notes = data["directions"]["ui_to_core"]["ui/attach"]["notes"]
    assert "`snapshot.messages[]` carries `partial` (bool, default false)" in notes
    assert "RFC-0013" in notes and "FR-8.35" in notes
    assert "without replaying `turn.delta`" in notes
    blocks = [b for b in data["x-vade"]["appends"] if "RFC-0013" in str(b.get("doc"))]
    assert len(blocks) == 1
    block = blocks[0]
    assert block["contract"] == "C-05" and block["version"] == "1.2.0"
    assert block["adds"] == [] and "none" in block["rewrites"]
    # Frozen history untouched.
    # Frozen history untouched: RFC-0013 itself never bumps the top-level version.
    # 1.3.0 was RFC-0010's bump; RFC-0012 then appended subagent/list|get and owns
    # 1.4.0; RFC-0014 (session/search) owned 1.5.0; RFC-0015 (ui/modules) owned
    # 1.6.0; RFC-0016 (ui/blob.token) owned 1.7.0; the f15 snapshot contract owns
    # 1.8.0. Assert against whoever last
    # appended, so this test keeps guarding
    # the real invariant ("RFC-0013 adds no top-level bump") instead of a stale
    # number.
    assert data["version"] == "1.8.0"
    assert data["x-vade"]["appends"][0]["rewrites"] == "none"
