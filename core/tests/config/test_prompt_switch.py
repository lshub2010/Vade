"""A11 (RFC-0003 §7): `prompt.injection` defaults to `legacy`; `layered` must
be turned on explicitly; the `[tools]` keys default per the RFC."""

from __future__ import annotations

import pytest

from vade.config import ConfigError, load_config


def test_empty_config_defaults_to_legacy(tmp_path):
    cfg = load_config(toml_path=tmp_path / "core.toml", overrides_path=tmp_path / "overrides.json")
    assert cfg.get("prompt.injection") == "legacy"
    assert cfg.get("tools.hot") == []
    assert cfg.get("tools.hot_max") == 12


def test_layered_must_be_explicit(tmp_path):
    (tmp_path / "core.toml").write_text('[prompt]\ninjection = "layered"\n', encoding="utf-8")
    cfg = load_config(toml_path=tmp_path / "core.toml", overrides_path=tmp_path / "overrides.json")
    assert cfg.get("prompt.injection") == "layered"
    assert cfg.get("tools.hot") == []  # layered does not imply a hot list


def test_invalid_injection_value_is_rejected(tmp_path):
    (tmp_path / "core.toml").write_text('[prompt]\ninjection = "turbo"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(toml_path=tmp_path / "core.toml", overrides_path=tmp_path / "overrides.json")


def test_unknown_prompt_key_is_rejected(tmp_path):
    (tmp_path / "core.toml").write_text('[prompt]\ninjection_mode = "layered"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(toml_path=tmp_path / "core.toml", overrides_path=tmp_path / "overrides.json")


def test_hot_tools_accept_a_list_and_hot_max_is_bounded(tmp_path):
    (tmp_path / "core.toml").write_text(
        '[tools]\nhot = ["fs__read_file"]\nhot_max = 4\n', encoding="utf-8")
    cfg = load_config(toml_path=tmp_path / "core.toml", overrides_path=tmp_path / "overrides.json")
    assert cfg.get("tools.hot") == ["fs__read_file"]
    assert cfg.get("tools.hot_max") == 4
    (tmp_path / "core.toml").write_text("[tools]\nhot_max = 65\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(toml_path=tmp_path / "core.toml", overrides_path=tmp_path / "overrides.json")


def test_default_loop_config_is_legacy_and_defaults_match_the_rfc():
    from vade.loop.engine import LoopConfig

    config = LoopConfig()
    assert config.injection == "legacy"
    assert config.hot_tools == ()
    assert config.hot_max == 12
