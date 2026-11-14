"""Catalog message + layered bodies (RFC-0003 §4.2) — A3, A4, A10.

A3: digest unchanged -> nothing is sent; a description change -> one full
replacement; an emptied catalog -> the retirement entry that names itself.
A4: the catalog message is a user-role message, and layered `assemble()` never
leaks `available_skills` into the system block — even when a legacy hook still
fills the old slot.
A10: in layered mode a loaded body lives in the user-role `<skill_content>`
message, never in a system section.
"""

from __future__ import annotations

import asyncio

from vade.loop.catalog import parse_catalog_digest
from vade.loop.coretools import SKILL_LOAD_NAME, build_core_tools
from vade.loop.prompt import PromptParts, assemble
from vade.skill.loader import SkillLoader

from .helpers import write_skill


class _Cfg:
    def __init__(self, values: dict) -> None:
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


def _loader(tmp_path) -> SkillLoader:
    loader = SkillLoader(tmp_path / "skills", config=_Cfg({"prompt.injection": "layered"}))
    loader.reload()
    return loader


def test_a3_no_resend_when_digest_unchanged(tmp_path):
    write_skill(tmp_path / "skills", "kb-guide")
    loader = _loader(tmp_path)
    states = loader.catalog()
    digest = loader.catalog_digest(states)
    message = loader.catalog_message(states, last=None)
    assert message is not None and parse_catalog_digest(message) == digest
    # Same digest -> explicitly None (§5.3: `None` = do not resend).
    assert loader.catalog_message(states, last=digest) is None
    # Rewording the envelope can never change the digest (rule 3 of §4.2.1):
    assert loader.catalog_digest(states) == digest


def test_a3_description_change_replaces_wholesale(tmp_path):
    write_skill(tmp_path / "skills", "kb-guide", description="First summary")
    loader = _loader(tmp_path)
    first_digest = loader.catalog_digest(loader.catalog())

    write_skill(tmp_path / "skills", "kb-guide", description="Second summary")
    loader.reload()
    states = loader.catalog()
    second_digest = loader.catalog_digest(states)
    assert first_digest != second_digest
    message = loader.catalog_message(states, last=first_digest)
    assert message is not None and "Second summary" in message
    assert parse_catalog_digest(message) == second_digest


def test_a3_empty_catalog_sends_the_retirement_entry(tmp_path):
    write_skill(tmp_path / "skills", "kb-guide")
    loader = _loader(tmp_path)
    filled = loader.catalog_message(loader.catalog(), last=None)
    assert filled is not None

    loader.uninstall("kb-guide")
    states = loader.catalog()
    assert states == ()
    empty_digest = loader.catalog_digest(states)
    message = loader.catalog_message(states, last=None)
    assert message is not None
    assert "<available_skills>\n</available_skills>" in message
    assert "ignore any skill names" in message
    assert empty_digest != parse_catalog_digest(filled)


def test_a4_catalog_message_is_user_role_and_assemble_never_leaks_it():
    from vade.loop.tools import ToolEntry, ToolSnapshot

    entry = ToolEntry(
        llm_name="fs__read_file", tool_id="com.example.fs/read_file", module_id="m",
        description="read", input_schema={"type": "object", "properties": {}},
        risk="low", timeout_ms=1000, idempotent=True,
    )
    snapshot = ToolSnapshot(entries=(entry,))

    async def hook(parts):
        from dataclasses import replace

        # A hook that still speaks legacy: fill BOTH old slots.
        return replace(parts, skill_catalog="Skills (activate with @name):\n- kb-guide: rules",
                       skill_bodies="## skill: kb-guide\nbody")

    parts = PromptParts(persona="You are Vade.", tool_block=snapshot.render_block(), user_message="hello")
    parts = asyncio.run(_apply(parts, [hook]))
    system = assemble(parts, injection="layered")
    assert all(m.role == "system" for m in system)
    assert "available_skills" not in "\n".join(m.content for m in system)
    assert "kb-guide" not in "\n".join(m.content for m in system)


async def _apply(parts, hooks):
    from vade.loop.prompt import apply_hooks

    return await apply_hooks(parts, hooks)


