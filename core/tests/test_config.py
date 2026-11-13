"""C-18 config loading: defaults, priority, unknown keys, secrets, origins."""

from __future__ import annotations

import json

import pytest

from vade.config import ENV_PREFIX, load_config
from vade.errors import VadeError


def test_defaults_come_from_the_schema():
    cfg = load_config(environ={})
    assert cfg.get("core.listen") == "127.0.0.1:8443"
    assert cfg.get("core.admin_host") == "127.0.0.1:8444"
    assert cfg.get("memory.global_capacity") == 5000
    assert cfg.get("policy.approval_timeout_s") == 60
    assert cfg.get("server.listen_hosts") == ["127.0.0.1", "localhost"]
    assert cfg.get("dev.allow_all") is False
    assert cfg.get("skills.active_limit") == 64


def test_apply_and_owner_metadata_are_readable():
    cfg = load_config(environ={})
    assert cfg.apply_of("core.log_level") == "hot"
    assert cfg.apply_of("core.listen") == "restart"
    assert cfg.owner_of("policy.approval_timeout_s") == "C5a"
    assert cfg.owner_of("memory.global_capacity") == "M4a"
    assert cfg.requires_confirm("evolve.enabled") is True


def test_unknown_key_is_rejected_with_a_dotted_path():
    with pytest.raises(VadeError) as excinfo:
        load_config(environ={}, cli={"core.listen_tls": "x"})
    assert excinfo.value.code == "UNKNOWN_CONFIG_KEY"
    assert "core.listen_tls" in str(excinfo.value)


def test_unknown_section_is_rejected():
    with pytest.raises(VadeError) as excinfo:
        load_config(environ={}, cli={"nonsense": {"key": 1}})
    assert excinfo.value.code == "UNKNOWN_CONFIG_KEY"


def test_priority_cli_over_env_over_toml(tmp_path):
    toml = tmp_path / "core.toml"
    toml.write_text('[core]\nlisten = "127.0.0.1:7000"\nlog_level = "debug"\n', encoding="utf-8")
    cfg = load_config(
        toml_path=toml,
        environ={f"{ENV_PREFIX}CORE__LISTEN": "127.0.0.1:8000"},
        cli={"core.listen": "127.0.0.1:9000"},
    )
    assert cfg.get("core.listen") == "127.0.0.1:9000"
    assert cfg.get("core.log_level") == "debug"
    # env still applied for keys the CLI did not touch
    assert cfg.get("core.admin_host") == "127.0.0.1:8444"


def test_env_only(tmp_path):
    cfg = load_config(environ={f"{ENV_PREFIX}CORE__LISTEN": "0.0.0.0:9443", f"{ENV_PREFIX}DEV__ALLOW_ALL": "true"})
    assert cfg.get("core.listen") == "0.0.0.0:9443"
    assert cfg.get("dev.allow_all") is True


def test_overrides_file_beats_toml(tmp_path):
    toml = tmp_path / "core.toml"
    toml.write_text('[core]\nlisten = "127.0.0.1:7000"\n', encoding="utf-8")
    overrides = tmp_path / "config.overrides.json"
    overrides.write_text(json.dumps({"core": {"listen": "127.0.0.1:7500"}}), encoding="utf-8")
    cfg = load_config(toml_path=toml, overrides_path=overrides, environ={})
    assert cfg.get("core.listen") == "127.0.0.1:7500"


def test_toml_unknown_key_rejected(tmp_path):
    toml = tmp_path / "core.toml"
    toml.write_text('[core]\nlistne = "127.0.0.1:7000"\n', encoding="utf-8")
    with pytest.raises(VadeError) as excinfo:
        load_config(toml_path=toml, environ={})
    assert excinfo.value.code == "UNKNOWN_CONFIG_KEY"
    assert "core.listne" in str(excinfo.value)


def test_literal_secret_is_rejected():
    with pytest.raises(VadeError) as excinfo:
        load_config(environ={}, cli={"llm.base_url": "sk-live-abcdef"})
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "environment variable" in str(excinfo.value)


def test_api_key_literal_key_does_not_exist_in_schema():
    # Only `<name>_env` exists; a literal `api_key` key is an unknown key, which
    # is a startup failure rather than a silently-used secret.
    with pytest.raises(VadeError) as excinfo:
        load_config(environ={}, cli={"llm.api_key": "sk-live-abcdef"})
    assert excinfo.value.code == "UNKNOWN_CONFIG_KEY"


def test_env_var_name_for_secret_is_allowed():
    cfg = load_config(environ={}, cli={"llm.api_key_env": "OPENAI_API_KEY"})
    assert cfg.get("llm.api_key_env") == "OPENAI_API_KEY"


def test_ui_origin_must_be_in_listen_hosts():
    with pytest.raises(VadeError) as excinfo:
        load_config(environ={}, cli={"server.ui_origins": ["https://evil.example.com"]})
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "listen_hosts" in str(excinfo.value)


def test_ui_origin_on_localhost_is_allowed():
    cfg = load_config(environ={}, cli={"server.ui_origins": ["http://localhost:8080"]})
    assert cfg.get("server.ui_origins") == ["http://localhost:8080"]


def test_wildcard_origin_is_forbidden():
    with pytest.raises(VadeError):
        load_config(environ={}, cli={"server.ui_origins": ["*"]})


def test_out_of_range_value_is_rejected():
    with pytest.raises(VadeError):
        load_config(environ={}, cli={"policy.max_steps": 9999})


def test_schema_is_the_declared_contract():
    cfg = load_config(environ={})
    assert cfg.schema["x-vade"]["contract"] == "C-18"
    assert cfg.schema["x-vade"]["merge_order"] == ["cli", "env", "overrides", "toml", "defaults"]
    for section in ("core", "tls", "policy", "audit", "memory", "evolve", "compaction", "skills", "server", "dev"):
        assert section in cfg.schema["properties"], section
