"""RFC-0003 layered wiring through the REAL component assembly.

`install_components` installs alphabetically, so the `session` component lands
BEFORE `skill`: the layered wiring (core tools + catalog port) must resolve
`app.skill_loader` lazily, at call time — capturing it at install time would
wire `None` forever. This test runs the same two calls `__main__._serve` makes
(`App.startup()` + `install_components`) with `prompt.injection=layered`, then
installs a skill AFTER assembly and proves the catalog message reaches the
model, `skill_load` answers with the body, and `turn.skill_catalog` is both
persisted in the store and delivered to a connected client.
"""

from __future__ import annotations

import pytest

from vade.app import App, install_components
from vade.config import load_config
from vade.llm.base import Delta, ToolCallDelta
from vade.llm.mock import MockLLMProvider
from tests.session.helpers import CollectSink, wait_until
from tests.test_skill.helpers import write_skill


def _tool_names(call: dict) -> list[str]:
    return [t["function"]["name"] for t in call["tools"]]


@pytest.mark.asyncio
async def test_layered_wiring_survives_the_session_before_skill_install_order(tmp_path):
    config = load_config(cli={"core.data_dir": str(tmp_path), "prompt.injection": "layered"})
    app = App(config)
    await app.startup()
    # The same two calls `__main__._serve` makes. Session installs before skill,
    # so the layered wiring can only see the loader lazily.
    install_components(app, strict=False)
    assert app.config.get("prompt.injection") == "layered"
    assert app.skill_loader is not None

    # The skill arrives AFTER assembly: a lazy provider sees it, a loader
    # captured at wiring time (None then) never would.
    write_skill(tmp_path / "skills", "kb-guide")
    app.skill_loader.reload()

    # The loop reads `self.llm.chat` per call; `app.llm` is only the wiring
    # handle, so the scripted provider must replace the ENGINE's provider.
    llm = MockLLMProvider([
        [Delta(tool_calls=(ToolCallDelta(index=0, id="c1", name="skill_load",
                                         arguments='{"name": "kb-guide"}'),))],
        [Delta(text="loaded")],
    ])
    app.llm = llm
    app.loop.llm = llm

    session = app.session.create_session()
    sink = CollectSink()
    await app.session.attach(session.id, None, sink)
    await app.session.submit(session.id, [{"type": "text", "text": "hello"}], "c1")
    await app.session.wait_idle(session.id)

    # The turn stream is durable: read the store (not just the live sink,
    # whose delivery is asynchronous).
    events = app.session_store.read_since(session.id, 0)
    types = [e.type for e in events]
    assert "turn.skill_catalog" in types
    catalog = [e for e in events if e.type == "turn.skill_catalog"]
    assert catalog[0].payload["entries"] == ["kb-guide"]
    assert catalog[0].payload["empty"] is False
    assert catalog[0].payload["digest"]

    # The model called skill_load and the core tool answered ok.
    tool_calls = [e for e in events if e.type == "turn.tool_call"]
    assert any(c.payload["tool"] == "skill_load" for c in tool_calls)
    tool_results = [e for e in events if e.type == "turn.tool_result"]
    assert tool_results and tool_results[-1].payload["ok"] is True

    # Contract on the wire: the connected client saw the catalog live too.
    assert await wait_until(lambda: "turn.skill_catalog" in sink.types())

    # The model actually saw the layer: the core tools are resident schemas,
    # the catalog reminder is a user message, and the loaded body came back
    # as a tool result inside the second call's context.
    assert "skill_load" in _tool_names(llm.calls[0])
    reminders = [
        m for m in llm.calls[0]["messages"]
        if m.get("role") == "user" and str(m.get("content", "")).startswith("<system-reminder>")
    ]
    assert reminders and "`kb-guide`" in reminders[0]["content"]
    tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and "Query the knowledge base." in tool_messages[-1]["content"]

    await app.session.close()
    await app.shutdown()
