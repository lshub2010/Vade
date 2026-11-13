"""`install(app)` wiring: idempotent hooks, tool-failure staleness, no startup crash."""
from __future__ import annotations

import json
from pathlib import Path

from vade.loop.prompt import PromptParts, apply_hooks
from vade.loop.tools import ToolSnapshot
from vade.skill import SkillLoader, install

from .helpers import skill_text, write_skill


class FakeConfig:
    def __init__(self, data_dir: Path) -> None:
        self.values = {"core.data_dir": str(data_dir)}

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class FakeRegistry:
    def __init__(self, tools: tuple[str, ...] = ()) -> None:
        from vade.loop.tools import ToolEntry

        self.entries = tuple(
            ToolEntry(llm_name=name, tool_id=f"com.example.mod/{name}", module_id="com.example.mod",
                      description="d", input_schema={}, risk="low", timeout_ms=1000, idempotent=True)
            for name in tools
        )

    def tool_snapshot(self) -> ToolSnapshot:
        from vade.loop.tools import ToolNameMap

        return ToolSnapshot(entries=self.entries, name_map=ToolNameMap.build(
            [(e.module_id, e.tool_id.split("/")[-1]) for e in self.entries]
        ))


class FakeApp:
    def __init__(self, data_dir: Path, tools: tuple[str, ...] = ()) -> None:
        self.config = FakeConfig(data_dir)
        self.registry = FakeRegistry(tools)
        self.prompt_hooks: list = []
        self.tool_failure_hooks: list = []


def test_install_is_idempotent(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills", "kb-guide")
    app = FakeApp(tmp_path)
    install(app)
    install(app)
    install(app)
    assert len(app.prompt_hooks) == 1
    assert len(app.tool_failure_hooks) == 1
    assert isinstance(app.skill_loader, SkillLoader)
    assert app.skill_loader is app.skill_loader


def test_hook_is_called_by_the_prompt_pipeline(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills", "kb-guide")
    app = FakeApp(tmp_path)
    install(app)
    parts = PromptParts(persona="p", tool_block="t", user_message="@kb-guide")
    result = app.prompt_hooks[0](parts)
    assert result.activated_skills == ("kb-guide",)
    assert "## skill: kb-guide" in result.skill_bodies


def test_tool_failure_marks_stale_after_three(tmp_path: Path) -> None:
    directory = write_skill(tmp_path / "skills", "kb-guide", requires=["mod__read_file"])
    (directory / "meta.json").write_text(json.dumps({"revision": 1, "status": "active"}))
    app = FakeApp(tmp_path, tools=("mod__read_file",))
    install(app)
    loader = app.skill_loader
    assert loader.get("kb-guide").available is True
    loader.record_tool_failure(["mod__read_file"])
    loader.record_tool_failure(["other_tool"])  # unrelated failures do not count
    loader.record_tool_failure(["mod__read_file"])
    assert loader.get("kb-guide").status == "active"
    loader.record_tool_failure(["mod__read_file"])
    state = loader.get("kb-guide")
    assert state.status == "stale" and state.fail_count == 3
    assert "[stale]" in loader.render(PromptParts(persona="p", tool_block="t", user_message="@kb-guide")).skill_catalog
    assert json.loads((directory / "meta.json").read_text())["status"] == "stale"


def test_unrelated_tool_failures_never_touch_a_skill(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills", "kb-guide", requires=["mod__read_file"])
    app = FakeApp(tmp_path, tools=("mod__read_file",))
    install(app)
    for _ in range(5):
        app.skill_loader.record_tool_failure(["mod__other"])
    assert app.skill_loader.get("kb-guide").status == "active"


def test_startup_survives_a_broken_skill_directory(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills", "kb-guide")
    broken = tmp_path / "skills" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("---\nname: [\n---\n", encoding="utf-8")
    app = FakeApp(tmp_path)
    install(app)  # must not raise
    assert app.skill_loader.get("kb-guide") is not None
    assert any("parse failed" in w for w in app.skill_loader.warnings)


def test_render_through_apply_hooks_is_async(tmp_path: Path) -> None:
    import asyncio

    write_skill(tmp_path / "skills", "kb-guide")
    app = FakeApp(tmp_path)
    install(app)
    result = asyncio.run(apply_hooks(PromptParts(persona="p", tool_block="t", user_message="@kb-guide"),
                                     app.prompt_hooks))
    assert result.activated_skills == ("kb-guide",)
