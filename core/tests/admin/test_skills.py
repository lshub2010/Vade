"""C5b: `/admin/api/skills*` is a thin surface over `SkillLoader`.

The tests pin the contract from `schema/admin-api.yaml` *and* the C3b rules the
API must not re-implement: install only lands in `pending/`, `approve` is what
bumps the revision and writes `.history/`, `rollback` appends a new revision
instead of rewriting an old one, and a browser cookie cannot write at all.
"""

from __future__ import annotations

import asyncio

import pytest

from vade.admin.app import create_admin_app
from vade.skill.loader import SkillLoader

from .conftest import bearer, client_for, login_browser, login_with_device, make_context, new_device


def _skill(name: str, marker: str = "first") -> str:
    return f"""---
name: {name}
description: 测试用 skill
when_to_use: 需要验证技能管理 API 时
triggers: ["测试"]
---

## When to Use

- 只用于测试。

## Procedure

1. body marker {marker}

## Pitfalls

- 无。

## Verification

- 正文里能找到 {name}。
"""


@pytest.fixture
def env(tmp_path):
    context = make_context(tmp_path)
    loader = SkillLoader(tmp_path / "skills", tool_snapshot=[])
    context.skills = loader
    app = create_admin_app(context)
    yield context, loader, app
    if context.audit is not None:
        context.audit.close()


async def _client_with_token(app, ctx):
    client = client_for(app)
    device, key = new_device(ctx)
    response = await login_with_device(client, ctx, device, key)
    assert response.status_code == 200, response.text
    return client, response.json()["access_token"]


