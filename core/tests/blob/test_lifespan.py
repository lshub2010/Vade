"""Lifespan forwarding and the second close path (review r2, 建议3/4).

`CoreApp._lifespan` is the only thing that gives the blob channel a long-lived
loop, so `start` (the GC task) and `stop` (await its cancellation) have to be
reached from the ASGI entry point, not just from the tests that call the
service directly. Deleting the `lifespan` branch of `CoreApp.__call__` used to
leave the whole suite green; these cases drive that branch.

`App.shutdown()` is the *second* close path (tests, embedded cores): it looks
for an awaitable closer first and only then falls back to the sync `close()`.
`BlobApp` has to expose both names for that to be true.
"""

from __future__ import annotations

import asyncio

from vade.app import App
from vade.config import load_config
from vade.transport.server import CoreApp

from .conftest import ORIGIN, Clock, build_env


def _config(tmp_path):
    return load_config(
        cli={
            "core.data_dir": str(tmp_path / "core"),
            "server.ui_origins": [ORIGIN],
            # The origin's host must be one of the listener's names, otherwise
            # `load_config` refuses to start (SameSite=Strict would break login).
            "server.listen_hosts": ["127.0.0.1", "localhost", "ui.example"],
        }
    )


def _drive_lifespan(app, *events: dict[str, str]) -> list[dict[str, str]]:
    """Run one ASGI lifespan exchange to completion and collect what was sent."""
    sent: list[dict[str, str]] = []
    queue = list(events)

    async def receive() -> dict[str, str]:
        if not queue:
            raise AssertionError("lifespan asked for more messages than the client sent")
        return queue.pop(0)

    async def send(message: dict[str, str]) -> None:
        sent.append(message)

    asyncio.run(app({"type": "lifespan"}, receive, send))
    return sent


class Recorder:
    """Blob stand-in: records the lifecycle calls, no service behind it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")


# ------------------------------------------------- 1. the forwarding itself
def test_lifespan_startup_and_shutdown_reach_the_blob_channel(tmp_path):
    blob = Recorder()
    core = CoreApp(config=_config(tmp_path), blob=blob)

    sent = _drive_lifespan(
        core, {"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}
    )

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert blob.calls == ["start", "stop"]  # in order, nothing skipped


def test_lifespan_without_a_blob_still_completes_both_halves(tmp_path):
    """`attach.enabled=false` must not leave uvicorn waiting on a message."""
    core = CoreApp(config=_config(tmp_path), blob=None)

    sent = _drive_lifespan(
        core, {"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}
    )

    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


def test_a_start_failure_is_reported_as_startup_failed(tmp_path):
    class Broken(Recorder):
        async def start(self) -> None:
            raise RuntimeError("no loop for you")

    core = CoreApp(config=_config(tmp_path), blob=Broken())

    sent = _drive_lifespan(core, {"type": "lifespan.startup"})

    assert sent == [{"type": "lifespan.startup.failed", "message": "blob channel failed to start"}]


# --------------------------------------------- 2. the real service behind it
def test_the_lifespan_really_starts_and_stops_the_gc_task(tmp_path):
    env = build_env(tmp_path / "data", Clock())
    core = CoreApp(config=_config(tmp_path), blob=env.app)
    try:
        sent = _drive_lifespan(
            core, {"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}
        )
        assert sent == [
            {"type": "lifespan.startup.complete"},
            {"type": "lifespan.shutdown.complete"},
        ]
        # `stop()` awaited the cancellation and released the sqlite handle.
        assert env.service._gc_task is None
        assert env.service._closed is True
    finally:
        env.service.close()


# ------------------------------------------------ 3. App.shutdown closers
def test_shutdown_prefers_the_async_closer(tmp_path):
    calls: list[str] = []

    class Dual:
        async def aclose(self) -> None:
            calls.append("aclose")

        def close(self) -> None:
            calls.append("close")

    app = App(_config(tmp_path))
    setattr(app, "blob", Dual())

    asyncio.run(app.shutdown())

    assert calls == ["aclose"]  # the awaitable one, not the sync fallback


def test_shutdown_falls_back_to_a_sync_closer(tmp_path):
    calls: list[str] = []

    class SyncOnly:
        def close(self) -> None:
            calls.append("close")

    app = App(_config(tmp_path))
    setattr(app, "blob", SyncOnly())

    asyncio.run(app.shutdown())

    assert calls == ["close"]


def test_shutdown_awaits_the_real_blob_app(tmp_path):
    env = build_env(tmp_path / "data", Clock())
    assert callable(getattr(env.app, "aclose", None))  # the name shutdown looks for
    app = App(_config(tmp_path))
    setattr(app, "blob", env.app)

    async def start_then_shutdown():
        await env.app.start()
        task = env.service._gc_task
        assert task is not None and not task.done()
        await app.shutdown()
        return task

    task = asyncio.run(start_then_shutdown())

    try:
        assert task.done()
        assert task.cancelled()  # awaited, not merely cancel()ed and dropped
        assert env.service._gc_task is None
        assert env.service._closed is True
    finally:
        env.service.close()
