"""The three shipped examples are the delivery-format artefacts; keep them valid.

One usable, one missing a tool, one over the 200-line cap.
"""
from __future__ import annotations

from pathlib import Path

from vade.skill.loader import SkillLoader

EXAMPLES = Path(__file__).resolve().parents[2] / "vade" / "skill" / "examples"


def _loader() -> SkillLoader:
    loader = SkillLoader(EXAMPLES, tool_snapshot=[])
    loader.reload()
    return loader


def test_examples_are_valid_and_cover_the_three_cases() -> None:
    loader = _loader()
    assert loader.invalid == {}
    rows = {row.name: row for row in loader.list_all()}
    assert set(rows) == {"kb-guide", "needs-mcp", "long-form"}
    assert rows["kb-guide"].status == "active" and rows["kb-guide"].available is True
    assert rows["needs-mcp"].available is False
    assert rows["needs-mcp"].unavailable_reason == "missing tool: mcp-bridge__s_kb_recall"
    assert rows["long-form"].status == "active"


def test_long_form_example_is_truncated_at_200_lines() -> None:
    state = _loader().get("long-form")
    assert state.truncated is True
    assert "[... truncated at 200 lines, use fs to read" in state.body
    assert len(state.body.splitlines()) <= 205


def test_needs_mcp_example_is_catalog_only() -> None:
    from vade.loop.prompt import PromptParts

    loader = _loader()
    parts = loader.render(PromptParts(persona="p", tool_block="t", user_message="@needs-mcp 看看外部资料"))
    assert "[unavailable]" in parts.skill_catalog
    assert parts.skill_bodies == ""
