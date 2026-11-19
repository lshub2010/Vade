"""P1-5: `POST /admin/setup` and the bind path share one first-run commit lock.

`admin.json` existing *is* "initialised"; both entry points create it. Before
this, `bind.py` serialised bind↔bind with `_SETUP_COMMITS` while the browser
path held nothing at all, so a phone and a laptop finishing first-run at the
same moment each wrote a document: the last writer won and the other password
silently stopped working. The setup token was also consumed twice (the second
`unlink` a no-op), so nothing looked wrong.

These tests produce the two orderings deterministically instead of hoping a race
lands: `_write_admin` is the first statement of the critical section, so a gate
around it parks whichever request got in first while the test drives the other.

Assertions are structural, not timing-based: the number of `admin.json` writes
across both requests, and which password the single surviving document holds.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from vade.admin.app import create_admin_app
from vade.admin.auth import verify_password
from vade.admin.setup import SetupService, setup_commit_lock

from .conftest import PASSWORD, client_for, make_context
from .test_bind import NEW_PASSWORD, hello, p256_device, post, run, seal


class _WriteGate:
    """Record every `admin.json` write and park the first one until released."""

    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def install(self, monkeypatch) -> "_WriteGate":
        original = SetupService._write_admin
        gate = self

        def gated(service, doc):  # noqa: ANN001 - bound method stand-in
            gate.docs.append(dict(doc))
            gate.entered.set()
            # Never hang the suite on a bug: a timeout releases the writer and
            # the call-count assertion below still reports the interleaving.
            gate.release.wait(5.0)
            return original(service, doc)

        monkeypatch.setattr(SetupService, "_write_admin", gated)
        return self


async def _wait_entered(gate: _WriteGate) -> bool:
    """Wait off the loop for the parked writer (it is a worker thread)."""
    return await asyncio.get_running_loop().run_in_executor(None, gate.entered.wait, 5.0)


def _admin_documents(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "admin").glob("admin*"))


def _first_run_plane(tmp_path):
    ctx = make_context(tmp_path, initialize=False)
    return ctx, create_admin_app(ctx)


def test_both_entry_points_wait_on_the_one_exported_lock(tmp_path):
    """Holding `setup_commit_lock()` from outside must hold *both* paths off.

    This is the direct statement of the fix: not "each path has a lock" but "the
    browser path and the bind path queue on the same object". A third party (a
    test, a future component) that takes the lock therefore sees a plane where
    nobody can create `admin.json` — and a bind reservation answers the
    documented, retriable 423 rather than blocking a worker thread.
    """
    ctx, app = _first_run_plane(tmp_path)
    lock = setup_commit_lock()
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock:
            held.set()
            release.wait(5.0)

    keeper = threading.Thread(target=hold, name="test-setup-lock-holder", daemon=True)
    keeper.start()
    assert held.wait(5.0), "the lock holder never started"

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        body = seal(info, ticket, password=NEW_PASSWORD, device_key=key, device_pub=pub)

        setup_task = asyncio.create_task(
            client.post("/admin/setup", json={"password": PASSWORD})
        )
        refused = await post(client, body)
        assert refused.status_code == 423, refused.text
        assert refused.json()["code"] == "BIND_SETUP_LOCKED"

        await asyncio.sleep(0.1)
        assert not setup_task.done(), "the browser wrote admin.json while the lock was held"
        assert ctx.setup.initialized() is False

        release.set()
        created = await setup_task
        assert created.status_code == 200, created.text
        return created.json()

    try:
        run(scenario())
    finally:
        release.set()
        keeper.join(timeout=5.0)

    assert ctx.setup.initialized() is True
    assert [path.name for path in _admin_documents(tmp_path)] == ["admin.json"]
    assert verify_password(PASSWORD, ctx.setup.password_hash()) is True
    assert len(ctx.devices) == 0, "the refused bind must not have enrolled a device"


def test_a_browser_setup_refuses_a_concurrent_first_run_bind(tmp_path, monkeypatch):
    """Browser wins: the bind gets 423 (not a second write), ticket still good.

    `BIND_SETUP_LOCKED` is the documented retriable refusal, so the App can
    simply retry the same QR — which this test does, with the password the
    browser actually installed.
    """
    ctx, app = _first_run_plane(tmp_path)
    gate = _WriteGate().install(monkeypatch)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        body = seal(info, ticket, password=NEW_PASSWORD, device_key=key, device_pub=pub)

        setup_task = asyncio.create_task(
            client.post("/admin/setup", json={"password": PASSWORD})
        )
        assert await _wait_entered(gate), "the browser never reached the admin.json write"

        refused = await post(client, body)
        assert refused.status_code == 423, refused.text
        assert refused.json()["code"] == "BIND_SETUP_LOCKED"

        gate.release.set()
        created = await setup_task
        assert created.status_code == 200, created.text

        # One document, one writer — and it holds the browser's password.
        assert len(gate.docs) == 1, f"admin.json was written {len(gate.docs)} times"
        assert ctx.setup.password_hash() == gate.docs[0]["password"]
        assert verify_password(PASSWORD, ctx.setup.password_hash()) is True
        assert verify_password(NEW_PASSWORD, ctx.setup.password_hash()) is False

        # The refused ticket was never burned: the App retries the same QR and
        # enrols with the password that now exists.
        assert ctx.setup.peek_ticket(ticket) is not None
        retried = await post(
            client,
            seal(await hello(client, ticket), ticket,
                 password=PASSWORD, device_key=key, device_pub=pub),
        )
        assert retried.status_code == 200, retried.text
        assert len(ctx.devices) == 1
        return retried.json()

    body = run(scenario())
    assert body["mode"] == "device"

    # Exactly one `admin.json`, no `admin.tmp` left behind by a losing writer.
    assert [path.name for path in _admin_documents(tmp_path)] == ["admin.json"]
    assert len(gate.docs) == 1


def test_a_bind_commit_refuses_a_concurrent_browser_setup(tmp_path, monkeypatch):
    """Bind wins: the browser is refused with 404 and never writes a document.

    The browser path has no ticket to retry — once `admin.json` exists there is
    nothing left to initialise — so the thing to prove is that it *cannot* get a
    write in while the bind's commit is in flight, and that the document that
    survives is the bind's.
    """
    ctx, app = _first_run_plane(tmp_path)
    gate = _WriteGate().install(monkeypatch)

    async def scenario():
        client = client_for(app)
        ticket = ctx.setup.create_ticket()["ticket"]
        info = await hello(client, ticket)
        key, pub = p256_device()
        body = seal(info, ticket, password=NEW_PASSWORD, device_key=key, device_pub=pub)

        bind_task = asyncio.create_task(post(client, body))
        assert await _wait_entered(gate), "the bind never reached the admin.json write"

        setup_task = asyncio.create_task(
            client.post("/admin/setup", json={"password": PASSWORD})
        )
        # 150 ms is many file writes: a browser that is not blocked on the
        # shared lock has long since written its own document by now.
        await asyncio.sleep(0.15)
        assert not setup_task.done(), (
            "the browser completed a first-run setup while a bind was committing"
        )

        gate.release.set()
        bound = await bind_task
        created = await setup_task
        assert bound.status_code == 200, bound.text
        assert created.status_code == 404, created.text
        assert created.json()["code"] == "NOT_FOUND"

        assert len(gate.docs) == 1, f"admin.json was written {len(gate.docs)} times"
        assert ctx.setup.password_hash() == gate.docs[0]["password"]
        assert verify_password(NEW_PASSWORD, ctx.setup.password_hash()) is True
        assert verify_password(PASSWORD, ctx.setup.password_hash()) is False
        assert len(ctx.devices) == 1
        return bound.json()

    body = run(scenario())
    assert body["mode"] == "device"

    assert [path.name for path in _admin_documents(tmp_path)] == ["admin.json"]
    assert len(gate.docs) == 1
