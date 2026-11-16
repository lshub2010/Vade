"""Prompt slots: order, stability, budgets, markers, truncation, secrets."""
from __future__ import annotations

from pathlib import Path

from vade.loop.prompt import PromptParts, apply_hooks, assemble
from vade.skill.render import CATALOG_HEADER, render_bodies, render_catalog

from .helpers import make_loader, write_skill


def _parts(message: str = "") -> PromptParts:
    return PromptParts(persona="PERSONA", tool_block="TOOLS", session_memory="MEMORY",
                       context="CONTEXT", user_message=message)


def test_injection_order_is_fixed(tmp_path: Path) -> None:
    write_skill(tmp_path, "kb-guide")
    loader = make_loader(tmp_path)
    parts = loader.render(_parts("@kb-guide"))
    messages = assemble(parts)
    assert [m.id for m in messages] == [
        "prompt-0-persona", "prompt-1-tool_block", "prompt-2-skill_catalog",
        "prompt-3-skill_bodies", "prompt-4-session_memory", "prompt-5-context",
    ]
    assert messages[2].content.startswith(CATALOG_HEADER)
    assert messages[3].content.startswith("## skill: kb-guide")


def test_same_input_is_byte_identical(tmp_path: Path) -> None:
    for name in ("zeta", "alpha", "mid"):
        write_skill(tmp_path, name, triggers=("go",))
    loader = make_loader(tmp_path)
    first = [m.content for m in assemble(loader.render(_parts("go")))]
    second = [m.content for m in assemble(loader.render(_parts("go")))]
    assert first == second


def test_catalog_is_sorted_by_name(tmp_path: Path) -> None:
    for name in ("zeta", "alpha", "mid"):
        write_skill(tmp_path, name)
    loader = make_loader(tmp_path)
    lines = loader.render(_parts()).skill_catalog.splitlines()[1:]
    assert [line.split(":")[0] for line in lines] == ["- alpha", "- mid", "- zeta"]


def test_unavailable_skill_is_catalog_only(tmp_path: Path) -> None:
    write_skill(tmp_path, "kb-guide", requires=["mcp-bridge__s_kb_recall"])
    loader = make_loader(tmp_path, tools=[])
    parts = loader.render(_parts("@kb-guide"))
    assert "[unavailable]" in parts.skill_catalog
    assert parts.skill_bodies == ""
    assert parts.activated_skills == ()
    assert any("skill unavailable: kb-guide" in w for w in loader.warnings)


def test_stale_and_disabled_skills_are_marked_and_not_injected(tmp_path: Path) -> None:
    write_skill(tmp_path, "stale-one", triggers=("go",))
    write_skill(tmp_path, "off-one", triggers=("go",))
    loader = make_loader(tmp_path)
    loader.set_status("stale-one", "stale")
    loader.set_status("off-one", "disabled")
    parts = loader.render(_parts("go"))
    assert "[stale]" in parts.skill_catalog
    assert "[disabled]" in parts.skill_catalog
    assert parts.skill_bodies == ""


def test_only_three_bodies_are_injected(tmp_path: Path) -> None:
    for name in ("aa", "bb", "cc", "dd", "ee"):
        write_skill(tmp_path, name, triggers=("go",))
    loader = make_loader(tmp_path)
    parts = loader.render(_parts("go"))
    assert parts.activated_skills == ("aa", "bb", "cc")
    assert parts.skill_bodies.count("## skill:") == 3
    assert len(parts.skill_catalog.splitlines()) == 6  # header + 5


def test_pending_never_reaches_the_prompt(tmp_path: Path) -> None:
    from .helpers import skill_text

    directory = tmp_path / "pending" / "draft"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(skill_text("draft"), encoding="utf-8")
    loader = make_loader(tmp_path)
    parts = loader.render(_parts("@draft"))
    assert parts.skill_catalog == ""
    assert parts.skill_bodies == ""
    assert parts.activated_skills == ()


def test_truncated_body_keeps_its_hint_in_the_prompt(tmp_path: Path) -> None:
    write_skill(tmp_path, "long", body_suffix="y" * 12_000)
    loader = make_loader(tmp_path)
    parts = loader.render(_parts("@long"))
    assert "[... truncated at 8192 chars" in parts.skill_bodies
    assert "use fs to read" in parts.skill_bodies