def test_a10_layered_body_lives_in_the_user_message_not_in_system(tmp_path):
    write_skill(tmp_path / "skills", "kb-guide", body_prefix="KB procedure. ")
    loader = _loader(tmp_path)

    parts = PromptParts(persona="You are Vade.", tool_block="tools", user_message="please @kb-guide help")
    layered = loader.render(parts)

    # §5.3: the two system slots stay empty; the body rides skill_content.
    assert layered.skill_catalog == "" and layered.skill_bodies == ""
    assert "<skill_content" in layered.skill_content
    assert "KB procedure." in layered.skill_content
    assert layered.activated_skills == ("kb-guide",)

    system = assemble(layered, injection="layered")
    system_text = "\n".join(m.content for m in system if m.role == "system")
    assert "KB procedure." not in system_text  # never in a system section
    user_messages = [m for m in system if m.role == "user"]
    assert len(user_messages) == 1
    assert "KB procedure." in user_messages[0].content
    assert 'name="kb-guide"' in user_messages[0].content


def test_a10_trigger_hits_do_not_inject_a_body(tmp_path):
    write_skill(tmp_path / "skills", "kb-guide", triggers=["history"], body_prefix="KB procedure. ")
    loader = _loader(tmp_path)

    parts = PromptParts(persona="You are Vade.", tool_block="tools", user_message="tell me about history")
    layered = loader.render(parts)
    assert layered.skill_content == ""          # no body injection
    assert layered.activated_skills == ()       # not counted as injected
    matched = loader.catalog_matches("tell me about history")
    assert matched == ("kb-guide",)             # but the catalog marks it


def test_skill_load_returns_a_fresh_body_with_revision(tmp_path):
    write_skill(tmp_path / "skills", "kb-guide", body_prefix="Revision marker. ")
    loader = _loader(tmp_path)
    tools = build_core_tools(loader)
    tool = tools[SKILL_LOAD_NAME]

    async def scenario():
        raw = await tool.handler({"name": "kb-guide"}, None, session_id="s", turn_id="t1")
        assert raw["ok"] is True
        result = raw["result"]
        assert result["skill"] == "kb-guide" and result["revision"] == 0
        assert result["truncated"] is False
        assert "Revision marker." in result["body"]
        assert result["requires_tools"] == []

        # Fresh read: edit the body on disk, load again, see new bytes — no
        # version bump, no catalog refresh (§4.2.4).
        write_skill(tmp_path / "skills", "kb-guide", body_prefix="Edited marker. ")
        raw2 = await tool.handler({"name": "kb-guide"}, None, session_id="s", turn_id="t1")
        assert "Edited marker." in raw2["result"]["body"]

        # Unknown name -> a C-04 error envelope, never a crash.
        bad = await tool.handler({"name": "nope"}, None, session_id="s", turn_id="t1")
        assert bad["ok"] is False and bad["code"] == "NOT_FOUND"

    asyncio.run(scenario())


def test_skill_load_per_turn_budget_is_cumulative(tmp_path):
    body = "x" * 2000
    write_skill(tmp_path / "skills", "kb-guide", body_prefix=body[:1900] + "\n")
    loader = _loader(tmp_path)
    tools = build_core_tools(loader, body_budget_chars=3000)
    tool = tools[SKILL_LOAD_NAME]

    async def scenario():
        first = await tool.handler({"name": "kb-guide"}, None, session_id="s", turn_id="t1")
        assert first["ok"] is True
        # Same turn: the remaining budget (3000 - ~2000 chars) still admits a
        # clipped body, marked truncated.
        second = await tool.handler({"name": "kb-guide"}, None, session_id="s", turn_id="t1")
        assert second["ok"] is True and second["result"]["truncated"] is True
        assert len(second["result"]["body"]) <= 3000
        # Budget spent -> BUDGET_EXCEEDED for the rest of THIS turn only.
        third = await tool.handler({"name": "kb-guide"}, None, session_id="s", turn_id="t1")
        assert third["ok"] is False and third["code"] == "BUDGET_EXCEEDED"
        # Next turn starts fresh.
        fresh = await tool.handler({"name": "kb-guide"}, None, session_id="s", turn_id="t2")
        assert fresh["ok"] is True

    asyncio.run(scenario())
