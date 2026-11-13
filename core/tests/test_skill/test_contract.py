"""C-11: the schema file and the loader must not drift apart."""
from __future__ import annotations

import json

import pytest

from vade._jsonschema import SchemaError, validate
from vade.loop import prompt as loop_prompt
from vade.skill import contract as contract_mod
from vade.skill.contract import SCHEMA_PATH, load_contract, load_schema, validate_frontmatter


def test_schema_file_is_frozen_c11() -> None:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert data["x-vade"]["contract"] == "C-11"
    assert data["x-vade"]["version"] == "1.0.0"
    assert data["x-vade"]["owner"] == "C3b"
    assert data["$id"].endswith("skill.schema.json")


def test_four_sections_are_required_and_ordered() -> None:
    contract = load_contract()
    assert contract.required_sections == ("When to Use", "Procedure", "Pitfalls", "Verification")
    assert load_schema()["x-vade"]["required_sections"] == list(contract.required_sections)


def test_injection_order_matches_the_loop() -> None:
    # The contract may not reorder what `loop.prompt.assemble` already freezes.
    assert load_contract().injection_order == tuple(loop_prompt._SECTIONS)


def test_snapshot_shape_is_exactly_name_revision_status() -> None:
    schema = load_schema()
    entry = {"name": "kb-guide", "revision": 2, "status": "active"}
    validate(entry, {"$ref": "#/$defs/catalog_snapshot"}, root=schema)
    with pytest.raises(SchemaError) as excinfo:
        validate({"name": "kb-guide", "status": "active"}, {"$ref": "#/$defs/catalog_snapshot"}, root=schema)
    assert excinfo.value.pointer == "/revision"
    with pytest.raises(SchemaError) as excinfo:
        validate({**entry, "path": "/root/data/skills/kb-guide"}, {"$ref": "#/$defs/catalog_snapshot"}, root=schema)
    assert excinfo.value.pointer == "/path"


def test_frontmatter_requires_name_description_when_to_use() -> None:
    validate_frontmatter({"name": "kb-guide", "description": "d", "when_to_use": "w"})
    with pytest.raises(SchemaError) as excinfo:
        validate_frontmatter({"name": "kb-guide", "description": "d"})
    assert excinfo.value.pointer == "/when_to_use"


@pytest.mark.parametrize("name", ["UPPER", "a", "with space", "-leading", "x" * 40, "ok.name"])
def test_name_pattern_rejects_non_slugs(name: str) -> None:
    with pytest.raises(SchemaError):
        validate_frontmatter({"name": name, "description": "d", "when_to_use": "w"})


def test_name_pattern_accepts_two_to_32_char_slugs() -> None:
    for name in ("kb-guide", "ab", "a1-b2-c3", "x" * 32):
        validate_frontmatter({"name": name, "description": "d", "when_to_use": "w"})


def test_unknown_frontmatter_key_is_rejected_not_ignored() -> None:
    # A typo like `when-to-use` silently disables activation; fail loudly instead.
    with pytest.raises(SchemaError) as excinfo:
        validate_frontmatter({"name": "kb-guide", "description": "d", "when_to_use": "w", "when-to-use": "w"})
    assert excinfo.value.pointer == "/when-to-use"


def test_description_and_when_to_use_length_caps() -> None:
    with pytest.raises(SchemaError):
        validate_frontmatter({"name": "kb-guide", "description": "d" * 201, "when_to_use": "w"})
    with pytest.raises(SchemaError):
        validate_frontmatter({"name": "kb-guide", "description": "d", "when_to_use": "w" * 301})


def test_requires_tools_shape() -> None:
    validate_frontmatter({
        "name": "kb-guide", "description": "d", "when_to_use": "w",
        "requires_tools": ["mcp-bridge__s_kb_recall", "com.example.fs/read_file"],
    })
    with pytest.raises(SchemaError):
        validate_frontmatter({
            "name": "kb-guide", "description": "d", "when_to_use": "w",
            "requires_tools": ["not a tool name"],
        })


def test_fallback_mirror_matches_the_frozen_schema() -> None:
    """The code fallback may never be *newer* than the contract file."""
    frozen = dict(load_schema()["x-vade"])
    mirror = contract_mod._FALLBACK
    assert mirror["required_sections"] == frozen["required_sections"]
    assert mirror["name_pattern"] == frozen["name_pattern"]
    assert mirror["injection_order"] == frozen["injection_order"]
    assert mirror["max_activated"] == frozen["max_activated"]
    assert mirror["body_max_chars"] == frozen["body_max_chars"]
    assert mirror["body_max_lines"] == frozen["body_max_lines"]
    assert mirror["body_budget_chars"] == frozen["body_budget_chars"]
    assert mirror["l0_token_budget"] == frozen["l0_token_budget"]
    assert mirror["reserved_names"] == frozen["reserved_names"]
    assert mirror["install"] == frozen["install"]


def test_missing_schema_falls_back_instead_of_crashing(tmp_path) -> None:
    contract = load_contract(tmp_path / "nope.json")
    assert contract.required_sections == ("When to Use", "Procedure", "Pitfalls", "Verification")
    assert contract.max_activated == 3