def test_l0_budget_drops_whole_lines_never_name_only() -> None:
    class Row:
        def __init__(self, name: str, description: str) -> None:
            self.name = name
            self.description = description
            self.marker = ""

    rows = [Row("alpha", "a" * 80), Row("beta", "b" * 80), Row("gamma", "g" * 80)]
    text, dropped = render_catalog(rows, [], token_budget=40)  # 160 chars: header + one line
    kept = text.splitlines()[1:]
    assert dropped == ("beta", "gamma")
    assert all(": " in line and len(line) > 20 for line in kept)


def test_l0_priority_is_activated_then_hit_count_then_name() -> None:
    """docs/agents/23 §T3: `(激活优先, hit_count 降序, name 字典序)` decides the cut."""
    class Row:
        def __init__(self, name: str) -> None:
            self.name = name
            self.description = "d" * 40
            self.marker = ""

    rows = [Row("alpha"), Row("beta"), Row("gamma")]

    # 120 chars: header + one 49-char line. The activated skill wins outright.
    text, dropped = render_catalog(rows, ["gamma"], token_budget=30, hits={"beta": 9})
    assert [line.split(":")[0] for line in text.splitlines()[1:]] == ["- gamma"]
    assert dropped == ("alpha", "beta")

    # 160 chars: activation, then the trigger hit count (beta over alpha).
    text, dropped = render_catalog(rows, ["gamma"], token_budget=40, hits={"beta": 2})
    assert [line.split(":")[0] for line in text.splitlines()[1:]] == ["- beta", "- gamma"]
    assert dropped == ("alpha",)

    # No hits: the remainder falls back to name order, byte-stable.
    text, dropped = render_catalog(rows, [], token_budget=40)
    assert [line.split(":")[0] for line in text.splitlines()[1:]] == ["- alpha", "- beta"]
    assert dropped == ("gamma",)


def test_l1_budget_cuts_from_the_priority_tail_and_marks_truncated() -> None:
    class Row:
        def __init__(self, name: str) -> None:
            self.name = name
            self.body = "b" * 6000
            self.path = f"/data/skills/{name}/SKILL.md"

    rows = [Row("alpha"), Row("beta"), Row("gamma")]
    text, injected, dropped = render_bodies(rows, ["alpha", "beta", "gamma"], budget_chars=9000)
    assert injected == ("alpha", "beta")
    assert dropped == ("gamma",)
    assert "truncated" in text
    assert len(text) <= 9000


def test_render_bodies_never_embeds_the_absolute_skill_path() -> None:
    """The prompt must not disclose the host's data-dir layout."""
    class Row:
        def __init__(self, name: str) -> None:
            self.name = name
            self.body = "b" * 6000
            self.path = f"/home/alice/.vade/data/skills/{name}/SKILL.md"

    text, injected, dropped = render_bodies([Row("alpha")], ["alpha"], budget_chars=1000)
    assert injected == ("alpha",) and dropped == ()
    assert "truncated" in text
    assert "/home/alice" not in text and "/data/skills" not in text


def test_a_truncated_body_hint_hands_out_a_relative_reference(tmp_path: Path) -> None:
    write_skill(tmp_path, "long", body_suffix="y" * 12_000)
    loader = make_loader(tmp_path)
    parts = loader.render(_parts("@long"))
    assert "use fs to read long/SKILL.md" in parts.skill_bodies
    assert str(tmp_path) not in parts.skill_bodies


def test_secret_warning_does_not_rewrite_the_body(tmp_path: Path) -> None:
    fake_key = "sk-" + "A" * 24
    write_skill(tmp_path, "leaky", body_suffix=f"Use {fake_key} as an example.")
    loader = make_loader(tmp_path)
    assert any("skill secret pattern: leaky" in w for w in loader.warnings)
    assert loader.secret_hits >= 1
    parts = loader.render(_parts("@leaky"))
    assert fake_key in parts.skill_bodies


def test_hook_chain_is_async_safe(tmp_path: Path) -> None:
    import asyncio

    write_skill(tmp_path, "kb-guide")
    loader = make_loader(tmp_path)
    result = asyncio.run(apply_hooks(_parts("@kb-guide"), [loader.render]))
    assert result.activated_skills == ("kb-guide",)
