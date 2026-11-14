"""Q5 + U1 (RFC-0003): compaction must not swallow the skill-catalog reminder,
and the stable section must stay byte-identical across compaction.

Q5 (§4.2.2, RFC §10 Q5): the catalog reminder is an ordinary user-role message.
Once it sits deeper than `protect_last_n` in the history, `fit()` drops it from
the fitted view — while `_inject_catalog`'s resend decision (made against the
RAW log, which still holds it) sees the digest as unchanged and sends nothing.
The model then runs with NO visible catalog until the digest next changes.
These tests pin the protection AND prove compaction still compacts everything
else (it must not become a no-op).

U1: byte-stability of the stable section across turns INCLUDING turns where
compaction fires. This is a NECESSARY condition for a provider-side prompt
cache hit, not a sufficient one — real hit rates still need a real provider.
"""

from __future__ import annotations

import asyncio
import hashlib

from vade.llm.base import Delta
from vade.llm.mock import MockLLMProvider
from vade.loop.catalog import (
    CatalogEntry,
    catalog_message,
    is_catalog_message,
    parse_catalog_digest,
)
from vade.loop.context import CompactionConfig, ContextBuilder
from vade.loop.coretools import build_core_tools
from vade.loop.engine import LoopConfig, LoopEngine
from vade.loop.tools import ToolEntry, ToolSnapshot
from vade.session.models import Message, Turn
from vade.skill.loader import SkillLoader
from vade.skill.port import LayeredCatalogPort

from ..test_skill.helpers import write_skill


class _Cfg:
    """Minimal C-18 stand-in: the loader reads `prompt.injection` from it."""

    def __init__(self, values: dict) -> None:
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)


def _layered_loader(tmp_path) -> SkillLoader:
    loader = SkillLoader(tmp_path / "skills", config=_Cfg({"prompt.injection": "layered"}))
    loader.reload()
    return loader


def _snapshot() -> ToolSnapshot:
    entry = ToolEntry(
        llm_name="fs__read_file", tool_id="com.example.fs/read_file", module_id="com.example.fs",
        description="read a file", input_schema={"type": "object", "properties": {}},
        risk="low", timeout_ms=2000, idempotent=True,
    )
    return ToolSnapshot(entries=(entry,))


class _Router:
    async def call(self, tool_id, args, **kwargs):
        from vade.router.dispatch import ToolCallResult

        return ToolCallResult(True, tool_id, result={"content": "hi"})


