"""Configuration loading (C-18).

Priority (later overridden by earlier):  CLI > env > overrides > core.toml > defaults

* Defaults, types, ranges, `hot|restart` and owner come from
  `schema/config.schema.json` — the single source of truth. Nothing here
  hardcodes a number, so C3/C3b/M4a/M4b cannot drift from the contract.
* Unknown keys abort startup with UNKNOWN_CONFIG_KEY. A typo must never fall
  back to a default silently.
* Secrets are env-var NAMES only. A literal `sk-...` in core.toml aborts.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping

from ._jsonschema import SchemaError, validate
from .errors import VadeError

__all__ = ["Config", "ConfigError", "load_config", "SCHEMA_PATH", "ENV_PREFIX"]

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "config.schema.json"
ENV_PREFIX = "VADE_"
ENV_NESTING = "__"

_SECRET_KEY_NAMES = {"api_key", "apikey", "secret", "client_secret", "password", "token", "access_token"}
_SECRET_VALUE_PREFIXES = ("sk-", "sk_", "ghp_", "xoxb-", "AKIA")


class ConfigError(VadeError):
    def __init__(self, key: str, message: str, *, code: str = "UNKNOWN_CONFIG_KEY") -> None:
        self.key = key
        super().__init__(code, f"{key}: {message}")


@dataclass(frozen=True, slots=True)
class Config:
    values: Mapping[str, Any]
    schema: Mapping[str, Any]

    # ---------------------------------------------------------------- access
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.values
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> Mapping[str, Any]:
        value = self.values.get(name, {})
        return value if isinstance(value, Mapping) else {}

    def flat(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        _flatten(self.values, "", out)
        return out

    # ---------------------------------------------------------------- metadata
    def meta(self, dotted: str) -> Mapping[str, Any]:
        node: Mapping[str, Any] = self.schema
        for part in dotted.split("."):
            props = node.get("properties", {})
            if part not in props:
                return {}
            node = props[part]
        return node.get("x-vade", {})

    def apply_of(self, dotted: str) -> str:
        return str(self.meta(dotted).get("apply", "restart"))

    def owner_of(self, dotted: str) -> str:
        return str(self.meta(dotted).get("owner", "?"))

    def requires_confirm(self, dotted: str) -> bool:
        return bool(self.meta(dotted).get("confirm_required", False))

    def dumps(self) -> str:
        return json.dumps(self.values, sort_keys=True, indent=2, ensure_ascii=False)


def _flatten(node: Mapping[str, Any], prefix: str, out: MutableMapping[str, Any]) -> None:
    for key, value in node.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            _flatten(value, dotted, out)
        else:
            out[dotted] = value


def _defaults(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Materialise every key from the schema's `default`."""
    out: dict[str, Any] = {}
    for section, sub in schema.get("properties", {}).items():
        block: dict[str, Any] = {}
        for key, leaf in sub.get("properties", {}).items():
            block[key] = json.loads(json.dumps(leaf.get("default"))) if "default" in leaf else None
        out[section] = block
    return out


def _coerce(value: Any, leaf: Mapping[str, Any], key: str) -> Any:
    expected = leaf.get("type")
    types = expected if isinstance(expected, list) else [expected] if expected else []
    if "boolean" in types and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(key, f"expected boolean, got {value!r}", code="INVALID_ARGUMENT")
    if "integer" in types and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            raise ConfigError(key, f"expected integer, got {value!r}", code="INVALID_ARGUMENT") from None
    if "number" in types and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            raise ConfigError(key, f"expected number, got {value!r}", code="INVALID_ARGUMENT") from None
    if "array" in types and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            raise ConfigError(key, f"expected JSON array, got {value!r}", code="INVALID_ARGUMENT") from None
    return value


