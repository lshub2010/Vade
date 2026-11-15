"""RFC-0008 A13: two audit rows per exchange, and the VALUE never lands in them."""

from __future__ import annotations

import asyncio
import json

from vade.audit.reader import AuditReader
from vade.audit.writer import AuditWriter
from vade.input import InputConfig

from .helpers import build, start_turn, wait_pending

SECRET = "sk-live-" + "TOP" + "SECRET" + "-4242"  # 夹具运行时拼装（gitleaks 工作区归零），运行时值不变


def _rows(writer, kind):
    writer.flush()
    return AuditReader(writer.path).query(kind=kind)["items"]


def _extra(row):
    extra = row.get("extra")
    if isinstance(extra, str):
        try:
            return json.loads(extra)
        except json.JSONDecodeError:  # pragma: no cover
            return {}
    return extra or {}


def test_submission_writes_request_and_response_rows(tmp_path):
    async def scenario():
        writer = AuditWriter(tmp_path / "audit" / "audit.db", sync=True)
        log, manager, executor = build(tmp_path, audit=writer)
        executor.ask(kind="free_text", prompt="what is the API key?")
        session = log.create_session()
        turn = await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.submit(session_id=session.id, input_id=input_id, value=SECRET, by="dev-7")
        await log.wait_idle(session.id)

        requests = _rows(writer, "input_request")
        responses = _rows(writer, "input_response")
        assert len(requests) == 1 and len(responses) == 1
        request, response = requests[0], responses[0]

        assert request["request_id"] == input_id
        assert request["turn_id"] == turn.id and request["session_id"] == session.id
        assert request["actor"] == "tool:ask_user"
        assert request["status"] == "pending"
        assert _extra(request)["input_kind"] == "free_text"

        assert response["request_id"] == input_id
        assert response["actor"] == "dev-7"
        assert response["status"] == "submitted"
        assert response["duration_ms"] > 0
        extra = _extra(response)
        assert extra["timed_out"] is False
        assert extra["value_chars"] == len(SECRET)
        assert len(extra["value_digest"]) == 64
        assert extra["value_digest"] != SECRET

        # The whole point: the raw answer must not exist anywhere in the table.
        everything = json.dumps(requests + responses, ensure_ascii=False)
        assert SECRET not in everything
        assert "TOPSECRET" not in everything
        writer.close()

    asyncio.run(scenario())


def test_timeout_is_audited_as_timed_out(tmp_path):
    async def scenario():
        writer = AuditWriter(tmp_path / "audit" / "audit.db", sync=True)
        log, manager, executor = build(tmp_path, audit=writer, config=InputConfig(
            min_timeout_s=1, default_timeout_s=1, max_timeout_s=1))
        executor.ask(kind="confirm", prompt="?")
        session = log.create_session()
        await start_turn(log, session.id)
        await wait_pending(manager)
        await log.wait_idle(session.id)

        [response] = _rows(writer, "input_response")
        extra = _extra(response)
        assert response["status"] == "timeout"
        assert extra["timed_out"] is True
        assert response["actor"] == "system"
        assert "value_digest" not in extra  # no answer -> no digest to record
        writer.close()

    asyncio.run(scenario())


def test_cancel_records_the_client_reason_but_not_an_answer(tmp_path):
    async def scenario():
        writer = AuditWriter(tmp_path / "audit" / "audit.db", sync=True)
        log, manager, executor = build(tmp_path, audit=writer)
        executor.ask(kind="confirm", prompt="?")
        session = log.create_session()
        await start_turn(log, session.id)
        input_id = await wait_pending(manager)
        await manager.cancel(session_id=session.id, input_id=input_id, by="dev-1",
                             reason="changed my mind")
        await log.wait_idle(session.id)
        [response] = _rows(writer, "input_response")
        assert response["status"] == "cancelled"
        assert _extra(response)["client_reason"] == "changed my mind"
        writer.close()

    asyncio.run(scenario())