class _Recorder:
    """Collects emitted events AND their persisted messages, and serves them
    back as the next turn's history — the session log's job, in miniature."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.messages: list = []

    async def __call__(self, session_id, event_type, payload=None, messages=(), **kw):
        self.events.append((event_type, dict(payload or {})))
        self.messages.extend(messages or ())

    def history(self, session_id):
        return tuple(self.messages)


def _engine(
    loader: SkillLoader,
    recorder: _Recorder,
    llm: MockLLMProvider,
    *,
    ctx_tokens: int = 32_000,
    compaction: CompactionConfig | None = None,
    injection: str = "layered",
) -> LoopEngine:
    layered = injection == "layered"
    return LoopEngine(
        _Router(), llm, _snapshot, recorder,
        history=recorder.history,
        prompt_hooks=lambda: [loader.render],
        config=LoopConfig(injection=injection, ctx_tokens=ctx_tokens, max_steps=2),
        compaction=compaction,
        core_tools=build_core_tools(loader) if layered else None,
        catalog=LayeredCatalogPort(loader) if layered else None,
        memory=None,
    )


def _seed(recorder: _Recorder, count: int, *, start: int = 0) -> None:
    """Ordinary long history traffic: alternating user/assistant messages.

    Assistants get trimmed by fit() step 2, users are dropped wholesale by
    step 3 — together they exercise both compaction attacks.
    """
    for i in range(start, start + count):
        recorder.messages.append(Message(
            id=f"seed-{i}", session_id="s1", turn_id=f"old-{i}",
            role="assistant" if i % 2 else "user",
            content=f"SEED-{i:02d} " + "x" * 600, created_at=0,
        ))


def _catalog_texts(recorder: _Recorder) -> list[str]:
    return [m.content for m in recorder.messages if is_catalog_message(m.content)]


def _reminders(call: dict) -> list[dict]:
    return [m for m in call["messages"] if is_catalog_message(str(m.get("content", "")))]


def _stable_digest(call: dict) -> str:
    """sha256 over the stable prefix: every system section EXCEPT the
    runtime-facts context block (the same view A1/A2 pin in test_prompt_layers)."""
    blob = []
    for message in call["messages"]:
        if message.get("role") != "system":
            continue
        content = str(message.get("content", ""))
        if content.startswith("Runtime facts"):
            continue
        blob.append(content)
    return hashlib.sha256(repr(blob).encode("utf-8")).hexdigest()


async def _run_turn(engine: LoopEngine, recorder: _Recorder, llm: MockLLMProvider, turn_id: str) -> dict:
    calls_before = len(llm.calls)
    await engine.run_turn(Turn(id=turn_id, session_id="s1", user_text="hello"), ())
    assert len(llm.calls) == calls_before + 1
    return llm.calls[-1]


def _no_window() -> CompactionConfig:
    """protect_last_n=0 / protect_first_n=0: ONLY current-turn and explicitly
    protected messages survive, so a lost protection is immediately visible."""
    return CompactionConfig(protect_last_n=0, protect_first_n=0)


# ----------------------------------------------------------------- Q5: engine


def test_compaction_keeps_the_catalog_reminder_visible(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
        engine = _engine(loader, recorder, llm, ctx_tokens=400, compaction=_no_window())

        await _run_turn(engine, recorder, llm, "t1")  # catalog injected here
        sent = _catalog_texts(recorder)
        assert len(sent) == 1
        original = sent[0]

        _seed(recorder, 6, start=10)  # ordinary traffic pushed past the reminder
        second = await _run_turn(engine, recorder, llm, "t2")  # compaction fires

        # Digest unchanged -> no resend (§4.2.2); the reminder must STILL reach
        # the model through the fitted view, byte for byte:
        assert len([p for t, p in recorder.events if t == "turn.skill_catalog"]) == 1
        reminders = _reminders(second)
        assert len(reminders) == 1
        assert reminders[0]["content"] == original

        # ...and compaction was NOT turned into a no-op by the protection:
        contents = [str(m.get("content", "")) for m in second["messages"]]
        assert sum(c.startswith("SEED-") for c in contents) == 0

    asyncio.run(scenario())


def test_compaction_still_compacts_ordinary_history(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
        engine = _engine(loader, recorder, llm, ctx_tokens=400, compaction=_no_window())
        await _run_turn(engine, recorder, llm, "t1")

        _seed(recorder, 6, start=10)
        history_size = len(recorder.messages)
        second = await _run_turn(engine, recorder, llm, "t2")

        non_system = [m for m in second["messages"] if m.get("role") != "system"]
        # Six seed messages went in; compaction physically removed them while
        # the protected reminder survived:
        assert len(non_system) == 1
        assert len(non_system) < history_size
        assert all(is_catalog_message(str(m.get("content", ""))) for m in non_system)

    asyncio.run(scenario())


def test_retirement_message_survives_compaction(tmp_path):
    async def scenario():
        loader = _layered_loader(tmp_path)  # NO skills: the retirement entry
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
        engine = _engine(loader, recorder, llm, ctx_tokens=400, compaction=_no_window())

        await _run_turn(engine, recorder, llm, "t1")
        sent = _catalog_texts(recorder)
        assert len(sent) == 1 and "No skills are available in this session" in sent[0]
        original = sent[0]

        _seed(recorder, 6, start=10)
        second = await _run_turn(engine, recorder, llm, "t2")

        reminders = _reminders(second)
        assert len(reminders) == 1
        assert reminders[0]["content"] == original  # full envelope, not truncated
        contents = [str(m.get("content", "")) for m in second["messages"]]
        assert sum(c.startswith("SEED-") for c in contents) == 0

    asyncio.run(scenario())


def test_every_catalog_generation_survives_compaction(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")]] * 3)
        engine = _engine(loader, recorder, llm, ctx_tokens=400, compaction=_no_window())

        await _run_turn(engine, recorder, llm, "t1")
        write_skill(tmp_path / "skills", "api-tips")
        loader.reload()
        await _run_turn(engine, recorder, llm, "t2")  # digest change -> resend

        payloads = [p for t, p in recorder.events if t == "turn.skill_catalog"]
        assert len(payloads) == 2
        generations = _catalog_texts(recorder)
        assert len(generations) == 2

        _seed(recorder, 6, start=10)
        third = await _run_turn(engine, recorder, llm, "t3")  # compaction fires

        reminders = _reminders(third)
        # BOTH generations survive; the last one wins and carries the new digest:
        assert [m["content"] for m in reminders] == generations
        assert "api-tips" in reminders[-1]["content"]
        assert parse_catalog_digest(reminders[-1]["content"]) == payloads[1]["digest"]

    asyncio.run(scenario())


def test_default_protect_window_alone_does_not_save_the_reminder(tmp_path):
    """The REAL configuration (protect_last_n=20): a reminder with >20 messages
    after it sits outside every built-in window. Only the Q5 predicate keeps it."""
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")], [Delta(text="ok")]])
        # Default CompactionConfig on purpose — no test-specific windows:
        engine = _engine(loader, recorder, llm, ctx_tokens=400)

        _seed(recorder, 4, start=0)
        await _run_turn(engine, recorder, llm, "t1")
        original = _catalog_texts(recorder)[0]
        _seed(recorder, 22, start=10)  # reminder is now 23 messages from the end

        second = await _run_turn(engine, recorder, llm, "t2")

        reminders = _reminders(second)
        assert len(reminders) == 1
        assert reminders[0]["content"] == original
        # Compaction still acted: first-3 seeds survive, something was dropped:
        contents = [str(m.get("content", "")) for m in second["messages"]]
        seeds = [c for c in contents if c.startswith("SEED-")]
        assert 0 < len(seeds) < 26

    asyncio.run(scenario())


# ------------------------------------------------------------ U1: stable bytes


def test_stable_section_is_byte_identical_across_five_ordinary_turns(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")]] * 5)
        engine = _engine(loader, recorder, llm)

        calls = [await _run_turn(engine, recorder, llm, f"t{i}") for i in range(1, 6)]
        digests = {_stable_digest(call) for call in calls}
        assert len(digests) == 1
        assert all(call["tools"] == calls[0]["tools"] for call in calls)
        # The catalog was visible in every turn, sent exactly once:
        assert len([p for t, p in recorder.events if t == "turn.skill_catalog"]) == 1
        assert all(len(_reminders(call)) == 1 for call in calls)

    asyncio.run(scenario())


def test_stable_section_is_byte_identical_across_compaction(tmp_path):
    """U1 core: the stable prefix does not move when compaction fires. A
    provider cache can only be hit if these bytes (and the tools array) are
    identical turn over turn — necessary, NOT sufficient, for a real hit."""
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")]] * 4)
        engine = _engine(loader, recorder, llm, ctx_tokens=400, compaction=_no_window())

        _seed(recorder, 6, start=0)  # every turn below compacts
        calls = [await _run_turn(engine, recorder, llm, f"t{i}") for i in range(1, 5)]
        digests = {_stable_digest(call) for call in calls}
        assert len(digests) == 1
        assert all(call["tools"] == calls[0]["tools"] for call in calls)
        # The reminder reached the model in every compacted turn:
        assert all(len(_reminders(call)) == 1 for call in calls)

    asyncio.run(scenario())


def test_digest_change_still_resends_after_compaction(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = _layered_loader(tmp_path)
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")]] * 3)
        engine = _engine(loader, recorder, llm, ctx_tokens=400, compaction=_no_window())

        await _run_turn(engine, recorder, llm, "t1")
        _seed(recorder, 6, start=10)
        await _run_turn(engine, recorder, llm, "t2")  # compaction fires

        write_skill(tmp_path / "skills", "api-tips")
        loader.reload()
        third = await _run_turn(engine, recorder, llm, "t3")  # resend + compact

        payloads = [p for t, p in recorder.events if t == "turn.skill_catalog"]
        assert len(payloads) == 2  # t1 and t3 — compaction did not mute the resend
        reminders = _reminders(third)
        assert len(reminders) == 2
        assert "api-tips" in reminders[-1]["content"]
        assert payloads[0]["digest"] != payloads[1]["digest"]
        assert parse_catalog_digest(reminders[-1]["content"]) == payloads[1]["digest"]

    asyncio.run(scenario())


# --------------------------------------------------- legacy path stays untouched


def test_legacy_path_has_no_reminder_and_still_compacts(tmp_path):
    async def scenario():
        write_skill(tmp_path / "skills", "kb-guide")
        loader = SkillLoader(tmp_path / "skills")  # default injection = legacy
        loader.reload()
        recorder = _Recorder()
        llm = MockLLMProvider([[Delta(text="ok")]])
        engine = _engine(loader, recorder, llm, ctx_tokens=400, injection="legacy",
                         compaction=_no_window())

        _seed(recorder, 6, start=0)
        call = await _run_turn(engine, recorder, llm, "t1")

        # Legacy carries the catalog in the system block, never as a reminder:
        assert _reminders(call) == []
        system_text = "\n".join(
            str(m.get("content", "")) for m in call["messages"] if m.get("role") == "system"
        )
        assert "kb-guide" in system_text
        # Compaction is unchanged: the seeds were fitted away as before Q5:
        contents = [str(m.get("content", "")) for m in call["messages"]]
        assert sum(c.startswith("SEED-") for c in contents) < 6

    asyncio.run(scenario())


# ------------------------------------------------------- Q5: ContextBuilder unit


def _unit_catalog() -> Message:
    text = catalog_message([CatalogEntry("kb-guide", "Project knowledge base rules")], "d" * 64)
    return Message(id="cat", session_id="s1", turn_id="old-9", role="user", content=text, created_at=0)


def test_fit_protect_predicate_spares_the_marked_message():
    builder = ContextBuilder(CompactionConfig(protect_last_n=0, protect_first_n=0))
    catalog = _unit_catalog()
    messages = [
        Message(id=f"u{i}", session_id="s1", turn_id=f"old-{i}", role="user",
                content="u" * 600, created_at=0)
        for i in range(4)
    ]
    out = builder.fit(
        messages + [catalog], ctx_tokens=400,
        protect=lambda m: is_catalog_message(m.content),
    )
    # Every unprotected message dropped; the protected one kept byte for byte:
    assert [m.id for m in out] == ["cat"]
    assert out[-1].content == catalog.content


def test_fit_without_protect_drops_the_catalog_message():
    """The pre-Q5 counterfactual, pinned at unit level: without the predicate
    the fitter happily drops the reminder — this is the defect Q5 fixes, and it
    is what the engine-side mutation check must turn red."""
    builder = ContextBuilder(CompactionConfig(protect_last_n=0, protect_first_n=0))
    catalog = _unit_catalog()
    messages = [
        Message(id=f"u{i}", session_id="s1", turn_id=f"old-{i}", role="user",
                content="u" * 600, created_at=0)
        for i in range(4)
    ]
    out = builder.fit(messages + [catalog], ctx_tokens=400)
    assert out == []


def test_fit_protect_keeps_order_and_later_messages_survive():
    builder = ContextBuilder(CompactionConfig(protect_last_n=0, protect_first_n=0))
    catalog = _unit_catalog()
    before = [
        Message(id=f"b{i}", session_id="s1", turn_id=f"old-{i}", role="user",
                content="u" * 600, created_at=0)
        for i in range(14)
    ]
    after = [
        Message(id=f"a{i}", session_id="s1", turn_id=f"new-{i}", role="user",
                content="v" * 600, created_at=0)
        for i in range(2)
    ]
    out = builder.fit(
        before + [catalog] + after, ctx_tokens=4000,
        protect=lambda m: is_catalog_message(m.content),
    )
    # The budget stopped mid-history: the reminder keeps its place and the
    # newer messages after it were never touched:
    ids = [m.id for m in out]
    assert "cat" in ids
    assert ids.index("cat") < len(ids) - 1
    assert ids[ids.index("cat") + 1:] == ["a0", "a1"]
    assert len(ids) < len(before) + 1 + len(after)  # earlier messages were dropped
    kept_catalog = next(m for m in out if m.id == "cat")
    assert kept_catalog.content == catalog.content


def test_fit_protect_trims_neighbors_but_never_the_reminder():
    builder = ContextBuilder(CompactionConfig(protect_last_n=0, protect_first_n=0,
                                              target_ratio=0.35))
    catalog = _unit_catalog()
    assistants = [
        Message(id=f"a{i}", session_id="s1", turn_id=f"old-{i}", role="assistant",
                content="y" * 600, created_at=0)
        for i in range(6)
    ]
    out = builder.fit(
        assistants + [catalog], ctx_tokens=600,
        protect=lambda m: is_catalog_message(m.content),
    )
    # Step 2 trimmed the assistants in place; the reminder's bytes are intact:
    trimmed = [m for m in out if m.role == "assistant"]
    assert trimmed and all(m.content == "[trimmed]" for m in trimmed)
    kept = [m for m in out if is_catalog_message(m.content)]
    assert len(kept) == 1 and kept[0].content == catalog.content
