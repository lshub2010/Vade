"""`ui/attach` bounds: both ends of the cursor, and both ends of the frame.

Four defects from `batch-results/f9-audit-core-attach.md` share one shape — the
caller believes it has the whole session and the server silently says less:

  P1-1  `last_seq > newest` was "covered": `snapshot=null`, zero events, and a
        `from_seq` the client can never satisfy. History gone, no error.
  P1-2  an empty replay buffer meant "brand-new session", so after a restart
        `attach(last_seq=0)` replayed events as if it were a snapshot — every
        state item persisted WITHOUT an event (`session/model.set`) was lost.
  P0-1  `message.content` + `parts[].text` + `turn.user_text` carried the same
        text three times, so a legal 32 000-character message made the session
        permanently unopenable (PAYLOAD_TOO_LARGE on every attach).
  P1-3  a snapshot taken mid-turn had no assistant text: the deltas already
        streamed lived only in `turn.delta` events, and `turn.completed`
        carries no body — that answer stayed half-rendered forever.

Every test drives the real `SessionLog` + real `SQLiteStore`; the size checks
use `Connection._encode_json`, the same encoder `_send_result` measures with.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from vade.session.manager import SNAPSHOT_ENVELOPE_RESERVE, SessionLog
from vade.session.models import Message
from vade.session.replay import ReplayBuffer
from vade.session.rpc import SessionRPC
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import MAX_REPLY_BYTES, _encode_json
from .helpers import CollectSink, GateExecutor, wait_until


class Audit:
    """The two methods `SessionLog.audit` is allowed to call."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, event: dict[str, Any]) -> None:
        self.rows.append(dict(event))


class Conn:
    """A C2 connection stand-in for `SessionRPC.ui_attach`'s one sink."""

    def __init__(self) -> None:
        self.connection_id = "conn-1"
        self.device_id = "dev-01"
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def _log(tmp_path, **kwargs) -> SessionLog:
    async def executor(turn, content):
        from vade.loop.engine import TurnOutcome

        return TurnOutcome(assistant_text=f"echo:{turn.user_text}", stopped_reason="stop")

    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"),
                     replay=kwargs.pop("replay", ReplayBuffer(maxlen=500)), **kwargs)
    log.set_executor(executor)
    return log


def _rpc(log: SessionLog) -> SessionRPC:
    return SessionRPC(log)


async def _write_message(log: SessionLog, session_id: str, role: str, content: str,
                         turn_id: str | None = None) -> Message:
    """Persist one message through a real emit (so it gets a real seq)."""
    message = Message(id=f"m-{len(content)}-{role}-{turn_id or ''}", session_id=session_id,
                      role=role, content=content, turn_id=turn_id)
    await log.emit(session_id, "session.updated", {"session_id": session_id},
                   messages=[message])
    return message


def _reply_size(session_id: str, result: dict[str, Any], *, reply_id: Any = 1) -> int:
    """The exact bytes `Connection._send_result` would seal for this reply."""
    envelope = {
        "jsonrpc": "2.0",
        "id": reply_id,
        "result": {
            "session_id": session_id,
            "from_seq": result["from_seq"],
            "snapshot": result["snapshot"],
        },
    }
    return len(_encode_json(envelope))


# --------------------------------------------------------------- P1-1: upper bound
def test_last_seq_beyond_the_newest_event_is_answered_with_a_snapshot(tmp_path):
    """A cursor the session never reached is unverifiable, not "caught up".

    The old `covers()` only looked at `oldest`, so `last_seq=999` on a session
    with seq 1..3 answered `snapshot: null`, `replay_count: 0` and
    `from_seq: 1000`: the client kept a cursor it could never satisfy and the
    three events it did not have were silently declared present.
    """

    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        for index in range(3):
            await log.emit(session.id, "turn.delta", {"turn_id": "t", "text": str(index)})
        assert log.store.last_seq(session.id) == 3

        result = await log.attach(session.id, 999, CollectSink())

        assert result.snapshot is not None, "a cursor ahead of the stream must re-sync"
        assert result.replay == ()
        assert result.from_seq == result.snapshot["from_seq"] == 3
        assert [m["content"] for m in result.snapshot["messages"]] == []
        assert result.snapshot["queue"] == {"running": None, "pending": []}
        await log.close()

    asyncio.run(scenario())


def test_last_seq_equal_to_the_newest_event_is_still_covered(tmp_path):
    """The normal "I am caught up" case must not start snapshotting."""
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        for index in range(3):
            await log.emit(session.id, "turn.delta", {"turn_id": "t", "text": str(index)})
        result = await log.attach(session.id, 3, CollectSink())
        assert result.snapshot is None
        assert result.replay == ()
        await log.close()

    asyncio.run(scenario())


