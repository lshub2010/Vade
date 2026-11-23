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

f15 fix3: the retry that kept all three copies (legal under C-05) paid with
the user row at ~7 100 CJK; the snapshot now sends the text once and declares
the elision in `x-vade.dedup`, so the fields stay in the contract and the row
stays in the tree.

Every test drives the real `SessionLog` + real `SQLiteStore`; the size checks
use `Connection._encode_json`, the same encoder `_send_result` measures with.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from vade.loop.ports import ApprovalRequest
from vade.policy.adapter import PolicyAdapter
from vade.policy.approval import ApprovalManager
from vade.policy.engine import PolicyEngine
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
        assert _reply_size(session.id, result, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
        await log.close()

    asyncio.run(scenario())


def test_two_text_parts_plus_an_image_do_not_spin_the_clipper(tmp_path):
    """A legal message can carry two 6k-CJK text parts plus an image.

    Halving `content` and rebuilding it from the two untouched parts was a
    no-op: `_bound_snapshot` looped forever inside a synchronous attach and
    blocked the event loop. The attach must finish and mark the row clipped.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        left = "甲" * 6_000
        right = "乙" * 6_000
        message = Message(
            id="m-multi", session_id=session.id, role="user",
            content=left + right, turn_id="turn-multi",
            parts=(
                {"type": "text", "text": left},
                {"type": "image", "file_id": "f1", "mime": "image/png"},
                {"type": "text", "text": right},
            ),
        )
        await log.emit(session.id, "session.updated", {"session_id": session.id},
                       messages=[message])
        rpc = _rpc(log)
        result = await asyncio.wait_for(
            rpc.ui_attach(Conn(), {"session_id": session.id, "last_seq": None}),
            timeout=5,
        )
        snapshot = result["snapshot"]
        assert snapshot["truncated"] is True
        [row] = snapshot["messages"]
        assert row["truncated"] is True
        assert len(row["content"]) < len(left) + len(right)
        await log.close()

    asyncio.run(scenario())


def test_the_snapshot_sends_the_text_once(tmp_path):
    """The snapshot is deduped, and the elision is DECLARED on the wire.

    C-05 1.7.0 keeps `content`, `parts` and `user_text` declared, but sending
    all three cost a whole frame: a ~7 100-CJK user turn lost its own message
    row (f15 fix2 review P1-1). The snapshot sends the text once — text-only
    `parts` and `turns[].user_text` are elided — and `x-vade.dedup` names
    exactly what is gone plus how to rebuild it, so no client has to infer the
    shape from a missing key. A row with an image part keeps `parts`.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        await log.submit(session.id, [{"type": "text", "text": "hello"}], "c1")
        await log.wait_idle(session.id)

        snapshot = log.snapshot(session.id)
        user = next(m for m in snapshot["messages"] if m["role"] == "user")
        assert user["content"] == "hello"
        assert "parts" not in user, "text-only parts are the projection of content"
        [turn] = snapshot["turns"]
        assert "user_text" not in turn, "user_text is the projection of messages[]"
        # The declared fallback still resolves the user row both ways.
        assert user["id"] == turn["user_message_id"] or turn["user_message_id"] is None
        assert next(m for m in snapshot["messages"]
                    if m.get("turn_id") == turn["id"])["content"] == "hello"
        # The marker is additive, always present, and names both elisions.
        dedup = snapshot["x-vade"]["dedup"]
        assert dedup["version"] == 1
        assert dedup["elided"] == ["messages[].parts:text-only", "turns[].user_text"]
        assert dedup["fallback"]["messages[].parts"] == "content"
        assert "turn_id" in dedup["fallback"]["turns[].user_text"]
        # In-process shapes keep the fields: the elision is snapshot-only.
        stored = log.store.load(session.id)
        assert stored.messages[0].to_dict()["parts"] == [{"type": "text", "text": "hello"}]
        assert stored.turns[0].to_dict()["user_text"] == "hello"
        # A row with an image part keeps its ordered `parts` (order is data).
        mixed = Message(
            id="m-img", session_id=session.id, role="user", content="hi",
            parts=({"type": "text", "text": "hi"},
                   {"type": "image", "file_id": "f1", "mime": "image/png"}),
        )
        assert [p["type"] for p in mixed.to_snapshot()["parts"]] == ["text", "image"]
        await log.close()

    asyncio.run(scenario())


def test_a_7100_cjk_user_turn_keeps_its_message_row(tmp_path):
    """The row that used to be lost was the user's own text.

    De-duplicated text is not a micro-optimisation: `content` + `parts[].text`
    + `turns[].user_text` made a 7 100-CJK turn ~64 KB on its own, the reply
    went over the frame, and step ① dropped the USER message (`omitted=1`) —
    the tree lost its bubble and only `turns[].user_text` still had the words.
    With the text sent once the same session fits whole.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        text = "汉" * 7_100
        await log.submit(session.id, [{"type": "text", "text": text}], "c1")
        await log.wait_idle(session.id)

        snapshot = log.snapshot(session.id)
        assert snapshot["omitted"] == 0, "the user row must survive"
        assert snapshot["omitted_turns"] == 0
        assert snapshot["truncated"] is False
        assert snapshot["content_truncated"] is False
        user = next(m for m in snapshot["messages"] if m["role"] == "user")
        assert user["content"] == text, "the whole 7 100-CJK body is still there"
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
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
        # 32 000 DISTINCT characters: a head-keeping clip can never equal the
        # tail, so the assertion below is not a tautology on repeated text.
        text = "".join(chr(0x4E00 + (index % 2_000)) for index in range(32_000))
        await log.emit(session.id, "turn.delta", {"turn_id": turn.id, "text": text})

        snapshot = log.snapshot(session.id)
        partial = snapshot["running_partial"]
        assert partial["truncated"] is True
        assert partial["omitted_chars"] > 0
        # Real tail assertion: the THOUSANDS of characters are unique, so this
        # fails if the clipper keeps the head (the old `endswith("字")` was a
        # no-op when every character was the same).
        assert len(partial["text"]) < len(text)
        assert partial["text"] == text[-len(partial["text"]):]
        assert partial["chars"] == len(partial["text"])
        assert snapshot["content_truncated"] is True
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
        gate.gate.set()
        await log.wait_idle(session.id)
        await log.close()

    asyncio.run(scenario())


# ------------------------------------- f15: blocking state, not optional blocks
class ApprovalPort:
    """A pending-approval port whose rows the snapshot must never mutate."""

    def __init__(self, rows):
        self.rows = rows

    def pending(self, session_id):
        return self.rows


class TaskPort:
    def __init__(self, rows, truncated=False):
        self.rows = rows
        self.truncated = truncated

    def snapshot(self, session_id):
        return [dict(row) for row in self.rows], self.truncated


def _approval_row(index: int, *, args=None):
    """The exact shape `ApprovalManager._row()` publishes (no invented keys).

    A fake port must not invent fields (`detail` was one): the real approval
    row is `request_id/session_id/turn_id/tool/args/risk/created_at/deadline/
    task_id/task_name/depth`, and a test that pads with a field the port never
    sends proves nothing about a real row being dropped.
    """
    return {
        "request_id": f"req-{index}",
        "session_id": "s",
        "turn_id": f"turn-{index}",
        "tool": "fs__write_file",
        "args": args if args is not None else {"path": f"/tmp/{index}.txt"},
        "risk": "high",
        "created_at": 1,
        "deadline": 2,
        "task_id": None,
        "task_name": None,
        "depth": 0,
    }


def test_a_32k_approval_argument_is_clipped_not_dropped(tmp_path):
    """A legal 32k `args` string must be clipped, not cost the whole dialog.

    `pending_approvals[].args` is a Mapping, not one of the flat prompt fields
    the old clipper knew. One `fs__write_file` request with 32 000 CJK
    characters blew the frame and step ④ dropped the approval — a turn that
    then only ends by timeout. The row survives, clipped and marked, and the
    port's live request is untouched (it is detached before clipping).
    """
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()
        huge = "字" * 32_000
        row = _approval_row(1, args={"file": {"path": "/tmp/a.txt", "content": huge}})
        log.approval_port = ApprovalPort([row])

        snapshot = log.snapshot(session.id)

        assert len(snapshot["pending_approvals"]) == 1, "an approval is blocking state"
        kept = snapshot["pending_approvals"][0]
        assert kept["args"]["file"]["content"].startswith("字")
        assert len(kept["args"]["file"]["content"]) < 32_000
        assert kept["args_truncated"] is True
        assert kept["args_omitted_chars"] > 0
        assert kept["truncated"] is True
        assert snapshot["pending_truncated"] is False
        assert snapshot["omitted_approvals"] == 0
        assert snapshot["content_truncated"] is True
        assert snapshot["truncated"] is True
        # The port's live args are NOT the snapshot's copy.
        assert len(row["args"]["file"]["content"]) == 32_000
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
        assert [r["kind"] for r in audit.rows] == ["session.snapshot_truncated"]
        assert audit.rows[0]["extra"]["pending_truncated"] is False
        assert audit.rows[0]["extra"]["args_truncated"] is True
        assert audit.rows[0]["extra"]["args_omitted_chars"] > 0
        await log.close()

    asyncio.run(scenario())


def test_a_huge_task_list_does_not_take_the_approvals_with_it(tmp_path):
    """④ drops `tasks` first and RE-MEASURES; an approval in budget survives.

    The old step cleared tasks + pending in one unconditional sweep, so a task
    list that was the only thing over budget still cost every dialog.
    """
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()
        await _write_message(log, session.id, "assistant", "ok")
        log.tasks = TaskPort([
            {"task_id": f"t-{index}", "session_id": session.id,
             "state": "running", "summary": "x" * 4_000}
            for index in range(20)
        ], truncated=True)
        log.approval_port = ApprovalPort([_approval_row(7)])

        snapshot = log.snapshot(session.id)

        assert snapshot["tasks"] == []
        assert snapshot["tasks_truncated"] is True
        assert snapshot["omitted_tasks"] == 20
        assert len(snapshot["pending_approvals"]) == 1
        assert snapshot["pending_approvals"][0]["request_id"] == "req-7"
        assert snapshot["pending_truncated"] is False
        assert snapshot["omitted_approvals"] == 0
        assert snapshot["truncated"] is True
        assert audit.rows[0]["extra"]["omitted_tasks"] == 20
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
        await log.close()

    asyncio.run(scenario())


def _deep_value(depth: int, leaf: str = "leaf") -> Any:
    value: Any = leaf
    for _ in range(depth):
        value = {"n": value}
    return value


async def _open_approval(manager: ApprovalManager, session_id: str, turn_id: str,
                         args: dict[str, Any]):
    request = ApprovalRequest(
        session_id=session_id, turn_id=turn_id, tool_id="fs__write_file",
        args=args, risk="high", expires_at=0, emit=None,
    )
    pending, reason = await manager.open_request(request)
    assert pending is not None and reason == "approval_required"
    return pending


def test_a_deeply_nested_approval_is_kept_and_counted_not_cleared(tmp_path):
    """P0: a 900-level `args` must not deep-copy-fail every dialog away.

    Real `ApprovalManager` + `PolicyAdapter` (not a fake port): the manager
    mints a legal 6.3 KB request whose args nest 900 levels, `deepcopy` raises
    RecursionError, and the old snapshot swallowed it as `pending=[]` with
    `pending_truncated=false` and no audit — the exact silent-loss bug f15
    exists to kill. The row must survive WHOLE, be counted, and the sibling
    approval must not be collateral damage.
    """
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()
        manager = ApprovalManager(timeout_s=30.0, engine=PolicyEngine(), audit=None)
        log.approval_port = PolicyAdapter(manager)

        deep = await _open_approval(
            manager, session.id, "turn-deep",
            {"file": {"path": "/tmp/deep.txt", "content": _deep_value(900)}})
        shallow = await _open_approval(
            manager, session.id, "turn-shallow", {"path": "/tmp/plain.txt"})

        snapshot = log.snapshot(session.id)

        rows = {row["request_id"]: row for row in snapshot["pending_approvals"]}
        assert set(rows) == {deep.request_id, shallow.request_id}, \
            "one uncopyable row must not clear the whole pending block"
        deep_row = rows[deep.request_id]
        node: Any = deep_row["args"]["file"]["content"]
        depth = 0
        while isinstance(node, dict):
            node = node["n"]
            depth += 1
        assert (depth, node) == (900, "leaf"), "the row is kept whole"
        assert "args_truncated" not in deep_row, "shared args must never be clipped"
        # The shallow row was detached and is unaffected.
        assert rows[shallow.request_id]["args"] == {"path": "/tmp/plain.txt"}

        assert snapshot["pending_copy_failed"] == 1
        assert snapshot["pending_truncated"] is True
        assert snapshot["omitted_approvals"] == 0, "kept, not dropped"
        assert snapshot["truncated"] is True
        assert [row["kind"] for row in audit.rows] == ["session.snapshot_truncated"]
        extra = audit.rows[0]["extra"]
        assert extra["pending_copy_failed"] == 1
        assert extra["omitted_approvals"] == 0
        # The live request is byte-for-byte untouched.
        live = {row["request_id"]: row for row in manager.pending_requests()}
        node = live[deep.request_id]["args"]["file"]["content"]
        for _ in range(900):
            node = node["n"]
        assert node == "leaf"
        await log.close()

    asyncio.run(scenario())


def test_a_depth_9_argument_is_clipped_not_dropped(tmp_path):
    """Depth 9-64 is LEGAL: the copy accepts it, so the clipper must reach it.

    `_bounded_copy` allows 64 levels while `_longest_arg_text` used to stop at
    8. A depth-9 `args` was therefore copied (so it was NOT in
    `protected_args` — nobody protected it) and still invisible to
    `_clip_one_text`; step ④ then dropped the whole dialog. 21 500 CJK
    characters (~64.5 KB) fit the frame after ONE halving. The two bounds are
    now the same constant, so a legal nested request is clipped
    (`args_truncated`) instead of costing the dialog.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        manager = ApprovalManager(timeout_s=30.0, engine=PolicyEngine(), audit=None)
        log.approval_port = PolicyAdapter(manager)
        huge = "字" * 21_500
        pending = await _open_approval(
            manager, session.id, "turn-nested",
            {"file": {"path": "/tmp/nested.txt", "content": _deep_value(9, huge)}})

        snapshot = log.snapshot(session.id)

        rows = snapshot["pending_approvals"]
        assert len(rows) == 1, "clippable, so never dropped"
        row = rows[0]
        assert row["request_id"] == pending.request_id
        depth, leaf = 0, row["args"]["file"]["content"]
        while isinstance(leaf, dict):
            leaf = leaf["n"]
            depth += 1
        assert depth == 9
        assert len(leaf) < len(huge), "the clipper reached the depth-9 leaf"
        assert leaf == huge[:len(leaf)]
        assert row["args_truncated"] is True
        assert row["args_omitted_chars"] == len(huge) - len(leaf)
        assert snapshot["pending_copy_failed"] == 0
        assert snapshot["omitted_approvals"] == 0
        assert snapshot["pending_truncated"] is False
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
        # The approval port still holds the full, unclipped request.
        live = {r["request_id"]: r for r in manager.pending_requests()}
        live_leaf = live[pending.request_id]["args"]["file"]["content"]
        while isinstance(live_leaf, dict):
            live_leaf = live_leaf["n"]
        assert live_leaf == huge
        await log.close()

    asyncio.run(scenario())


def test_a_huge_task_list_does_not_eat_the_conversation(tmp_path):
    """P1-1: tasks go before MESSAGES, not just before pending dialogs.

    The old order dropped messages and clipped their text first and only then
    dropped `tasks`; a big task list left one 64-character message. The task
    list has `task/list` as a recovery channel — conversation rows do not.
    """
    async def scenario():
        log = _log(tmp_path)
        session = log.create_session()
        for index in range(40):
            await _write_message(log, session.id, "user",
                                 f"{index:04d}-" + "x" * 1_995,
                                 turn_id=f"turn-{index}")
        log.tasks = TaskPort([
            {"task_id": f"t-{index}", "session_id": session.id,
             "state": "running", "summary": "y" * 4_000}
            for index in range(20)
        ])

        snapshot = log.snapshot(session.id)

        assert snapshot["tasks"] == []
        assert snapshot["omitted_tasks"] == 20
        kept = snapshot["messages"]
        assert len(kept) >= 5, "tasks must be dropped before conversation rows"
        assert all(len(message["content"]) == 2_000 for message in kept)
        assert snapshot["omitted"] + len(kept) == 40
        assert snapshot["content_truncated"] is False
        await log.close()

    asyncio.run(scenario())


def test_a_huge_task_list_does_not_shrink_approval_args(tmp_path):
    """P1-1b: with tasks first, a legal 32k argument survives at ~16k, not 64.

    The old order clipped `pending_approvals[].args` in ③ and dropped `tasks`
    in ④: the approver saw the first 64 characters of the file that would be
    written. Dropping the recoverable task list first keeps the request legible
    (and `args_truncated` still says it is not the whole body).
    """
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()
        huge = "字" * 32_000
        log.tasks = TaskPort([
            {"task_id": f"t-{index}", "session_id": session.id,
             "state": "running", "summary": "y" * 4_000}
            for index in range(20)
        ])
        log.approval_port = ApprovalPort([
            _approval_row(1, args={"file": {"path": "/tmp/a.txt", "content": huge}})])

        snapshot = log.snapshot(session.id)

        assert snapshot["tasks"] == [] and snapshot["omitted_tasks"] == 20
        [row] = snapshot["pending_approvals"]
        kept = row["args"]["file"]["content"]
        assert len(kept) >= 8_000, "the old order left only 64 characters"
        assert len(kept) < 32_000
        assert row["args_truncated"] is True
        assert snapshot["pending_truncated"] is False
        assert snapshot["omitted_approvals"] == 0
        await log.close()

    asyncio.run(scenario())


def test_dropped_pending_is_counted_flagged_and_audited(tmp_path):
    """When pending really cannot fit, the loss is visible — and no tasks key appears.

    A core without the task component must not grow `tasks`/`tasks_truncated`
    during bounding: MISSING `tasks` is the documented "predates RFC-0007"
    signal. A dropped approval must set `truncated` even though no message or
    turn row was lost, and the audit row must carry the count.
    """
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()
        rows = [_approval_row(index) for index in range(400)]
        log.approval_port = ApprovalPort(rows)

        snapshot = log.snapshot(session.id)

        assert snapshot["pending_approvals"] == []
        assert snapshot["pending_truncated"] is True
        assert snapshot["omitted_approvals"] == 400
        assert snapshot["omitted_inputs"] == 0
        assert snapshot["truncated"] is True, "a silent loss is the bug"
        assert snapshot["content_truncated"] is False
        assert "tasks" not in snapshot
        assert "tasks_truncated" not in snapshot
        assert "omitted_tasks" not in snapshot
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
        assert [r["kind"] for r in audit.rows] == ["session.snapshot_truncated"]
        assert audit.rows[0]["extra"]["pending_truncated"] is True
        assert audit.rows[0]["extra"]["omitted_approvals"] == 400
        await log.close()

    asyncio.run(scenario())


def test_dropped_pending_inputs_are_counted_too(tmp_path):
    """A dropped `pending_inputs` row is blocking state as well: same flags.

    `_drop_pending` handles both blocks; covering only approvals would leave
    the input half free to regress into the old silent sweep. The task
    component is absent here, so the snapshot must not grow `tasks` keys just
    because bounding ran.
    """
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()

        class InputPort:
            def __init__(self, rows):
                self.rows = rows

            def pending(self, session_id):
                return self.rows

        # 200 chars is BELOW the 256-char clip threshold: nothing here can be
        # clipped away, so the only way to fit is to drop the block — counted.
        log.input_port = InputPort([
            {"input_id": f"in-{index}", "session_id": session.id,
             "turn_id": f"turn-{index}", "kind": "text",
             "prompt": "q" * 200, "created_at": index, "expires_at": index + 60}
            for index in range(400)
        ])

        snapshot = log.snapshot(session.id)

        assert snapshot["pending_inputs"] == []
        assert snapshot["pending_truncated"] is True
        assert snapshot["omitted_inputs"] == 400
        assert snapshot["omitted_approvals"] == 0
        assert snapshot["truncated"] is True
        assert snapshot["content_truncated"] is False
        assert "tasks" not in snapshot
        assert "tasks_truncated" not in snapshot
        assert "omitted_tasks" not in snapshot
        assert _reply_size(session.id, {
            "from_seq": snapshot["from_seq"], "snapshot": snapshot,
        }, reply_id="a" * 36) <= MAX_REPLY_BYTES - SNAPSHOT_ENVELOPE_RESERVE
        assert [r["kind"] for r in audit.rows] == ["session.snapshot_truncated"]
        assert audit.rows[0]["extra"]["pending_truncated"] is True
        assert audit.rows[0]["extra"]["omitted_inputs"] == 400
        await log.close()

    asyncio.run(scenario())


def test_both_pending_blocks_are_counted_when_dropped(tmp_path):
    """Approvals and inputs can be over budget together; both counts must move."""
    async def scenario():
        log = _log(tmp_path)
        audit = Audit()
        log.audit = audit
        session = log.create_session()

        class InputPort:
            def __init__(self, rows):
                self.rows = rows

            def pending(self, session_id):
                return self.rows

        log.approval_port = ApprovalPort(
            [_approval_row(index) for index in range(300)])
        log.input_port = InputPort([
            {"input_id": f"in-{index}", "session_id": session.id,
             "turn_id": f"turn-{index}", "kind": "text",
             "prompt": "q" * 200, "created_at": index, "expires_at": index + 60}
            for index in range(300)
        ])

        snapshot = log.snapshot(session.id)

        assert snapshot["pending_approvals"] == []
        assert snapshot["pending_inputs"] == []
        assert snapshot["pending_truncated"] is True
        assert snapshot["omitted_approvals"] == 300
        assert snapshot["omitted_inputs"] == 300
        assert snapshot["truncated"] is True
        assert audit.rows[0]["extra"]["omitted_approvals"] == 300
        assert audit.rows[0]["extra"]["omitted_inputs"] == 300
        await log.close()

    asyncio.run(scenario())


def test_rpc_json_keeps_the_snapshot_contract_compliant():
    """C-05 1.7.0: f15 adds no method, removes no field, registers no append.

    The rejected revision bumped the frozen contract to 1.8.0 with `adds=[]`
    and a free-text "removal" that no RFC owned; the fix2 retry then kept the
    full three-copy row and lost the user message at 7 100 CJK. The legal
    combination is: the CONTRACT keeps every field, the SNAPSHOT sends the
    text once, and `x-vade.dedup` declares the elision + the rebuild fallback.
    `check_contracts` enforces the same facts.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    rpc = json.loads((root / "schema" / "rpc.json").read_text(encoding="utf-8"))
    assert rpc["version"] == "1.7.0"
    assert not [
        block for block in rpc["x-vade"]["appends"]
        if "f15-snapshot-contract" in str(block.get("doc") or "")
    ], "f15 must not register a non-RFC append block"

    notes = rpc["directions"]["ui_to_core"]["ui/attach"]["notes"]
    for needle in ("`content`", "`parts`", "`user_text`", "`user_message_id`",
                   "turn_id", "x-vade.dedup", "ELIDED", "text travels ONCE",
                   "text:content", "1.7.0"):
        assert needle in notes, f"ui/attach notes do not document {needle}"
    assert "no key was renamed or removed" in notes
    for key in ("pending_truncated", "omitted_approvals", "omitted_inputs",
                "omitted_tasks", "args_truncated", "pending_copy_failed",
                "running_partial", "content_truncated"):
        assert key in notes, f"ui/attach notes do not document {key}"
    assert "dropped FIRST" in notes

    # Wiring pins: the notes describe a marker the code must actually emit,
    # and the two depth bounds must be the same constant.
    models = (root / "core" / "vade" / "session" / "models.py").read_text(encoding="utf-8")
    assert 'data.pop("parts", None)' in models
    assert 'data.pop("user_text", None)' in models
    manager = (root / "core" / "vade" / "session" / "manager.py").read_text(encoding="utf-8")
    assert "SNAPSHOT_DEDUP_PATHS" in manager and '"x-vade": _dedup_marker()' in manager
    assert "depth > cls._ARGS_COPY_MAX_DEPTH" in manager