async def _preview(client, token, name, marker="first"):
    response = await client.post(
        "/admin/api/skills/install/preview",
        json={"source": "upload", "content": _skill(name, marker)},
        headers=bearer(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _install(client, token, preview_token, expected=200):
    response = await client.post("/admin/api/skills/install",
                                 json={"preview_token": preview_token}, headers=bearer(token))
    assert response.status_code == expected, response.text
    return response


def _seed(tmp_path, loader, name, marker="seeded"):
    directory = tmp_path / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(_skill(name, marker), encoding="utf-8")
    loader.reload()


# ----------------------------------------------------------------------- list
def test_list_comes_from_the_loader_and_install_lands_in_pending(env, tmp_path):
    _ctx, loader, app = env
    _seed(tmp_path, loader, "manual-one", "seeded")

    async def scenario():
        client, token = await _client_with_token(app, _ctx)
        rows = (await client.get("/admin/api/skills", headers=bearer(token))).json()["items"]
        assert [row["name"] for row in rows] == ["manual-one"]
        assert rows[0]["status"] == "active"
        assert rows[0]["revision"] == 0

        preview = await _preview(client, token, "fresh-one")
        assert preview["valid"] is True
        assert preview["name"] == "fresh-one"
        assert preview["conflict"] is False
        assert preview["preview_token"]

        installed = await _install(client, token, preview["preview_token"])
        assert installed.json()["status"] == "pending"
        assert installed.json()["name"] == "fresh-one"

        # AC "安装不生效": the loader's own active catalog does not contain it.
        assert "fresh-one" not in {state.name for state in loader.list_all()}
        assert "fresh-one" in {state.name for state in loader.list_all(include_pending=True)}

        rows = {row["name"]: row for row in
                (await client.get("/admin/api/skills", headers=bearer(token))).json()["items"]}
        assert rows["fresh-one"]["status"] == "pending"
        assert rows["manual-one"]["status"] == "active"

        pending_only = (await client.get("/admin/api/skills/fresh-one",
                                         headers=bearer(token))).json()
        assert pending_only["status"] == "pending"
        assert pending_only["pending_revision"] == 0
        assert pending_only["pending_diff"] is not None    # everything is new
        assert pending_only["history"] == []

        # A preview token is one-shot on BOTH sides (stash and loader).
        await _install(client, token, preview["preview_token"], expected=400)

    asyncio.run(scenario())


def test_install_without_a_token_is_invalid_argument(env):
    _ctx, _loader, app = env

    async def scenario():
        client, token = await _client_with_token(app, _ctx)
        missing = await client.post("/admin/api/skills/install", json={}, headers=bearer(token))
        assert missing.status_code == 400, missing.text
        assert missing.json()["code"] == "INVALID_ARGUMENT"

        unknown = await client.post("/admin/api/skills/install",
                                    json={"preview_token": "0" * 64}, headers=bearer(token))
        assert unknown.status_code == 400, unknown.text
        assert unknown.json()["code"] == "INVALID_ARGUMENT"

        bad_source = await client.post("/admin/api/skills/install/preview",
                                       json={"source": "carrier-pigeon"}, headers=bearer(token))
        assert bad_source.status_code == 400

    asyncio.run(scenario())


# -------------------------------------------------------------------- approve
def test_approve_bumps_the_revision_and_writes_history(env, tmp_path):
    _ctx, loader, app = env

    async def scenario():
        client, token = await _client_with_token(app, _ctx)
        preview = await _preview(client, token, "promo-one")
        await _install(client, token, preview["preview_token"])

        approved = await client.post("/admin/api/skills/promo-one/approve",
                                     json={"revision": 0}, headers=bearer(token))
        assert approved.status_code == 200, approved.text
        assert approved.json()["revision"] == 1
        assert approved.json()["status"] == "active"

        assert (tmp_path / "skills" / ".history" / "promo-one" / "1.md").is_file()
        assert "promo-one" in {state.name for state in loader.list_all()}

        detail = (await client.get("/admin/api/skills/promo-one", headers=bearer(token))).json()
        assert detail["revision"] == 1
        assert detail["body"].find("body marker first") != -1
        assert [row["revision"] for row in detail["history"]] == [1]
        assert detail["pending_diff"] is None

    asyncio.run(scenario())


def test_approve_with_a_stale_revision_is_a_conflict(env):
    _ctx, _loader, app = env

    async def scenario():
        client, token = await _client_with_token(app, _ctx)
        preview = await _preview(client, token, "conflict-one")
        await _install(client, token, preview["preview_token"])

        stale = await client.post("/admin/api/skills/conflict-one/approve",
                                  json={"revision": 7}, headers=bearer(token))
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "TOOL_CONFLICT"

        missing = await client.post("/admin/api/skills/nope/approve",
                                    json={"revision": 0}, headers=bearer(token))
        assert missing.status_code == 404

    asyncio.run(scenario())


# --------------------------------------------------------------------- detail
def test_detail_serves_a_backend_diff_for_a_pending_proposal(env, tmp_path):
    _ctx, loader, app = env
    _seed(tmp_path, loader, "manual-two", "first")

    async def scenario():
        client, token = await _client_with_token(app, _ctx)
        preview = await _preview(client, token, "manual-two", "second")
        assert preview["conflict"] is True
        assert preview["conflict_with"] == "active"
        await _install(client, token, preview["preview_token"])

        detail = (await client.get("/admin/api/skills/manual-two", headers=bearer(token))).json()
        assert detail["status"] == "active"          # the live one
        assert detail["pending_revision"] == 0
        assert detail["pending_diff"] and "+1. body marker second" in detail["pending_diff"]
        assert "-1. body marker first" in detail["pending_diff"]

    asyncio.run(scenario())


# --------------------------------------------------------------------- reject
def test_reject_moves_the_proposal_into_rejected(env, tmp_path):
    _ctx, _loader, app = env

    async def scenario():
        client, token = await _client_with_token(app, _ctx)
        preview = await _preview(client, token, "rej-one")
        await _install(client, token, preview["preview_token"])

        no_reason = await client.post("/admin/api/skills/rej-one/reject",
                                      json={"revision": 0}, headers=bearer(token))
        assert no_reason.status_code == 400, no_reason.text

        rejected = await client.post("/admin/api/skills/rej-one/reject",
                                     json={"revision": 0, "reason": "not useful"},
                                     headers=bearer(token))
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["ok"] is True

        moved = list((tmp_path / "skills" / ".rejected").iterdir())
        assert [path.name.startswith("rej-one-") for path in moved] == [True]
        rows = (await client.get("/admin/api/skills", headers=bearer(token))).json()["items"]
        assert "rej-one" not in {row["name"] for row in rows}
        assert (await client.get("/admin/api/skills/rej-one",
                                 headers=bearer(token))).status_code == 404

    asyncio.run(scenario())


# ------------------------------------------------------------------- rollback
def test_rollback_appends_a_new_revision(env, tmp_path):
    _ctx, loader, app = env

    async def scenario():
        client, token = await _client_with_token(app, _ctx)

        first = await _preview(client, token, "roll-one", "first")
        await _install(client, token, first["preview_token"])
        await client.post("/admin/api/skills/roll-one/approve",
                          json={"revision": 0}, headers=bearer(token))

        second = await _preview(client, token, "roll-one", "second")
        await _install(client, token, second["preview_token"])
        approved = await client.post("/admin/api/skills/roll-one/approve",
                                     json={"revision": 1}, headers=bearer(token))
        assert approved.json()["revision"] == 2

        rolled = await client.post("/admin/api/skills/roll-one/rollback",
                                   json={"to_revision": 1}, headers=bearer(token))
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["revision"] == 3
        # History is append-only: 1, 2 and the new 3 all exist.
        for revision in (1, 2, 3):
            assert (tmp_path / "skills" / ".history" / "roll-one" / f"{revision}.md").is_file()
        assert "body marker first" in loader.get("roll-one").body

        unknown = await client.post("/admin/api/skills/roll-one/rollback",
                                    json={"to_revision": 9}, headers=bearer(token))
        assert unknown.status_code == 404

    asyncio.run(scenario())


# ------------------------------------------------------------- toggle / delete
def test_disable_and_uninstall_go_through_the_loader(env, tmp_path):
    _ctx, loader, app = env

    async def scenario():
        client, token = await _client_with_token(app, _ctx)
        preview = await _preview(client, token, "tog-one")
        await _install(client, token, preview["preview_token"])
        await client.post("/admin/api/skills/tog-one/approve",
                          json={"revision": 0}, headers=bearer(token))

        disabled = await client.patch("/admin/api/skills/tog-one",
                                      json={"status": "disabled"}, headers=bearer(token))
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disabled"
        assert loader.get("tog-one").status == "disabled"

        bad = await client.patch("/admin/api/skills/tog-one",
                                 json={"status": "approved"}, headers=bearer(token))
        assert bad.status_code == 400

        removed = await client.delete("/admin/api/skills/tog-one", headers=bearer(token))
        assert removed.status_code == 200, removed.text
        assert removed.json()["ok"] is True
        assert loader.get("tog-one") is None
        assert any(path.name.startswith("tog-one-")
                   for path in (tmp_path / "skills" / ".trash").iterdir())

    asyncio.run(scenario())


# ------------------------------------------------------------------- read-only
def test_without_a_loader_the_routes_still_answer_501(tmp_path):
    """A build without C3b keeps the label the schema's 501 documents."""
    context = make_context(tmp_path)
    app = create_admin_app(context)
    assert context.skills is None

    async def scenario():
        client, token = await _client_with_token(app, context)
        response = await client.get("/admin/api/skills", headers=bearer(token))
        assert response.status_code == 501, response.text
        assert response.json()["code"] == "UNSUPPORTED"

    asyncio.run(scenario())
    if context.audit is not None:
        context.audit.close()


def test_a_browser_cookie_cannot_write_skills(env):
    _ctx, _loader, app = env

    async def scenario():
        browser = client_for(app)
        await login_browser(browser, _ctx)
        headers = {"Sec-Fetch-Site": "same-origin"}

        preview = await browser.post(
            "/admin/api/skills/install/preview",
            json={"source": "upload", "content": _skill("cookie-one")}, headers=headers,
        )
        assert preview.status_code == 403, preview.text
        assert preview.json()["code"] == "READ_ONLY_SESSION"

        patch = await browser.patch("/admin/api/skills/whatever",
                                    json={"status": "disabled"}, headers=headers)
        assert patch.status_code == 403
        assert patch.json()["code"] == "READ_ONLY_SESSION"

        # GET stays open: a browser session renders the list.
        assert (await browser.get("/admin/api/skills", headers=headers)).status_code == 200

    asyncio.run(scenario())
