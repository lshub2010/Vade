"""The admin plane's late ports really are bound by the assembly (review D2).

`install_components()` walks `vade.*` **alphabetically**, so `admin` installs
before `audit` and `policy` — the two components that own `audit_writer` and
`approvals`. `AdminContext.from_app` therefore saw `None` for both, and a
second pass at the end of `install_components()` is the only place that knows
every `install()` has run.

These tests use the same two calls `__main__._serve` makes (`App.startup()` +
`install_components()`) and then drive the real Starlette app. Nothing here
injects `ctx.audit`/`ctx.approvals` by hand: a fixture that did would pass even
with the bug this file pins, which is how the empty audit page and the 501 on
`/admin/api/approvals/decide` survived the unit tests.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from vade.admin.app import create_admin_app
from vade.app import App, install_components
from vade.config import load_config
from vade.loop.ports import ApprovalRequest

ADMIN_HOST = "127.0.0.1:8444"
ORIGIN = f"https://{ADMIN_HOST}"
PASSWORD = "correct-horse-battery"


@pytest_asyncio.fixture
async def app(tmp_path):
    config = load_config(cli={"core.data_dir": str(tmp_path), "core.admin_host": ADMIN_HOST})
    instance = App(config)
    await instance.startup()
    install_components(instance, strict=False)
    try:
        yield instance
    finally:
        writer = getattr(instance, "audit_writer", None)
        if writer is not None:
            writer.close()
        await instance.shutdown()


def _client(instance: App) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(instance.admin), client=("127.0.0.1", 40000)),
        base_url=ORIGIN,
    )


async def _login(client: httpx.AsyncClient) -> dict[str, str]:
    """First-run setup, then a loopback CLI login (mode `device`).

    The CLI header is the documented way to get a device-mode bearer from
    loopback (`__main__.py` uses the same path); a browser session could not
    approve anything, by design.
    """
    setup = await client.post("/admin/setup", json={"password": PASSWORD})
    assert setup.status_code == 200, setup.text
    challenge = (await client.post("/admin/api/login/challenge", json={})).json()
    login = await client.post(
        "/admin/api/login",
        json={"challenge_id": challenge["challenge_id"], "password": PASSWORD},
        headers={"x-ac-client": "cli"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["mode"] == "device"
    body = login.json()
    return {"Authorization": f"Bearer {body['access_token']}", "x-csrf-token": body["csrf_token"]}


def test_install_components_binds_the_ports_owned_by_later_components(app: App) -> None:
    ctx = app.admin
    assert ctx is not None
    # Same objects the app published, not copies: the admin plane must read the
    # rows C5a writes and resolve the requests C5a's policy mints.
    assert app.audit_writer is not None
    assert ctx.audit is app.audit_writer
    assert app.approvals is not None
    assert ctx.approvals is app.approvals


@pytest.mark.asyncio
async def test_an_audit_row_written_by_the_admin_plane_is_queryable(app: App) -> None:
    ctx = app.admin
    audit_id = ctx.write_audit(kind="assembly.audit", status="ok", actor="k3-identity")
    assert audit_id, "ctx.audit was not bound, so write_audit dropped the event"
    # The writer queues by default; the admin reader opens the same SQLite file.
    app.audit_writer.flush()

    async with _client(app) as client:
        headers = await _login(client)
        response = await client.get("/admin/api/audit", params={"kind": "assembly.audit"}, headers=headers)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1, items
    # The db row id is numeric; the id `record()` returns is `extra.audit_id`.
    assert items[0]["extra"]["audit_id"] == audit_id
    assert items[0]["actor"] == "k3-identity"


@pytest.mark.asyncio
async def test_approvals_decide_resolves_a_real_pending_request(app: App) -> None:
    """The 501 was `ctx.approvals is None`; a bound port answers 200."""
    pending, reason = await app.approvals.open_request(ApprovalRequest(
        session_id="s-1", turn_id="t-1", tool_id="fs/write", args={"path": "notes.md"},
        risk="high", expires_at=0,
    ))
    assert reason == "approval_required" and pending is not None

    async with _client(app) as client:
        headers = await _login(client)
        response = await client.post(
            "/admin/api/approvals/decide",
            json={"request_id": pending.request_id, "decision": "allow", "scope": "once"},
            headers=headers,
        )

    assert response.status_code != 501, response.text
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert pending.future.done()
    allowed, by, scope, _ = pending.future.result()
    assert (allowed, by, scope) == (True, "loopback_admin", "once")
