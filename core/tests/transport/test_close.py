"""Governance really closes a live connection (blocking item B).

`App._close_connection` used to call the async `Connection.close()` without
awaiting it, so `set_enabled(False)` / `disconnect` / `revoke` only changed
memory and left the socket open. These tests use a real `Connection` with a
fake transport and the real `App._close_connection` implementation.
"""

from __future__ import annotations

import asyncio

import pytest

from vade.app import App
from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry
from vade.transport.connection import Connection, ConnectionState


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed_with: int | None = None
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def receive(self) -> bytes | None:
        return await self._inbox.get()

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code
        self._inbox.put_nowait(None)


def _manifest(instance: str = "fs-01"):
    return validate_manifest(
        {
            "protocol": "vade/v1",
            "module_id": "com.example.fs",
            "instance_id": instance,
            "version": "1.0.0",
            "concurrency": "pooled",
            "limits": {"max_concurrency": 3, "max_inflight": 8, "max_payload": 65519},
            "capabilities": {
                "tools": [
                    {
                        "name": "read_file",
                        "description": "read",
                        "risk": "low",
                        "timeout_ms": 10000,
                        "idempotent": False,
                        "input_schema": {"type": "object"},
                    }
                ]
            },
        }
    )


def _app_with(connection: Connection) -> App:
    app = App.__new__(App)  # no config needed for the hook
    app.transport = {"connections": {connection.connection_id: connection}}
    return app


@pytest.mark.asyncio
async def test_registry_disable_really_closes_the_websocket():
    ws = FakeWs()
    connection = Connection(ws, None, connection_id="c1", browser_session=True)
    task = asyncio.create_task(connection.run())
    await asyncio.sleep(0.05)
    assert connection.state is ConnectionState.ESTABLISHED

    app = _app_with(connection)
    registry = Registry(close_connection=app._close_connection)
    registry.register(_manifest(), fingerprint="f" * 16, connection_id="c1")
    registry.set_enabled("com.example.fs", "fs-01", False)

    for _ in range(50):
        if ws.closed_with is not None:
            break
        await asyncio.sleep(0.01)

    assert ws.closed_with == 1001, "the socket must be closed, not just the record"
    assert connection.state is ConnectionState.CLOSED
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_request_close_before_run_is_not_lost():
    ws = FakeWs()
    connection = Connection(ws, None, connection_id="c2", browser_session=True)
    connection.request_close(1001, "closed before start")
    assert connection.state is ConnectionState.NEW

    await asyncio.wait_for(connection.run(), timeout=2)
    assert ws.closed_with == 1001
    assert connection.state is ConnectionState.CLOSED


@pytest.mark.asyncio
async def test_revoke_fingerprint_closes_live_connection():
    ws = FakeWs()
    connection = Connection(ws, None, connection_id="c3", browser_session=True)
    task = asyncio.create_task(connection.run())
    await asyncio.sleep(0.05)

    app = _app_with(connection)
    registry = Registry(close_connection=app._close_connection)
    registry.register(_manifest(), fingerprint="a" * 16, connection_id="c3")
    closed = registry.revoke_fingerprint("a" * 16)
    assert closed == ("c3",)

    for _ in range(50):
        if ws.closed_with is not None:
            break
        await asyncio.sleep(0.01)
    assert ws.closed_with == 1001
    await asyncio.wait_for(task, timeout=2)
