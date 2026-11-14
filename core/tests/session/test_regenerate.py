"""RFC-0006 §6.2 (A8–A10, A12): regenerate = new turn + `superseded_by`.

Covered here: the version chain, the event order, idempotency, the two refusal
reasons, and durability across a process restart (the greyed-out state must come
from the store, not from a client-side set).
"""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.loop.engine import TurnOutcome
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

from .helpers import wait_until


def _log(tmp_path, db="sessions.db", gate=None) -> SessionLog:
    async def executor(turn, content):
        if gate is not None:
            await gate.wait()
        return TurnOutcome(assistant_text=f"answer:{turn.user_text}", stopped_reason="stop")

    log = SessionLog(SQLiteStore(tmp_path / db), replay=ReplayBuffer(maxlen=500))
    log.set_executor(executor)
    return log


def _types(events):
    return [e.type for e in events]


def test_regenerate_supersedes_the_old_turn_and_reuses_the_user_message(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "s1")
        await log.wait_idle(sess.id)

        result = await log.regenerate(sess.id, first.id, "r1")
        assert result["supersedes_turn_id"] == first.id
        assert result["root_turn_id"] == first.id
        assert result["version"] == 2
        await log.wait_idle(sess.id)

        snapshot = log.snapshot(sess.id)
        old = next(t for t in snapshot["turns"] if t["id"] == first.id)
        new = next(t for t in snapshot["turns"] if t["id"] == result["turn_id"])
        assert old["superseded_by"] == new["id"]
        assert old["superseded_reason"] == "regenerate"
        assert old["version"] == 1
        assert new["version"] == 2
        assert new["parent_turn_id"] == first.id
        assert new["user_message_id"] == old["user_message_id"]

        # Exactly one user bubble: regenerate never appends a user message.
        users = [m for m in snapshot["messages"] if m["role"] == "user"]
        assert len(users) == 1
        assert users[0]["superseded_by"] is None

        events = log.store.read_since(sess.id, 0)
        superseded = [e for e in events if e.type == "turn.superseded"]
        queued = [e for e in events if e.type == "turn.queued"]
        assert len(superseded) == 1
        assert superseded[0].payload["reason"] == "regenerate"
        assert superseded[0].payload["superseded_by"] == new["id"]
        # Ordering (§6.4): the old turn is marked before the new one is queued.
        assert superseded[0].seq < queued[-1].seq
        assert queued[-1].payload["reason"] == "regenerate"
        assert queued[-1].payload["root_turn_id"] == first.id
        assert queued[-1].payload["version"] == 2
        assert queued[-1].payload["parent_turn_id"] == first.id
        assert queued[-1].payload["user_message_id"] == old["user_message_id"]

    asyncio.run(scenario())


def test_regenerating_a_turn_with_non_text_parts_is_unsupported(tmp_path):
    """S5: RFC §6.2 — parts are not rebuildable yet, so refuse, never drop.

    Only `text` becomes a stored message, so the old code rebuilt the text and
    silently lost the attachment. The turn row now remembers the part types and
    `regenerate` answers `UNSUPPORTED` with `data.capability="parts"` before it
    writes any row or event.
    """

    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        first, _ = await log.submit(
            sess.id,
            [{"type": "text", "text": "look"}, {"type": "image", "file_id": "f_123"}],
            "s1",
        )
        await log.wait_idle(sess.id)

        row = next(t for t in log.snapshot(sess.id)["turns"] if t["id"] == first.id)
        assert row["user_parts"] == ["image"]
        events_before = len(log.store.read_since(sess.id, 0))
        messages_before = len(log.snapshot(sess.id)["messages"])

        with pytest.raises(VadeError) as caught:
            await log.regenerate(sess.id, first.id, "r1")
        assert caught.value.code == "UNSUPPORTED"
        assert caught.value.data["capability"] == "parts"

        snapshot = log.snapshot(sess.id)
        assert len(snapshot["turns"]) == 1
        assert len(snapshot["messages"]) == messages_before
        assert len([m for m in snapshot["messages"] if m["role"] == "user"]) == 1
        assert len(log.store.read_since(sess.id, 0)) == events_before

        # The refusal is stored, not remembered: a restart answers the same.
        await log.close()
        reopened = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer())
        with pytest.raises(VadeError) as again:
            await reopened.regenerate(sess.id, first.id, "r2")
        assert again.value.code == "UNSUPPORTED"
        assert again.value.data["capability"] == "parts"
        await reopened.close()

    asyncio.run(scenario())