def test_a_coalescible_tail_does_not_force_a_snapshot(tmp_path):
    """`last_seq == store.last_seq` must stay covered when the tail coalesces.

    Coalescible rows (`task.progress`) never enter the replay window, so the
    buffer's own newest is older than the store's. The upper bound therefore
    comes from the store, not from `buf[-1]`, or every reconnect after a task
    update would pay a snapshot.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        await log.emit(session.id, "turn.delta", {"turn_id": "t", "text": "a"})
        await log.emit(session.id, "task.progress", {"task_id": "x", "percent": 10},
                       coalesce=True)
        assert log.replay.newest(session.id) == 1
        assert log.store.last_seq(session.id) == 2
        result = await log.attach(session.id, 2, CollectSink())
        assert result.snapshot is None, "a coalescible tail must not look like a gap"
        await log.close()

    asyncio.run(scenario())


# --------------------------------------------------------------- P1-2: empty buffer
def test_restart_empty_buffer_with_last_seq_zero_answers_with_a_snapshot(tmp_path):
    """`last_seq=0` after a restart is not "brand-new session".

    `session/model.set` writes a column and emits nothing, so a replay-only
    attach can never carry it. `snapshot=null` therefore meant the override was
    invisible to every client until it happened to call `session/model.get`.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        await log.emit(session.id, "turn.delta", {"turn_id": "t", "text": "a"})
        log.set_session_model(session.id, "openai/gpt-4o")
        await log.close()

        # A new process: fresh ReplayBuffer over the same database.
        reborn = _log(tmp_path)
        result = await reborn.attach(session.id, 0, CollectSink())

        assert result.snapshot is not None, "an empty buffer proves nothing after a restart"
        assert result.replay == ()
        assert result.snapshot["session"]["model"]["model_id"] == "openai/gpt-4o"
        assert result.from_seq == 1
        await reborn.close()

    asyncio.run(scenario())


def test_a_brand_new_empty_session_also_answers_with_a_snapshot(tmp_path):
    """The other side of the same coin: nothing persisted is still not proof.

    A session created in this process has no events, so a replay would be empty
    either way; the snapshot is the answer that cannot be wrong (it also covers
    a model set between create and attach).
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        result = await log.attach(session.id, 0, CollectSink())
        assert result.snapshot is not None
        assert result.replay == ()
        await log.close()

    asyncio.run(scenario())


def test_an_in_process_replay_at_zero_still_works(tmp_path):
    """The regression guard: a live buffer must keep serving `last_seq=0`."""
    async def scenario():
        log = _log(tmp_path, sink_queue=16)
        session = log.create_session()
        for index in range(4):
            await log.emit(session.id, "queue.updated", {"n": index})
        result = await log.attach(session.id, 0, CollectSink())
        assert result.snapshot is None
        assert len(result.replay) == 4
        await log.close()

    asyncio.run(scenario())


# ----------------------------------------------------------------- P0-1: the frame
def test_a_long_session_attaches_with_a_truncated_snapshot(tmp_path):
    """A legal-sized session must never be permanently unopenable.

    120 messages x 1 500 ASCII characters is ~180 KB of `content` alone — three
    times the frame when `content`, `parts[].text` and `user_text` all carry it.
    The answer is the recent tail plus `truncated/omitted/
    truncated_before_seq`, and an audit row; never PAYLOAD_TOO_LARGE.
    """
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()
        for index in range(120):
            await _write_message(log, session.id, "user", f"{index:04d}-" + "x" * 1500,
                                 turn_id=f"turn-{index}")
        rpc = _rpc(log)
        conn = Conn()

        result = await rpc.ui_attach(conn, {"session_id": session.id, "last_seq": None})

        snapshot = result["snapshot"]
        assert snapshot is not None
        assert snapshot["truncated"] is True
        assert snapshot["omitted"] > 0
        assert snapshot["messages"], "the recent tail is the part worth keeping"
        assert snapshot["truncated_before_seq"] == snapshot["messages"][0]["seq"]
        assert snapshot["messages"][-1]["content"] == "0119-" + "x" * 1500
        # Every dropped row is accounted for, and the shape is stable.
        assert snapshot["omitted"] + len(snapshot["messages"]) == 120
        assert snapshot["content_truncated"] is False

        # The exact bytes `_send_result` would judge, with a long JSON-RPC id.
        size = _reply_size(session.id, result, reply_id="a" * 36)
        assert size <= MAX_REPLY_BYTES, f"{size} bytes would be refused"
        assert size <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE

        # Truncation is audible, not silent.
        assert [row["kind"] for row in audit.rows] == ["session.snapshot_truncated"]
        assert audit.rows[0]["session_id"] == session.id
        assert audit.rows[0]["extra"]["omitted"] == snapshot["omitted"]
        await log.close()

    asyncio.run(scenario())


def test_one_oversized_message_is_clipped_not_refused(tmp_path):
    """A single message can exceed the whole budget (RFC-0006 allows 32 000 cps).

    32 000 CJK characters is ~96 KB of UTF-8 — larger than the entire frame.
    Dropping every other row does not help, so the row itself is clipped and
    marked; the attach still succeeds.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        text = "字" * 32_000
        await _write_message(log, session.id, "user", text, turn_id="turn-big")
        rpc = _rpc(log)
        result = await rpc.ui_attach(Conn(), {"session_id": session.id, "last_seq": None})
        snapshot = result["snapshot"]

        assert snapshot["content_truncated"] is True
        assert snapshot["truncated"] is True
        [message] = snapshot["messages"]
        assert message["truncated"] is True
        assert message["omitted_chars"] > 0
        assert message["content"] and len(message["content"]) < len(text)
        assert text.startswith(message["content"]), "a completed row keeps its head"
        assert _reply_size(session.id, result, reply_id="a" * 36) <= MAX_REPLY_BYTES
        await log.close()

    asyncio.run(scenario())


