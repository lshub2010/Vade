"""RFC-0006 §6.3 (A11): edit_resubmit appends a new message, never UPDATEs one.

The old user message keeps its bytes and gains a `superseded_by` pointer, so an
export or a search still sees what the user actually typed the first time.
"""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore


def _log(tmp_path) -> SessionLog:
    async def executor(turn, content):
        return TurnOutcome(assistant_text=f"answer:{turn.user_text}", stopped_reason="stop")

    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    return log


def _user_messages(log, session_id):
    return [m for m in log.snapshot(session_id)["messages"] if m["role"] == "user"]


def test_edit_appends_a_message_and_points_the_old_one_at_it(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "typo"}], "s1")
        await log.wait_idle(sess.id)
        original = _user_messages(log, sess.id)[0]

        result = await log.edit_resubmit(
            sess.id, original["id"], [{"type": "text", "text": "fixed"}], "e1"
        )
        assert result["message_id"] != original["id"]
        assert result["supersedes_turn_id"] == turn.id
        assert result["version"] == 2
        await log.wait_idle(sess.id)

        snapshot = log.snapshot(sess.id)
        users = [m for m in snapshot["messages"] if m["role"] == "user"]
        old = next(m for m in users if m["id"] == original["id"])
        new = next(m for m in users if m["id"] == result["message_id"])
        # Immutability: the old row still holds its original text, plus a pointer.
        assert old["content"] == "typo"
        assert old["superseded_by"] == new["id"]
        assert new["content"] == "fixed"
        assert new["superseded_by"] is None

        old_turn = next(t for t in snapshot["turns"] if t["id"] == turn.id)
        new_turn = next(t for t in snapshot["turns"] if t["id"] == result["turn_id"])
        assert old_turn["superseded_by"] == new_turn["id"]
        assert old_turn["superseded_reason"] == "edit_resubmit"
        assert new_turn["user_message_id"] == new["id"]
        assert new_turn["root_turn_id"] == turn.id
        assert new_turn["version"] == 2

        events = log.store.read_since(sess.id, 0)
        edited = [e for e in events if e.type == "message.edited"]
        superseded = [e for e in events if e.type == "turn.superseded"]
        queued = [e for e in events if e.type == "turn.queued"]
        assert len(edited) == 1
        assert edited[0].payload == {
            "message_id": original["id"], "session_id": sess.id,
            "superseded_by_message_id": new["id"], "new_turn_id": new_turn["id"],
            "text": "fixed", "edited_at": edited[0].payload["edited_at"],
        }
        assert isinstance(edited[0].payload["edited_at"], int)
        # §6.4 order: edited → superseded → queued, strictly increasing.
        assert edited[0].seq < superseded[0].seq < queued[-1].seq
        assert superseded[0].payload["reason"] == "edit_resubmit"
        assert queued[-1].payload["reason"] == "edit_resubmit"
        assert queued[-1].payload["user_message_id"] == new["id"]

    asyncio.run(scenario())


