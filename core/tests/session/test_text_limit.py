"""RFC-0006 §8 (A14): one message is at most 32,000 code points.

The counting unit is the code point, so 16,500 non-BMP emoji (33,000 UTF-16
units) must pass: a client that counted with JavaScript's `.length` would
refuse input the core accepts.
"""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.loop.engine import TurnOutcome
from vade.session.limits import TEXT_LIMIT
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore


def _log(tmp_path) -> SessionLog:
    async def executor(turn, content):
        return TurnOutcome(assistant_text="ok", stopped_reason="stop")

    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=100))
    log.set_executor(executor)
    return log


def test_the_limit_is_the_protocol_constant():
    assert TEXT_LIMIT == 32_000


def test_32000_code_points_pass(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": "汉" * 32_000}], "ok1")
        assert turn.user_text == "汉" * 32_000
        await log.wait_idle(sess.id)

    asyncio.run(scenario())


def test_32001_code_points_are_refused_with_a_field_level_error(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        with pytest.raises(VadeError) as caught:
            await log.submit(sess.id, [{"type": "text", "text": "汉" * 32_001}], "too-big")
        assert caught.value.code == "PAYLOAD_TOO_LARGE"
        assert caught.value.data == {
            "field": "content[0].text", "limit": 32_000, "actual": 32_001,
            "unit": "codepoints",
        }
        stored = log.store.load(sess.id)
        assert stored.turns == ()
        assert stored.messages == ()
        assert log.store.read_since(sess.id, 0) == []

    asyncio.run(scenario())


def test_the_sum_across_parts_is_capped_too(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        blocks = [{"type": "text", "text": "a" * 16_001},
                  {"type": "text", "text": "b" * 16_001}]
        with pytest.raises(VadeError) as caught:
            await log.submit(sess.id, blocks, "sum-too-big")
        assert caught.value.code == "PAYLOAD_TOO_LARGE"
        assert caught.value.data["field"] == "content"
        assert caught.value.data["actual"] == 32_002
        assert log.store.load(sess.id).turns == ()

    asyncio.run(scenario())


def test_the_limit_counts_code_points_not_utf16_units(tmp_path):
    async def scenario():
        log = _log(tmp_path)
        sess = log.create_session()
        emoji = "😀" * 16_500
        assert len(emoji.encode("utf-16-le")) // 2 == 33_000
        turn, _ = await log.submit(sess.id, [{"type": "text", "text": emoji}], "emoji")
        await log.wait_idle(sess.id)
        assert turn.user_text == emoji
        # And one code point over the line is still refused.
        with pytest.raises(VadeError) as caught:
            await log.submit(sess.id, [{"type": "text", "text": "😀" * 32_001}], "emoji-big")
        assert caught.value.data["actual"] == 32_001

    asyncio.run(scenario())
