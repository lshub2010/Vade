"""C-11 contract loader: one source of truth for every limit the loader enforces.

`schema/skill.schema.json` is the frozen contract. The loader does not carry its
own copies of the name pattern / section list / install deny-lists: it reads
them from `x-vade`. `core/tests/test_skill/test_contract.py` pins the two
together (plus `loop.prompt._SECTIONS`), so a schema edit that the code does not
follow fails a test instead of silently changing behaviour.

If the schema file is absent (an installed core without the repo checkout), a
built-in mirror keeps the loader working. The mirror is only a fallback: the
drift test asserts it equals the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .._jsonschema import SchemaError, validate

__all__ = ["Contract", "SCHEMA_PATH", "load_contract", "load_schema", "validate_frontmatter", "SchemaError"]

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "skill.schema.json"

#: Mirror of `x-vade` for a core installed without the repo's `schema/`.
#: `test_contract.py::test_fallback_matches_frozen_schema` keeps it honest.
_FALLBACK: dict[str, Any] = {
    "version": "1.0.0",
    "name_pattern": r"^[a-z][a-z0-9-]{1,31}$",
    "required_sections": ["When to Use", "Procedure", "Pitfalls", "Verification"],
    "injection_order": ["persona", "tool_block", "skill_catalog", "skill_bodies", "session_memory", "context"],
    "max_activated": 3,
    "body_max_chars": 8192,
    "body_max_lines": 200,
    "body_budget_chars": 12288,
    "l0_token_budget": 3000,
    "reserved_names": ["pending", ".history", ".trash", ".rejected"],
    "install": {
        "max_entries": 32,
        "max_uncompressed_bytes": 5_242_880,
        "max_skill_md_depth": 2,
        "exactly_one_skill_md": True,
        "forbidden_names": [
            "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "setup.py", "setup.sh", "pyproject.toml", "requirements.txt",
            "Makefile", "Dockerfile", "docker-compose.yml",
        ],
        "forbidden_suffixes": [
            ".sh", ".bash", ".zsh", ".fish", ".py", ".pyc", ".exe", ".dll", ".so",
            ".dylib", ".bin", ".bat", ".cmd", ".ps1", ".com", ".msi", ".jar",
            ".war", ".class", ".wasm", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl",
            ".php", ".lua", ".app", ".run", ".service", ".socket",
        ],
    },
}


@dataclass(frozen=True, slots=True)
class Contract:
    version: str
    name_pattern: str
    required_sections: tuple[str, ...]
    injection_order: tuple[str, ...]
    max_activated: int
    body_max_chars: int
    body_max_lines: int
    body_budget_chars: int
    l0_token_budget: int
    reserved_names: frozenset[str]
    forbidden_names: frozenset[str]
    forbidden_suffixes: tuple[str, ...]
    max_entries: int
    max_uncompressed_bytes: int
    max_skill_md_depth: int


def _from_mapping(data: Mapping[str, Any]) -> Contract:
    install = data.get("install") or {}
    return Contract(
        version=str(data.get("version", "1.0.0")),
        name_pattern=str(data.get("name_pattern", r"^[a-z][a-z0-9-]{1,31}$")),
        required_sections=tuple(data.get("required_sections") or ()),
        injection_order=tuple(data.get("injection_order") or ()),
        max_activated=int(data.get("max_activated", 3)),
        body_max_chars=int(data.get("body_max_chars", 8192)),
        body_max_lines=int(data.get("body_max_lines", 200)),
        body_budget_chars=int(data.get("body_budget_chars", 12288)),
        l0_token_budget=int(data.get("l0_token_budget", 3000)),
        reserved_names=frozenset(data.get("reserved_names") or ()),
        forbidden_names=frozenset(str(n).lower() for n in install.get("forbidden_names") or ()),
        forbidden_suffixes=tuple(str(s).lower() for s in install.get("forbidden_suffixes") or ()),
        max_entries=int(install.get("max_entries", 32)),
        max_uncompressed_bytes=int(install.get("max_uncompressed_bytes", 5_242_880)),
        max_skill_md_depth=int(install.get("max_skill_md_depth", 2)),
    )


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict[str, Any], Contract]:
    p = Path(path)
    if not p.exists():
        return {"x-vade": dict(_FALLBACK)}, _from_mapping(_FALLBACK)
    data = json.loads(p.read_text(encoding="utf-8"))
    if str((data.get("x-vade") or {}).get("contract")) != "C-11":
        raise ValueError(f"{p}: x-vade.contract must be C-11")
    return data, _from_mapping(data["x-vade"])


def load_schema(path: Path | str = SCHEMA_PATH) -> Mapping[str, Any]:
    return _load(str(path))[0]


def load_contract(path: Path | str = SCHEMA_PATH) -> Contract:
    return _load(str(path))[1]


def validate_frontmatter(document: Mapping[str, Any], *, path: Path | str = SCHEMA_PATH) -> None:
    """Raise `SchemaError` (with a JSON Pointer) on the first violation."""
    validate(dict(document), load_schema(path))