def test_the_snapshot_carries_each_text_once(tmp_path):
    """`content` xor `parts` per message; no `user_text` on the turn."""
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        await log.submit(session.id, [{"type": "text", "text": "hello"}], "c1")
        await log.wait_idle(session.id)

        snapshot = log.snapshot(session.id)
        user = next(m for m in snapshot["messages"] if m["role"] == "user")
        assert user["content"] == "hello"
        assert "parts" not in user, "a text-only parts list is the projection of content"
        assert all("user_text" not in turn for turn in snapshot["turns"])
        # The in-process shape still has both, for callers that want them.
        assert log.get_session(session.id).message_tree()[0]["parts"] == [
            {"type": "text", "text": "hello"}]
        assert log.get_session(session.id).turns[0].to_dict()["user_text"] == "hello"
        await log.close()

    asyncio.run(scenario())


# ------------------------------------------------------------- P1-3: running partial
def test_a_mid_turn_snapshot_carries_the_streamed_partial(tmp_path):
    """The already-streamed assistant text is in the snapshot, not only in events.

    Without it a client that attaches mid-turn gets the user message and
    `queue.running`, then only the SECOND half of the deltas: the answer stays
    half-rendered forever, because `turn.completed` carries no body.
    """
    async def scenario():
        gate = GateExecutor()
        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"),
                         replay=ReplayBuffer(maxlen=500))
        log.set_executor(gate)
        session = log.create_session()
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "c1")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id)

        for chunk in ("这是", "已经", "生成的前半段"):
            await log.emit(session.id, "turn.delta", {"turn_id": turn.id, "text": chunk})

        snapshot = log.snapshot(session.id)
        partial = snapshot["running_partial"]
        assert partial is not None
        assert partial["turn_id"] == turn.id
        assert partial["text"] == "这是已经生成的前半段"
        assert partial["chars"] == len(partial["text"])
        assert "truncated" not in partial
        # The row really is not persisted yet: the partial is the ONLY copy.
        assert [m["content"] for m in snapshot["messages"]] == ["hi"]

        # And the attach answer carries it, not just the internal snapshot.
        result = await log.attach(session.id, None, CollectSink())
        assert result.snapshot["running_partial"] == partial
        gate.gate.set()
        await log.wait_idle(session.id)
        await log.close()

    asyncio.run(scenario())


def test_an_idle_snapshot_reports_no_running_partial(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        await log.submit(session.id, [{"type": "text", "text": "hi"}], "c1")
        await log.wait_idle(session.id)
        assert log.snapshot(session.id)["running_partial"] is None
        await log.close()

    asyncio.run(scenario())


def test_a_running_partial_bigger_than_the_frame_is_clipped(tmp_path):
    """A partial is unbounded text too: it counts against the same budget."""
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        gate = GateExecutor()
        log.set_executor(gate)
        turn, _ = await log.submit(session.id, [{"type": "text", "text": "hi"}], "c2")
        assert await wait_until(lambda: log.queue_state(session.id)["running"] == turn.id)
        await log.emit(session.id, "turn.delta", {"turn_id": turn.id, "text": "字" * 32_000})

        snapshot = log.snapshot(session.id)
        partial = snapshot["running_partial"]
        assert partial["truncated"] is True
        assert partial["omitted_chars"] > 0
        assert partial["text"].endswith("字")  # the tail survives: deltas append
        assert partial["chars"] == len(partial["text"])
        assert snapshot["content_truncated"] is True
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES
        gate.gate.set()
        await log.wait_idle(session.id)
        await log.close()

    asyncio.run(scenario())