def test_editing_after_a_regenerate_builds_on_the_new_head(tmp_path):
    """S3: v1 → regenerate v2 → edit must produce v3, not `not_head`.

    `regenerate` reuses the user message but leaves `message.turn_id` pointing
    at v1, so resolving the turn from that row read a superseded turn and killed
    the edit path on any version chain.
    """

    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "v1"}], "s1")
        await log.wait_idle(sess.id)
        second = await log.regenerate(sess.id, first.id, "r1")
        await log.wait_idle(sess.id)
        original = _user_messages(log, sess.id)[0]

        result = await log.edit_resubmit(
            sess.id, original["id"], [{"type": "text", "text": "v3"}], "e1"
        )
        assert result["supersedes_turn_id"] == second["turn_id"]
        assert result["root_turn_id"] == first.id
        assert result["version"] == 3
        await log.wait_idle(sess.id)

        turns = {t["id"]: t for t in log.snapshot(sess.id)["turns"]}
        assert turns[first.id]["superseded_by"] == second["turn_id"]
        assert turns[second["turn_id"]]["superseded_by"] == result["turn_id"]
        assert turns[second["turn_id"]]["superseded_reason"] == "edit_resubmit"
        head = turns[result["turn_id"]]
        assert head["parent_turn_id"] == second["turn_id"]
        assert head["superseded_by"] is None
        assert head["version"] == 3
        assert head["user_message_id"] == result["message_id"]
        assert len([t for t in turns.values() if t["superseded_by"] is None]) == 1

        users = _user_messages(log, sess.id)
        assert len(users) == 2
        new_message = next(m for m in users if m["id"] == result["message_id"])
        assert new_message["content"] == "v3"
        assert next(m for m in users if m["id"] == original["id"])["content"] == "v1"

        events = log.store.read_since(sess.id, 0)
        edited = [e for e in events if e.type == "message.edited"][-1]
        superseded = [e for e in events if e.type == "turn.superseded"][-1]
        queued = [e for e in events if e.type == "turn.queued"][-1]
        assert edited.seq < superseded.seq < queued.seq
        assert edited.payload["superseded_by_message_id"] == result["message_id"]
        assert superseded.payload["turn_id"] == second["turn_id"]
        assert queued.payload["version"] == 3

        # Idempotency must survive the head moving forward: a retry of the same
        # edit returns the turn it already created instead of a conflict.
        retry = await log.edit_resubmit(
            sess.id, original["id"], [{"type": "text", "text": "v3"}], "e1"
        )
        assert retry["turn_id"] == result["turn_id"]
        assert len(_user_messages(log, sess.id)) == 2

    asyncio.run(scenario())


def test_only_the_last_user_message_is_editable(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        await log.submit(sess.id, [{"type": "text", "text": "one"}], "s1")
        await log.wait_idle(sess.id)
        await log.submit(sess.id, [{"type": "text", "text": "two"}], "s2")
        await log.wait_idle(sess.id)
        first = _user_messages(log, sess.id)[0]

        with pytest.raises(VadeError) as caught:
            await log.edit_resubmit(sess.id, first["id"], [{"type": "text", "text": "x"}], "e1")
        assert caught.value.code == "INVALID_ARGUMENT"
        assert caught.value.data["reason"] == "not_tail"
        assert len(_user_messages(log, sess.id)) == 2

    asyncio.run(scenario())


def test_a_message_cannot_be_edited_twice(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        await log.submit(sess.id, [{"type": "text", "text": "a"}], "s1")
        await log.wait_idle(sess.id)
        original = _user_messages(log, sess.id)[0]
        await log.edit_resubmit(sess.id, original["id"],
                                [{"type": "text", "text": "b"}], "e1")
        await log.wait_idle(sess.id)
        with pytest.raises(VadeError) as caught:
            await log.edit_resubmit(sess.id, original["id"],
                                    [{"type": "text", "text": "c"}], "e2")
        assert caught.value.data["reason"] == "already_superseded"

    asyncio.run(scenario())


def test_an_edit_over_the_limit_creates_no_rows(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        await log.submit(sess.id, [{"type": "text", "text": "a"}], "s1")
        await log.wait_idle(sess.id)
        original = _user_messages(log, sess.id)[0]
        before = len(log.snapshot(sess.id)["messages"])

        with pytest.raises(VadeError) as caught:
            await log.edit_resubmit(sess.id, original["id"],
                                    [{"type": "text", "text": "汉" * 32_001}], "e1")
        assert caught.value.code == "PAYLOAD_TOO_LARGE"
        assert caught.value.data["field"] == "content[0].text"
        assert len(log.snapshot(sess.id)["messages"]) == before

    asyncio.run(scenario())


def test_a_non_user_message_cannot_be_edited(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        await log.submit(sess.id, [{"type": "text", "text": "hi"}], "s1")
        await log.wait_idle(sess.id)
        assistant = next(m for m in log.snapshot(sess.id)["messages"] if m["role"] == "assistant")
        with pytest.raises(VadeError) as caught:
            await log.edit_resubmit(sess.id, assistant["id"],
                                    [{"type": "text", "text": "x"}], "e1")
        assert caught.value.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())