def test_the_same_client_msg_id_returns_the_same_new_turn(tmp_path):
    """A9: double click produces one version, not two."""

    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "s1")
        await log.wait_idle(sess.id)

        one = await log.regenerate(sess.id, first.id, "same")
        two = await log.regenerate(sess.id, first.id, "same")
        await log.wait_idle(sess.id)
        assert two["turn_id"] == one["turn_id"]
        assert len(log.snapshot(sess.id)["turns"]) == 2

    asyncio.run(scenario())


def test_a_client_msg_id_used_by_another_operation_is_a_conflict(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "taken")
        await log.wait_idle(sess.id)
        with pytest.raises(VadeError) as caught:
            await log.regenerate(sess.id, first.id, "taken")
        assert caught.value.code == "INVALID_ARGUMENT"
        assert caught.value.data["reason"] == "client_msg_id_conflict"

    asyncio.run(scenario())


def test_an_active_turn_cannot_be_regenerated(tmp_path):
    """A10: running/queued → INVALID_ARGUMENT + reason=turn_active."""

    async def scenario():
        gate = asyncio.Event()
        log = _log(tmp_path, gate=gate)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "s1")
        assert await wait_until(lambda: log.queue_state(sess.id)["running"] == first.id, 2.0)
        with pytest.raises(VadeError) as caught:
            await log.regenerate(sess.id, first.id, "r1")
        assert caught.value.code == "INVALID_ARGUMENT"
        assert caught.value.data["reason"] == "turn_active"
        assert len(log.snapshot(sess.id)["turns"]) == 1
        gate.set()
        await log.wait_idle(sess.id)

    asyncio.run(scenario())


def test_only_the_tail_of_the_session_can_be_regenerated(tmp_path):
    """A10: an older, still-head turn → reason=not_tail with head_turn_id."""

    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "one"}], "s1")
        await log.wait_idle(sess.id)
        second, _ = await log.submit(sess.id, [{"type": "text", "text": "two"}], "s2")
        await log.wait_idle(sess.id)

        with pytest.raises(VadeError) as caught:
            await log.regenerate(sess.id, first.id, "r1")
        assert caught.value.code == "INVALID_ARGUMENT"
        assert caught.value.data["reason"] == "not_tail"
        assert caught.value.data["head_turn_id"] == second.id
        assert len(log.snapshot(sess.id)["turns"]) == 2

    asyncio.run(scenario())


def test_a_superseded_turn_is_not_a_head(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "s1")
        await log.wait_idle(sess.id)
        newer = await log.regenerate(sess.id, first.id, "r1")
        await log.wait_idle(sess.id)

        with pytest.raises(VadeError) as caught:
            await log.regenerate(sess.id, first.id, "r2")
        assert caught.value.data["reason"] == "not_head"
        assert caught.value.data["head_turn_id"] == newer["turn_id"]

    asyncio.run(scenario())


def test_the_version_chain_survives_a_reopen(tmp_path):
    """A12: kill and restart; the greyed-out state must still be in the store."""

    async def scenario():
        log = _log(tmp_path, db="persist.db")
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "s1")
        await log.wait_idle(sess.id)
        newer = await log.regenerate(sess.id, first.id, "r1")
        await log.wait_idle(sess.id)
        await log.close()

        reopened = SessionLog(SQLiteStore(tmp_path / "persist.db"), replay=ReplayBuffer())
        snapshot = reopened.snapshot(sess.id)
        old = next(t for t in snapshot["turns"] if t["id"] == first.id)
        head = next(t for t in snapshot["turns"] if t["id"] == newer["turn_id"])
        assert old["superseded_by"] == head["id"]
        assert old["superseded_reason"] == "regenerate"
        assert head["superseded_by"] is None
        assert head["version"] == 2
        assert head["root_turn_id"] == first.id
        await reopened.close()

    asyncio.run(scenario())


def test_regenerated_history_excludes_the_replaced_answer(tmp_path):
    """§6.6: the new turn must not see the answer it replaced."""

    async def scenario():
        seen: list[list[str]] = []
        answers = iter(["first try", "second try"])

        async def executor(turn, content):
            session = log.get_session(turn.session_id)
            from vade.loop.context import filter_superseded

            seen.append([m.content for m in filter_superseded(session.messages, session.turns)])
            return TurnOutcome(assistant_text=next(answers, "more"), stopped_reason="stop")

        log = SessionLog(SQLiteStore(tmp_path / "hist.db"), replay=ReplayBuffer())
        log.set_executor(executor)
        sess = log.create_session()
        first, _ = await log.submit(sess.id, [{"type": "text", "text": "hi"}], "s1")
        await log.wait_idle(sess.id)
        await log.regenerate(sess.id, first.id, "r1")
        await log.wait_idle(sess.id)

        # The regenerated turn sees the user message once and NOT the old answer.
        assert seen[-1] == ["hi"]

    asyncio.run(scenario())
