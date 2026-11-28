"""`/admin/api/memory*` over a fake `MemoryPort` (no module, no SQLite).

These tests pin the core-side contract only: the routes must forward to the
port, a browser cookie must not write, and only writes produce audit rows. They
deliberately do NOT claim the real `memory` module is exercised — that is M4a's
own suite. The port is the seam `admin/memory.py` owns, so a fake is the correct
unit under test here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vade.admin.app import create_admin_app
from vade.audit.reader import AuditReader
from vade.errors import VadeError
from vade.loop.ports import MemoryItem

from .conftest import bearer, client_for, login_browser, login_with_device, make_context, new_device


def _item(mem_id: str, text: str = "remembered", **meta) -> MemoryItem:
    base = {
        "layer": "global", "namespace": "user:default", "status": "active",
        "created_at": 1_700_000_000, "hit_count": 0, "tags": ["pref"],
        "supersedes": [], "session_id": "", "turn_id": None, "expires_at": None,
    }
    base.update(meta)
    return MemoryItem(id=mem_id, text=text, meta=base)


class FakeMemoryPort:
    """Minimal `MemoryPort`: records calls, returns canned rows."""

    def __init__(self) -> None:
        self.rows = [_item("01J0000000000000000000000A", "first"),
                     _item("01J0000000000000000000000B", "second")]
        self.cursors: list[str | None] = [None, "cursor-2", None]
        self.list_queries: list[dict] = []
        self.get_calls: list[str] = []
        self.writes: list[tuple[str, str, str | None]] = []
        self.stats_calls = 0
        self._list_index = 0

    async def read_session(self, session_id: str, limit: int = 50):
        return []

    async def admin_list(self, query):
        self.list_queries.append(dict(query))
        index = min(self._list_index, len(self.cursors) - 1)
        self._list_index += 1
        return list(self.rows), self.cursors[index]

    async def admin_get(self, mem_id: str):
        self.get_calls.append(mem_id)
        if mem_id == "01J0000000000000000000000A":
            return _item(mem_id), [_item(mem_id, status="superseded"),
                                   _item("01J0000000000000000000000C", status="active")]
        raise VadeError("NOT_FOUND", f"no memory {mem_id!r}")

    async def admin_promote(self, mem_id: str, by: str) -> bool:
        self.writes.append(("promote", mem_id, by))
        return True

    async def admin_archive(self, mem_id: str, reason: str, by: str) -> bool:
        self.writes.append(("archive", mem_id, reason))
        return True

    async def admin_restore(self, mem_id: str, by: str) -> bool:
        self.writes.append(("restore", mem_id, by))
        return True

    async def admin_stats(self):
        self.stats_calls += 1
        return {"global_count": 12, "global_limit": 5000, "staging_count": 3,
                "archived_count": 1, "oldest_at": 1_600_000_000, "internal": "drop-me"}


@pytest.fixture
def env(tmp_path):
    context = make_context(tmp_path)
    port = FakeMemoryPort()
    context.memory = port
    app = create_admin_app(context)
    yield context, port, app
    if context.audit is not None:
        context.audit.close()


async def _device_token(app, ctx) -> str:
    client = client_for(app)
    device, key = new_device(ctx)
    response = await login_with_device(client, ctx, device, key)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _kinds(ctx) -> list[str]:
    rows = AuditReader(ctx.audit.path).query(limit=200)["items"]
    return [row["kind"] for row in rows]


def test_list_forwards_the_query_and_returns_a_page(env):
    ctx, port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        client = client_for(app)
        response = await client.get(
            "/admin/api/memory?layer=global&namespace=user:default&status=active"
            "&from=10.5&to=20.9&q=pref&limit=25",
            headers=bearer(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [row["id"] for row in body["items"]] == [row.id for row in port.rows]
        assert body["items"][0]["text"] == "first"
        assert body["items"][0]["layer"] == "global"
        assert body["next_cursor"] is None
        assert port.list_queries == [{
            "layer": "global", "namespace": "user:default", "status": "active",
            # C-08 says `number`, C-16 says integer seconds: the handler is the
            # boundary that converts once (10.5 -> 10), so the module never sees
            # a float it would reject.
            "from": 10, "to": 20, "q": "pref", "limit": 25, "cursor": None,
        }]

    asyncio.run(scenario())


def test_stats_drops_module_internal_keys(env):
    ctx, port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        response = await client_for(app).get("/admin/api/memory/stats", headers=bearer(token))
        assert response.status_code == 200, response.text
        assert response.json() == {
            "global_count": 12, "global_limit": 5000, "staging_count": 3,
            "archived_count": 1, "oldest_at": 1_600_000_000,
        }
        assert port.stats_calls == 1

    asyncio.run(scenario())


def test_list_rejects_unknown_enum_and_bad_limit(env):
    ctx, _port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        client = client_for(app)
        for query, needle in (
            ("layer=disk", "layer"),
            ("status=dead", "status"),
            ("limit=0", "limit"),
            ("limit=201", "limit"),
            ("from=soon", "from"),
            ("q=" + "x" * 201, "q"),
        ):
            response = await client.get(f"/admin/api/memory?{query}", headers=bearer(token))
            assert response.status_code == 400, (query, response.status_code, response.text)
            assert response.json()["code"] == "INVALID_ARGUMENT"
            assert needle in response.json()["message"]

    asyncio.run(scenario())


def test_get_returns_the_supersedes_chain_and_a_missing_id_is_404(env):
    ctx, port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        client = client_for(app)
        found = await client.get("/admin/api/memory/01J0000000000000000000000A", headers=bearer(token))
        assert found.status_code == 200, found.text
        body = found.json()
        assert body["id"] == "01J0000000000000000000000A"
        assert [row["status"] for row in body["chain"]] == ["superseded", "active"]
        missing = await client.get("/admin/api/memory/01J0000000000000000000000Z", headers=bearer(token))
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "NOT_FOUND"
        assert port.get_calls == ["01J0000000000000000000000A", "01J0000000000000000000000Z"]

    asyncio.run(scenario())


def test_promote_archive_restore_write_audit_rows(env):
    ctx, port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        client = client_for(app)
        promote = await client.post("/admin/api/memory/m1/promote", headers=bearer(token))
        archive = await client.post("/admin/api/memory/m1/archive", json={"reason": "wrong fact"},
                                    headers=bearer(token))
        restore = await client.post("/admin/api/memory/m1/restore", headers=bearer(token))
        assert (promote.json(), archive.json(), restore.json()) == (
            {"ok": True}, {"ok": True}, {"ok": True})
        assert [w[0] for w in port.writes] == ["promote", "archive", "restore"]
        assert port.writes[0][2]  # the acting device id, never empty
        kinds = _kinds(ctx)
        assert "memory.promote" in kinds and "memory.archive" in kinds and "memory.restore" in kinds
        archive_rows = AuditReader(ctx.audit.path).query(kind="memory.archive")["items"]
        assert archive_rows[0]["extra"]["reason"] == "wrong fact"
        assert archive_rows[0]["extra"]["memory_id"] == "m1"

    asyncio.run(scenario())


def test_reads_do_not_write_audit_rows(env):
    ctx, _port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        client = client_for(app)
        await client.get("/admin/api/memory", headers=bearer(token))
        await client.get("/admin/api/memory/stats", headers=bearer(token))
        await client.get("/admin/api/memory/01J0000000000000000000000A", headers=bearer(token))
        assert not [kind for kind in _kinds(ctx) if kind.startswith("memory.")]

    asyncio.run(scenario())


def test_archive_requires_a_reason(env):
    ctx, port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        response = await client_for(app).post("/admin/api/memory/m1/archive", json={},
                                              headers=bearer(token))
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "INVALID_ARGUMENT"
        assert port.writes == []

    asyncio.run(scenario())


def test_bind_ports_picks_up_the_memory_slot_from_the_app(tmp_path):
    context = make_context(tmp_path)
    port = FakeMemoryPort()
    context.bind_ports(SimpleNamespace(memory_port=port))
    assert context.memory is port
    # Never overwrite a port a test injected directly.
    other = FakeMemoryPort()
    context.bind_ports(SimpleNamespace(memory_port=other))
    assert context.memory is port
    if context.audit is not None:
        context.audit.close()


def test_a_browser_cookie_may_read_but_never_write(env):
    ctx, port, app = env

    async def scenario():
        browser = client_for(app)
        logged = await login_browser(browser, ctx)
        assert logged.status_code == 200, logged.text
        assert logged.json()["mode"] == "browser"

        same_origin = {"Sec-Fetch-Site": "same-origin"}
        readable = await browser.get("/admin/api/memory", headers=same_origin)
        assert readable.status_code == 200, readable.text
        assert (await browser.get("/admin/api/memory/stats", headers=same_origin)).status_code == 200

        for path, body in (("/admin/api/memory/m1/promote", None),
                           ("/admin/api/memory/m1/archive", {"reason": "x"}),
                           ("/admin/api/memory/m1/restore", None)):
            response = await browser.post(path, json=body, headers=same_origin)
            assert response.status_code == 403, (path, response.status_code, response.text)
            assert response.json()["code"] == "READ_ONLY_SESSION"

        assert port.writes == []
        rejected = AuditReader(ctx.audit.path).query(kind="admin.rejected")["items"]
        assert any(row["code"] == "READ_ONLY_SESSION" for row in rejected)

    asyncio.run(scenario())


def test_archive_reason_matches_the_contract_limit(env):
    """C-16 caps `reason` at 300; the handler must not silently keep 200."""
    ctx, port, app = env

    async def scenario():
        token = await _device_token(app, ctx)
        client = client_for(app)
        too_long = await client.post(
            "/admin/api/memory/m1/archive",
            json={"reason": "x" * 301},
            headers=bearer(token),
        )
        assert too_long.status_code == 400, too_long.text
        assert too_long.json()["code"] == "INVALID_ARGUMENT"

        exact = await client.post(
            "/admin/api/memory/m1/archive",
            json={"reason": "y" * 300},
            headers=bearer(token),
        )
        assert exact.status_code == 200, exact.text
        assert port.writes[-1][0] == "archive"
        assert len(port.writes[-1][2]) == 300

    asyncio.run(scenario())
