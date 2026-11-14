"""RFC-0007 A15 + §6: the `task/*` RPC surface, read-only gates, messaging."""

from __future__ import annotations

import asyncio

import pytest

from vade.errors import VadeError
from vade.tasks.rpc import TaskRPC

from .helpers import FakeConnection, make_log, make_registry


def _rpc(log, **overrides):
    registry = make_registry(log, **overrides)
    return registry, TaskRPC(registry)


def test_read_only_session_may_read_but_not_write():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry, rpc = _rpc(log)
        job = await registry.spawn(kind="job", name="index", session_id=session.id)
        registry.append_log(job.task_id, "hello")
        readonly = FakeConnection(read_only=True)

        listed = await rpc.task_list(readonly, {"session_id": session.id})
        assert listed["tasks"][0]["task_id"] == job.task_id
        tail = await rpc.task_log_tail(readonly, {"task_id": job.task_id})
        assert tail["lines"][0]["text"] == "hello"

        with pytest.raises(VadeError) as cancel:
            await rpc.task_cancel(readonly, {"task_id": job.task_id})
        assert cancel.value.code == "READ_ONLY_SESSION"

        with pytest.raises(VadeError) as message:
            await rpc.subagent_message(readonly, {"task_id": job.task_id, "text": "hi"})
        assert message.value.code == "READ_ONLY_SESSION", "write check precedes the feature flag"

    asyncio.run(scenario())


def test_cancel_rpc_shape_and_unknown_task():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry, rpc = _rpc(log, cancel_grace_ms=100)
        job = await registry.spawn(kind="job", name="index", session_id=session.id)
        await registry.set_state(job.task_id, "running")

        result = await rpc.task_cancel(FakeConnection(), {"task_id": job.task_id, "reason": "stop"})
        assert result == {"ok": True, "state": "cancelling", "forced": False}
        await asyncio.sleep(0.3)
        again = await rpc.task_cancel(FakeConnection(), {"task_id": job.task_id})
        assert again["ok"] is True and again["state"] == "cancelled" and again["forced"] is True

        with pytest.raises(VadeError) as missing:
            await rpc.task_cancel(FakeConnection(), {"task_id": "nope"})
        assert missing.value.code == "NOT_FOUND"

        with pytest.raises(VadeError) as bad:
            await rpc.task_cancel(FakeConnection(), {})
        assert bad.value.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_subagent_message_is_unsupported_by_default():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry, rpc = _rpc(log)
        sub = await registry.spawn(
            kind="subagent", name="helper", session_id=session.id, child_session_id="child"
        )
        with pytest.raises(VadeError) as caught:
            await rpc.subagent_message(FakeConnection(), {"task_id": sub.task_id, "text": "hi"})
        assert caught.value.code == "UNSUPPORTED"
        assert caught.value.data.get("reason") == "subagent_message_disabled"

    asyncio.run(scenario())


def test_subagent_message_queues_when_enabled_and_wired():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry, rpc = _rpc(log, subagent_message=True)
        sub = await registry.spawn(
            kind="subagent", name="helper", session_id=session.id,
            child_session_id="child", child_turn_id="child-turn",
        )
        delivered: list[tuple[str, str]] = []

        async def hook(task, text):
            delivered.append((task.task_id, text))

        registry.set_message_hook(hook)
        result = await rpc.subagent_message(
            FakeConnection(), {"task_id": sub.task_id, "text": "please also check b"}
        )
        assert result == {"ok": True, "turn_id": "child-turn", "delivery": "queued"}
        assert delivered == [(sub.task_id, "please also check b")]

        with pytest.raises(VadeError) as empty:
            await rpc.subagent_message(FakeConnection(), {"task_id": sub.task_id, "text": ""})
        assert empty.value.code == "INVALID_ARGUMENT"

        job = await registry.spawn(kind="job", name="job", session_id=session.id)
        with pytest.raises(VadeError) as not_subagent:
            await rpc.subagent_message(FakeConnection(), {"task_id": job.task_id, "text": "hi"})
        assert not_subagent.value.code == "INVALID_ARGUMENT"

    asyncio.run(scenario())


def test_list_and_log_tail_reject_bad_parameters():
    async def scenario():
        log = make_log()
        session = log.create_session()
        registry, rpc = _rpc(log)
        job = await registry.spawn(kind="job", name="index", session_id=session.id)

        with pytest.raises(VadeError) as limit:
            await rpc.task_list(FakeConnection(), {"limit": 500})
        assert limit.value.code == "INVALID_ARGUMENT"

        with pytest.raises(VadeError) as follow:
            await rpc.task_log_tail(FakeConnection(), {"task_id": job.task_id, "follow": "yes"})
        assert follow.value.code == "INVALID_ARGUMENT"

        with pytest.raises(VadeError) as cursor:
            await rpc.task_log_tail(FakeConnection(), {"task_id": job.task_id, "cursor": "@@"})
        assert cursor.value.code == "INVALID_ARGUMENT"

        with pytest.raises(VadeError) as missing:
            await rpc.task_log_tail(FakeConnection(), {"task_id": "nope"})
        assert missing.value.code == "NOT_FOUND"

    asyncio.run(scenario())


def test_install_publishes_the_four_handlers():
    from vade import tasks as tasks_package

    class Cfg(dict):
        pass

    class App:
        config = Cfg()
        session_handlers: dict = {}

    log = make_log()
    app = App()
    app.config = Cfg({"tasks.cancel_grace_ms": 500})
    app.session_handlers = {}
    tasks_package.install(app)
    assert set(app.session_handlers) == {
        "task/list", "task/log_tail", "task/cancel", "subagent/message",
    }
    assert app.tasks is app.task_registry

    class WithSession(App):
        pass

    app2 = WithSession()
    app2.config = Cfg()
    app2.session_handlers = {}
    app2.session = log
    tasks_package.install(app2)
    assert log.tasks is app2.tasks, "install() wires the log for snapshot/interrupt"