def _leaf_schema(schema: Mapping[str, Any], dotted: str) -> Mapping[str, Any] | None:
    node: Mapping[str, Any] = schema
    for part in dotted.split("."):
        if part == "tool_risk":
            pass
        props = node.get("properties")
        if not props or part not in props:
            # Free-form tables (policy.tool_risk.<module>.<tool>) have no leaf.
            return None
        node = props[part]
    return node


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any], *, source: str, schema: Mapping[str, Any], prefix: str = "") -> None:
    """Merge overlay into base, rejecting unknown keys with a dotted path."""
    for key, value in overlay.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if key in base and isinstance(base[key], Mapping):
            if not isinstance(value, Mapping):
                raise ConfigError(dotted, f"{source}: expected a table, got {type(value).__name__}", code="INVALID_ARGUMENT")
            _deep_merge(base[key], value, source=source, schema=schema, prefix=dotted)
        elif key in base:
            leaf = _leaf_schema(schema, dotted) or {}
            base[key] = _coerce(value, leaf, dotted)
        else:
            raise ConfigError(dotted, f"unknown key in {source} (spelling errors are not defaulted)")


def _from_env(schema: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX) or ENV_NESTING not in name:
            continue
        dotted = name[len(ENV_PREFIX):].lower().replace(ENV_NESTING, ".")
        node: MutableMapping[str, Any] = out
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[parts[-1]] = raw
    return out


def _check_secrets(values: Mapping[str, Any], schema: Mapping[str, Any], prefix: str = "") -> None:
    """No literal credentials anywhere in core.toml/overrides/env."""
    for key, value in values.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            _check_secrets(value, schema, dotted)
            continue
        if key.endswith("_env"):
            continue  # env var NAME, allowed
        if key.lower() in _SECRET_KEY_NAMES and isinstance(value, str) and value:
            raise ConfigError(
                dotted,
                "secret literals are forbidden; put the value in an environment variable and use <name>_env = \"ENV_VAR_NAME\"",
                code="INVALID_ARGUMENT",
            )
        if isinstance(value, str) and value.startswith(_SECRET_VALUE_PREFIXES):
            raise ConfigError(dotted, "value looks like a credential; store it in an environment variable", code="INVALID_ARGUMENT")


def _check_origins(cfg: Config) -> None:
    hosts = {str(h) for h in (cfg.get("server.listen_hosts") or [])}
    for origin in cfg.get("server.ui_origins") or []:
        if origin == "*":
            raise ConfigError("server.ui_origins", "'*' is forbidden: cookies with SameSite=Strict need an exact origin", code="INVALID_ARGUMENT")
        host = str(origin).split("://", 1)[-1].split("/", 1)[0]
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if host not in hosts:
            raise ConfigError(
                "server.ui_origins",
                f"origin host {host!r} is not in server.listen_hosts {sorted(hosts)}; "
                "SameSite=Strict cookies are not sent cross-site, so login would succeed and every request 401",
                code="INVALID_ARGUMENT",
            )


def load_config(
    *,
    toml_path: Path | str | None = None,
    overrides_path: Path | str | None = None,
    cli: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    schema_path: Path | str | None = None,
) -> Config:
    schema = json.loads(Path(schema_path or SCHEMA_PATH).read_text(encoding="utf-8"))
    merged = _defaults(schema)

    if toml_path is not None and Path(toml_path).exists():
        with open(toml_path, "rb") as fh:
            _deep_merge(merged, tomllib.load(fh), source="core.toml", schema=schema)

    overrides_target = Path(overrides_path or merged["core"]["data_dir"] + "/config.overrides.json")
    if overrides_target.exists():
        data = json.loads(overrides_target.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ConfigError("overrides", "root must be an object", code="INVALID_ARGUMENT")
        _deep_merge(merged, data, source="overrides.json", schema=schema)

    env_overlay = _from_env(schema, environ if environ is not None else os.environ)
    if env_overlay:
        _deep_merge(merged, env_overlay, source="env", schema=schema)

    if cli:
        overlay: dict[str, Any] = {}
        for dotted, value in cli.items():
            if value is None:
                continue
            node: MutableMapping[str, Any] = overlay
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})  # type: ignore[assignment]
            node[parts[-1]] = value
        _deep_merge(merged, overlay, source="cli", schema=schema)

    _check_secrets(merged, schema)

    try:
        validate(merged, schema)
    except SchemaError as exc:
        raise ConfigError(exc.pointer.lstrip("/") or "config", exc.message, code="INVALID_ARGUMENT") from None

    cfg = Config(values=merged, schema=schema)
    _check_origins(cfg)
    return cfg
