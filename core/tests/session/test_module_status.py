"""`module.status` on the session seq bus (C-06 / 22-C3 T6).

Three trigger points the admin surface depends on: a node comes online
(register), goes away (disconnect) and is revoked. Each must reach every
attached UI as a normal persisted event, so a reconnect replays it instead of
guessing. Sessions with no live sink are skipped on purpose.
"""

from __future__ import annotations

import asyncio

from vade.registry.manifest import validate_manifest
from vade.registry.table import Registry
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore
from vade.session.status import ModuleStatusPublisher
from .helpers import CollectSink, make_manifest, wait_until


def _status(events: list[dict]) -> list[dict]:
    return [event["payload"] for event in events if event["type"] == "module.status"]


def test_module_status_fires_on_register_disconnect_and_revoke(tmp_path):
    async def scenario():
        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=500))
        registry = Registry()
        publisher = ModuleStatusPublisher(log)
        publisher.attach(registry)

        session = log.create_session()
        sink = CollectSink()
        await log.attach(session.id, None, sink)
        idle_session = log.create_session()  # no sink: must not get module.status

        manifest = validate_manifest(make_manifest())
        registry.register(manifest, fingerprint="fp-fs", connection_id="c-fs")
        assert await wait_until(lambda: _status(sink.events), 1.0)
        online = _status(sink.events)[-1]
        assert online == {"module_id": "com.example.fs", "instance_id": "fs-01",
                          "online": True, "version": "1.0.0"}

        registry.disconnect("com.example.fs", "fs-01")
        assert await wait_until(lambda: len(_status(sink.events)) == 2, 1.0)
        offline = _status(sink.events)[-1]
        assert offline["online"] is False
        assert offline["instance_id"] == "fs-01"

        closed = registry.revoke_fingerprint("fp-fs")
        # disconnect already dropped the connection, so nothing is left to close.
        assert closed == ()
        assert await wait_until(lambda: len(_status(sink.events)) == 3, 1.0)
        revoked = _status(sink.events)[-1]
        assert revoked["online"] is False

        # The same three changes are on the persisted seq bus, gapless.
        events = log.store.read_since(session.id, 0)
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert [e.type for e in events].count("module.status") == 3
        for event in (e for e in events if e.type == "module.status"):
            # C-06 required keys + the optional version is present as str|null.
            assert {"module_id", "instance_id", "online"} <= set(event.payload)
            assert event.payload["version"] in ("1.0.0", None)

        # A session nobody is attached to is skipped: no state to replay later.
        assert log.store.read_since(idle_session.id, 0) == []
        assert await publisher.publish({"module_id": "com.example.fs", "instance_id": "fs-01",
                                        "online": False}) == 1
        await asyncio.sleep(0.02)
        assert log.store.read_since(idle_session.id, 0) == []
        await log.close()

    asyncio.run(scenario())


def test_module_status_also_fires_on_a_capability_update(tmp_path):
    async def scenario():
        log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer())
        registry = Registry()
        publisher = ModuleStatusPublisher(log)
        publisher.attach(registry)
        session = log.create_session()
        sink = CollectSink()
        await log.attach(session.id, None, sink)

        registry.register(validate_manifest(make_manifest()), fingerprint="fp-fs",
                          connection_id="c-fs")
        assert await wait_until(lambda: len(_status(sink.events)) == 1, 1.0)
        epoch = registry.update_capabilities(
            validate_manifest({**make_manifest("com.example.fs", "fs-01"), "version": "1.0.1"}),
            fingerprint="fp-fs",
        )
        assert epoch == 2
        assert await wait_until(lambda: len(_status(sink.events)) == 2, 1.0)
        assert _status(sink.events)[-1]["version"] == "1.0.1"
        await log.close()

    asyncio.run(scenario())
