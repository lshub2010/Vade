"""Config plane: nested overrides + reload, schema-aligned keys, honest hot.

Three defects used to live here and each has a regression below:

* PUT wrote `{"evolve.enabled": false}` (flat dotted key). `load_config()` reads
  one level per `properties` walk, so the next start saw an unknown *section*
  and refused to boot with UNKNOWN_CONFIG_KEY — one admin write bricked the core.
* The item table was a hand-maintained copy of the schema: four keys did not
  exist at all (`memory.global_limit`, `memory.staging_ttl_days`,
  `evolve.review_interval`, `e2e.enabled`) and `evolve.enabled` defaulted to
  True while the schema says False.
* GET echoed schema defaults forever (`source: "default"`) and every key was
  labelled `hot` even though nothing in the process re-read it.

The tests drive the real admin app (`create_admin_app`) over a real
`vade.config.Config`, and the restart case re-runs `load_config()` against the
same data dir, which is the only thing that would have caught defect 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from vade.admin.app import (
    CONFIG_ITEMS, HOT_APPLIERS, AdminContext, create_admin_app, item_mode, schema_leaf,
)
from vade.config import load_config
from vade.loop.ports import ApprovalRequest
from vade.policy.approval import ApprovalManager

from .conftest import bearer, client_for, login_with_device, make_context, new_device


def overrides_at(tmp_path: Path) -> Path:
    return tmp_path / "config.overrides.json"


def loaded(tmp_path: Path, *, toml: str | None = None):
    """A real `Config` whose overrides live in `tmp_path` (never `./data`)."""
    toml_path = None
    if toml is not None:
        toml_path = tmp_path / "core.toml"
        toml_path.write_text(toml, encoding="utf-8")
    return load_config(toml_path=toml_path, overrides_path=overrides_at(tmp_path), environ={})


def wire(tmp_path: Path, *, toml: str | None = None):
    """`(ctx, app)` exactly as production assembles them, config included."""
    ctx = make_context(tmp_path)
    ctx.app_config = loaded(tmp_path, toml=toml)
    return ctx, create_admin_app(ctx)


async def token_for(client, ctx):
    device, key = new_device(ctx)
    return (await login_with_device(client, ctx, device, key)).json()["access_token"]


def items_of(body) -> dict:
    return {item["key"]: item for item in body["items"]}


class _AppWithConfig:
    """The only attribute `AdminContext.from_app` reads for the config plane."""

    def __init__(self, config):
        self.config = config


def test_from_app_carries_the_loaded_config_into_the_context(tmp_path):
    """Assembly pin: the production path must hand `app.config` to the context.

    Every other test here sets `ctx.app_config` by hand, which is exactly how a
    wiring bug survives a green suite — `AdminContext.from_app` is what
    `admin.install()` calls.
    """
    (tmp_path / "core.toml").write_text("[audit]\nretention_days = 90\n", encoding="utf-8")
    config = load_config(
        toml_path=tmp_path / "core.toml", overrides_path=overrides_at(tmp_path),
        cli={"core.data_dir": str(tmp_path)}, environ={},
    )
    ctx = AdminContext.from_app(_AppWithConfig(config))
    assert ctx.app_config is config
    assert ctx.live_value("audit.retention_days") == 90
    assert ctx.source_of("audit.retention_days") == "core.toml"
    assert ctx.source_of("core.log_level") == "default"


def test_source_reports_env_when_the_variable_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("VADE_CORE__LOG_LEVEL", "debug")
    ctx = make_context(tmp_path)
    ctx.app_config = loaded(tmp_path)
    try:
        assert ctx.source_of("core.log_level") == "env"
    finally:
        ctx.audit.close()


# --------------------------------------------------------------- schema alignment
def test_every_whitelisted_key_is_a_real_schema_leaf():
    """A whitelist entry that is not a config key is a startup brick waiting."""
    for key in CONFIG_ITEMS:
        leaf = schema_leaf(key)
        assert leaf, (
            f"{key} is not a leaf of schema/config.schema.json; a PUT would write "
            "an override that the next load_config() rejects as UNKNOWN_CONFIG_KEY"
        )
        assert "default" in leaf, f"{key} has no schema default to report"


def test_the_four_phantom_keys_are_gone():
    for key in ("memory.global_limit", "memory.staging_ttl_days",
                "evolve.review_interval", "e2e.enabled"):
        assert key not in CONFIG_ITEMS, f"{key} is not a config key"
        assert schema_leaf(key) == {}, f"{key} unexpectedly exists in the schema"


def test_defaults_are_read_from_the_schema_not_a_copy(tmp_path):
    ctx, app = wire(tmp_path)

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        return items_of((await client.get("/admin/api/config", headers=bearer(token))).json())

    try:
        items = asyncio.run(scenario())
    finally:
        ctx.audit.close()
    for key, item in items.items():
        leaf = schema_leaf(key)
        assert item["default"] == leaf["default"], f"{key}: default drifted from the schema"
        if "minimum" in leaf:
            assert item["min"] == leaf["minimum"], key
        if "maximum" in leaf:
            assert item["max"] == leaf["maximum"], key
        if "enum" in leaf:
            assert item["values"] == list(leaf["enum"]), key
    # The one that had drifted for real.
    assert items["evolve.enabled"]["default"] is False
    assert items["evolve.enabled"]["value"] is False


# ------------------------------------------------------- nested write + reload
def test_put_writes_nested_overrides_and_the_next_load_succeeds(tmp_path):
    ctx, app = wire(tmp_path)

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        response = await client.put("/admin/api/config",
                                    json={"key": "evolve.nudge_interval", "value": 5},
                                    headers=bearer(token))
        assert response.status_code == 200, response.text

    try:
        asyncio.run(scenario())
    finally:
        ctx.audit.close()

    raw = json.loads(overrides_at(tmp_path).read_text(encoding="utf-8"))
    assert raw == {"evolve": {"nudge_interval": 5}}, raw
    assert not [key for key in raw if "." in key], "a flat dotted key aborts the next start"

    # The restart itself: same data_dir, fresh load_config(). Before the fix this
    # raised UNKNOWN_CONFIG_KEY with `evolve.nudge_interval` as the section name.
    reloaded = load_config(overrides_path=overrides_at(tmp_path), environ={})
    assert reloaded.get("evolve.nudge_interval") == 5

    # ... and a context built after the restart serves the persisted value.
    restarted = make_context(tmp_path, initialize=False)
    try:
        restarted.app_config = reloaded
        assert restarted.live_value("evolve.nudge_interval") == 5
        assert restarted.source_of("evolve.nudge_interval") == "overrides"
    finally:
        restarted.audit.close()


def test_a_flat_overrides_file_from_the_old_writer_is_repaired(tmp_path):
    overrides_at(tmp_path).write_text(
        json.dumps({"evolve.enabled": True, "core": {"log_level": "debug"}}), encoding="utf-8"
    )
    ctx = make_context(tmp_path)  # __post_init__ reads + repairs
    try:
        assert ctx._config_overrides == {"evolve": {"enabled": True}, "core": {"log_level": "debug"}}
    finally:
        ctx.audit.close()
    assert json.loads(overrides_at(tmp_path).read_text(encoding="utf-8")) == ctx._config_overrides
    reloaded = load_config(overrides_path=overrides_at(tmp_path), environ={})
    assert reloaded.get("evolve.enabled") is True
    assert reloaded.get("core.log_level") == "debug"


def test_an_unknown_key_is_rejected_and_leaves_a_bootable_data_dir(tmp_path):
    ctx, app = wire(tmp_path)

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        for key in ("e2e.enabled", "memory.global_limit", "policy.max_steps"):
            # `policy.max_steps` is a real config key, just not a §7.9 one.
            response = await client.put("/admin/api/config",
                                        json={"key": key, "value": 1}, headers=bearer(token))
            assert response.status_code == 400, (key, response.text)
            assert response.json()["code"] == "UNKNOWN_CONFIG_KEY", key

    asyncio.run(scenario())
    assert not overrides_at(tmp_path).exists(), "a rejected write must not touch the overrides file"
    # Still boots: that is what "被拒但不砖" means.
    load_config(overrides_path=overrides_at(tmp_path), environ={})
    ctx.audit.close()


def test_out_of_range_and_wrong_type_are_rejected_without_persisting(tmp_path):
    ctx, app = wire(tmp_path)

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        for value in (4, 99999, "120", True):
            response = await client.put("/admin/api/config",
                                        json={"key": "policy.approval_timeout_s", "value": value},
                                        headers=bearer(token))
            assert response.status_code == 400, (value, response.text)
            assert response.json()["code"] == "INVALID_ARGUMENT", value
        bad_level = await client.put("/admin/api/config",
                                     json={"key": "core.log_level", "value": "verbose"},
                                     headers=bearer(token))
        assert bad_level.status_code == 400

    asyncio.run(scenario())
    assert not overrides_at(tmp_path).exists()
    ctx.audit.close()


# ----------------------------------------------------- GET reflects the config
def test_get_reports_values_and_sources_from_the_loaded_config(tmp_path):
    ctx, app = wire(tmp_path, toml=(
        '[core]\nlog_level = "warning"\n'
        "[audit]\nretention_days = 90\n"
        "[evolve]\ndaily_token_budget = 12345\n"
    ))

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        items = items_of((await client.get("/admin/api/config", headers=bearer(token))).json())
        assert items["core.log_level"]["value"] == "warning"
        assert items["core.log_level"]["source"] == "core.toml"
        assert items["audit.retention_days"]["value"] == 90
        assert items["audit.retention_days"]["source"] == "core.toml"
        assert items["evolve.daily_token_budget"]["value"] == 12345
        # Untouched keys are still defaults, and say so.
        assert items["policy.approval_timeout_s"]["value"] == 60
        assert items["policy.approval_timeout_s"]["source"] == "default"
        assert items["evolve.nudge_interval"]["source"] == "default"

        # A write flips the same key's source without a restart of the process.
        saved = await client.put("/admin/api/config",
                                 json={"key": "evolve.nudge_interval", "value": 12},
                                 headers=bearer(token))
        assert saved.status_code == 200, saved.text
        after = items_of((await client.get("/admin/api/config", headers=bearer(token))).json())
        assert after["evolve.nudge_interval"]["value"] == 12
        assert after["evolve.nudge_interval"]["source"] == "overrides"

    asyncio.run(scenario())
    ctx.audit.close()


def test_get_reports_the_budget_override_to_the_usage_bar(tmp_path):
    ctx, app = wire(tmp_path)

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        assert (await client.get("/admin/api/config/usage", headers=bearer(token))).json()["tokens_budget"] == 200_000
        saved = await client.put("/admin/api/config",
                                 json={"key": "evolve.daily_token_budget", "value": 1500},
                                 headers=bearer(token))
        assert saved.status_code == 200, saved.text
        usage = (await client.get("/admin/api/config/usage", headers=bearer(token))).json()
        assert usage["tokens_budget"] == 1500

    asyncio.run(scenario())
    ctx.audit.close()


# ------------------------------------------------------------- hot vs restart
def test_hot_labels_are_backed_by_appliers():
    """The anti-fake-hot pin: a `hot` badge must have code behind it."""
    hot = {key for key in CONFIG_ITEMS if item_mode(key) == "hot"}
    assert hot == {"core.log_level", "policy.approval_timeout_s"}, hot
    assert set(HOT_APPLIERS) == hot
    # Everything else must not claim hot, whatever x-vade says.
    for key in CONFIG_ITEMS:
        if key not in hot:
            assert item_mode(key) == "restart", key


def test_hot_log_level_changes_the_running_logger(tmp_path):
    ctx, app = wire(tmp_path)
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, [(h, h.level) for h in root.handlers]
    try:
        async def scenario():
            client = client_for(app)
            token = await token_for(client, ctx)
            response = await client.put("/admin/api/config",
                                        json={"key": "core.log_level", "value": "debug"},
                                        headers=bearer(token))
            assert response.status_code == 200, response.text
            assert response.json()["applied"] is True
            assert response.json()["needs_restart"] is False
            items = items_of((await client.get("/admin/api/config", headers=bearer(token))).json())
            item = items["core.log_level"]
            assert (item["value"], item["source"], item["mode"]) == ("debug", "overrides", "hot")

        asyncio.run(scenario())
        # The real effect: the process logs at DEBUG from the next record on.
        assert root.level == logging.DEBUG
        assert ctx.app_config.get("core.log_level") == "debug"
    finally:
        root.setLevel(saved_level)
        for handler, level in saved_handlers:
            handler.setLevel(level)
        ctx.audit.close()


def test_hot_approval_timeout_changes_the_next_approval_deadline(tmp_path):
    ctx, app = wire(tmp_path)
    manager = ApprovalManager(timeout_s=60.0)
    ctx.approvals = manager
    try:
        async def scenario():
            client = client_for(app)
            token = await token_for(client, ctx)
            response = await client.put("/admin/api/config",
                                        json={"key": "policy.approval_timeout_s", "value": 120},
                                        headers=bearer(token))
            assert response.status_code == 200, response.text
            assert response.json()["applied"] is True
            assert manager.timeout_s == 120.0

            # `open_request` is the code that mints the deadline the UI counts
            # down and the wait honours; the new value must be in it.
            request = ApprovalRequest(
                session_id="s-hot", turn_id="t-hot", tool_id="shell",
                args={"cmd": "ls"}, risk="high", expires_at=0,
            )
            pending, reason = await manager.open_request(request)
            assert reason == "approval_required"
            assert pending is not None
            assert pending.deadline - pending.started == 120.0

        asyncio.run(scenario())
    finally:
        manager._pending.clear()
        ctx.audit.close()


def test_a_restart_key_reports_needs_restart_and_never_applied(tmp_path):
    ctx, app = wire(tmp_path)

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        response = await client.put("/admin/api/config",
                                    json={"key": "audit.retention_days", "value": 60},
                                    headers=bearer(token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] is False
        assert body["needs_restart"] is True
        items = items_of((await client.get("/admin/api/config", headers=bearer(token))).json())
        assert items["audit.retention_days"]["mode"] == "restart"
        assert items["audit.retention_days"]["value"] == 60
        assert items["audit.retention_days"]["source"] == "overrides"

    asyncio.run(scenario())
    # Persisted as a nested table even for the restart group.
    assert json.loads(overrides_at(tmp_path).read_text(encoding="utf-8")) == {"audit": {"retention_days": 60}}
    ctx.audit.close()


def test_evolve_keys_do_not_claim_a_hot_apply_without_a_consumer(tmp_path):
    ctx, app = wire(tmp_path)

    async def scenario():
        client = client_for(app)
        token = await token_for(client, ctx)
        # Enabling is not the dangerous direction and it is not hot either.
        enabled = await client.put("/admin/api/config",
                                   json={"key": "evolve.enabled", "value": True},
                                   headers=bearer(token))
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["applied"] is False
        assert enabled.json()["needs_restart"] is True
        disabled = await client.put("/admin/api/config",
                                    json={"key": "evolve.enabled", "value": False},
                                    headers=bearer(token))
        assert disabled.status_code == 409, disabled.text
        assert disabled.json()["code"] == "CONFIRM_REQUIRED"

    asyncio.run(scenario())
    ctx.audit.close()
