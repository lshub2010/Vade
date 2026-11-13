"""Scanning, validation, truncation, path safety, reserved directories."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vade.skill.loader import SkillLoader

from .helpers import make_loader, skill_text, write_skill


def test_valid_skill_loads_with_meta_defaults(tmp_path: Path) -> None:
    write_skill(tmp_path, "kb-guide", triggers=["知识库"], requires=["fs/read_file"])
    loader = make_loader(tmp_path, tools=["fs__read_file"])
    state = loader.get("kb-guide")
    assert state is not None
    assert state.status == "active"
    assert state.revision == 0
    assert state.origin == "manual"
    assert state.available is True
    assert state.triggers == ("知识库",)
    assert set(state.sections) == {"When to Use", "Procedure", "Pitfalls", "Verification"}
    assert loader.warnings == ()


@pytest.mark.parametrize("missing", ["When to Use", "Procedure", "Pitfalls", "Verification"])
def test_any_missing_section_is_rejected(tmp_path: Path, missing: str) -> None:
    sections = {
        "When to Use": "a", "Procedure": "b", "Pitfalls": "c", "Verification": "d",
    }
    sections.pop(missing)
    write_skill(tmp_path, "kb-guide", sections=sections)
    loader = make_loader(tmp_path)
    assert loader.get("kb-guide") is None
    assert "kb-guide" not in [s.name for s in loader.list_all()]
    assert loader.invalid["kb-guide"] == f"missing section: {missing}"
    assert any("missing section: kb-guide" in w for w in loader.warnings)


def test_empty_section_counts_as_missing(tmp_path: Path) -> None:
    write_skill(tmp_path, "kb-guide", sections={"When to Use": "a", "Procedure": "b", "Pitfalls": "c", "Verification": "  "})
    loader = make_loader(tmp_path)
    assert loader.get("kb-guide") is None
    assert "Verification" in loader.invalid["kb-guide"]


def test_frontmatter_missing_required_key_is_invalid(tmp_path: Path) -> None:
    text = skill_text("kb-guide").replace("when_to_use: When the user asks about history or decisions\n", "")
    write_skill(tmp_path, "kb-guide", text=text)
    loader = make_loader(tmp_path)
    assert loader.get("kb-guide") is None
    assert "/when_to_use" in loader.invalid["kb-guide"]


def test_name_must_match_directory(tmp_path: Path) -> None:
    write_skill(tmp_path, "kb-guide", text=skill_text("other-name"))
    loader = make_loader(tmp_path)
    assert loader.get("other-name") is None
    assert "name mismatch" in loader.invalid["kb-guide"]


def test_bad_yaml_is_skipped_not_fatal(tmp_path: Path) -> None:
    write_skill(tmp_path, "broken", text="---\nname: [unclosed\n---\nbody\n")
    write_skill(tmp_path, "kb-guide")
    loader = make_loader(tmp_path)
    assert loader.get("broken") is None
    assert loader.get("kb-guide") is not None
    assert any("parse failed: broken" in w for w in loader.warnings)


def test_body_over_8k_is_truncated_with_a_hint(tmp_path: Path) -> None:
    body = "x" * 12_000
    write_skill(tmp_path, "long", body_suffix=body)
    loader = make_loader(tmp_path)
    state = loader.get("long")
    assert state is not None and state.truncated is True
    assert len(state.body) < 12_000
    assert "[... truncated at 8192 chars, use fs to read" in state.body


def test_body_over_200_lines_is_truncated_with_the_documented_line(tmp_path: Path) -> None:
    write_skill(tmp_path, "long", body_suffix="\n".join(f"line {i}" for i in range(300)))
    loader = make_loader(tmp_path)
    state = loader.get("long")
    assert state is not None and state.truncated is True
    assert "[... truncated at 200 lines, use fs to read" in state.body
    assert "line 250" not in state.body


def test_symlink_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(skill_text("evil"), encoding="utf-8")
    root = tmp_path / "skills"
    (root / "evil").mkdir(parents=True)
    os.symlink(outside, root / "evil" / "SKILL.md")
    write_skill(root, "kb-guide")
    loader = make_loader(root)
    assert loader.get("evil") is None
    assert loader.get("kb-guide") is not None
    assert any("escapes the skills root: evil" in w for w in loader.warnings)


def test_reserved_directories_are_never_scanned(tmp_path: Path) -> None:
    for reserved in ("pending", ".history", ".trash", ".rejected"):
        directory = tmp_path / reserved / "hidden-skill"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(skill_text("hidden-skill"), encoding="utf-8")
    write_skill(tmp_path, "kb-guide")
    loader = make_loader(tmp_path)
    assert [s.name for s in loader.list_all()] == ["kb-guide"]
    # `pending/` is a real quarantine area (installs + proposals): visible only
    # with `include_pending=True`, never in the catalog and never injected.
    assert [s.name for s in loader.list_all(include_pending=True)] == ["hidden-skill", "kb-guide"]
    assert loader.get("hidden-skill") is None
    assert loader.render(__import__("vade.loop.prompt", fromlist=["PromptParts"]).PromptParts(
        persona="p", tool_block="t", user_message="@hidden-skill")).skill_bodies == ""


def test_pending_skills_are_listed_only_on_request(tmp_path: Path) -> None:
    directory = tmp_path / "pending" / "draft"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(skill_text("draft"), encoding="utf-8")
    (directory / "meta.json").write_text(json.dumps({"revision": 2, "status": "active", "origin": "proposal"}))
    directory2 = tmp_path / "pending" / "draft2"
    directory2.mkdir(parents=True)
    (directory2 / "SKILL.md").write_text(skill_text("draft2"), encoding="utf-8")
    loader = make_loader(tmp_path)
    assert loader.list_all() == []
    rows = loader.list_all(include_pending=True)
    assert [(r.name, r.status, r.origin, r.revision) for r in rows] == [
        ("draft", "pending", "proposal", 2),
        ("draft2", "pending", "manual", 0),
    ]
    assert loader.get("draft") is None
    assert loader.get("draft", include_pending=True) is not None


def test_references_are_allowed_but_not_scanned(tmp_path: Path) -> None:
    directory = write_skill(tmp_path, "kb-guide")
    (directory / "references").mkdir()
    (directory / "references" / "schema.md").write_text("---\nname: nope\n---\n", encoding="utf-8")
    loader = make_loader(tmp_path)
    assert [s.name for s in loader.list_all()] == ["kb-guide"]


def test_meta_json_revision_and_status_are_honoured(tmp_path: Path) -> None:
    directory = write_skill(tmp_path, "kb-guide")
    (directory / "meta.json").write_text(json.dumps({"revision": 7, "status": "stale", "origin": "install",
                                                     "fail_count": 3}))
    loader = make_loader(tmp_path)
    state = loader.get("kb-guide")
    assert state is not None
    assert (state.revision, state.status, state.origin, state.fail_count) == (7, "stale", "install", 3)
    assert state.marker == "[stale]"


def test_broken_meta_json_falls_back_to_defaults(tmp_path: Path) -> None:
    directory = write_skill(tmp_path, "kb-guide")
    (directory / "meta.json").write_text("{not json")
    loader = make_loader(tmp_path)
    assert loader.get("kb-guide").revision == 0
    assert any("meta.json unreadable" in w for w in loader.warnings)


def test_reload_sees_new_and_removed_skills(tmp_path: Path) -> None:
    write_skill(tmp_path, "one")
    loader = make_loader(tmp_path)
    assert [s.name for s in loader.list_all()] == ["one"]
    write_skill(tmp_path, "two")
    loader.reload()
    assert [s.name for s in loader.list_all()] == ["one", "two"]
    (tmp_path / "one" / "SKILL.md").unlink()
    loader.reload()
    assert [s.name for s in loader.list_all()] == ["two"]


def test_snapshot_is_sorted_and_stable(tmp_path: Path) -> None:
    write_skill(tmp_path, "zeta")
    write_skill(tmp_path, "alpha")
    loader = make_loader(tmp_path)
    first = loader.snapshot()
    loader.reload()
    second = loader.snapshot()
    assert first == second
    assert [e["name"] for e in first["entries"]] == ["alpha", "zeta"]
    assert all(set(e) == {"name", "revision", "status"} for e in first["entries"])
    assert len(first["digest"]) == 64


def test_list_all_does_not_leak_paths(tmp_path: Path) -> None:
    write_skill(tmp_path, "kb-guide")
    loader = make_loader(tmp_path)
    (row,) = loader.list_all()
    assert not hasattr(row, "path")
    assert "tmp" not in json.dumps(row.__dict__ if hasattr(row, "__dict__") else {
        "name": row.name, "description": row.description,
    })


def test_check_rows_are_json_ready(tmp_path: Path) -> None:
    write_skill(tmp_path, "kb-guide", triggers=["知识库"], requires=["missing__tool"])
    loader = make_loader(tmp_path, tools=[])
    (row,) = loader.check()
    assert row["name"] == "kb-guide" and row["marker"] == "[unavailable]"
    json.dumps(row)


def test_invalid_directory_name_is_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "Bad Name"
    directory.mkdir()
    (directory / "SKILL.md").write_text(skill_text("kb-guide"), encoding="utf-8")
    loader = make_loader(tmp_path)
    assert loader.list_all() == []
    assert any("invalid skill directory name" in w for w in loader.warnings)


def test_missing_skills_root_is_empty_not_an_error(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path / "does-not-exist")
    loader.reload()
    assert loader.list_all() == []
    assert loader.render(__import__("vade.loop.prompt", fromlist=["PromptParts"]).PromptParts(
        persona="p", tool_block="t", user_message="@kb-guide")).skill_catalog == ""
