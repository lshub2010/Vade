"""Prompt assembly: the single place C3b can hook into.

`assemble` is a pure function — same parts in, byte-identical messages out — so
C3b's snapshot tests are meaningful.

Two layouts live here (RFC-0003 §4.4), selected by `injection`:

* **legacy** (default) — the fixed six-section system block
  persona -> tool_block -> skill_catalog -> skill_bodies -> session_memory ->
  context, one system message per non-empty section. Byte-for-byte the
  pre-RFC-0003 behaviour; pinned by `core/tests/loop/golden/prompt_legacy.json`.
* **layered** — stable sections (orders 100–200: persona, policy, tool_block)
  come first and must stay byte-stable within a session; context sections
  (orders 500–600: session_memory, runtime_facts) may change every turn. The
  catalog/bodies slots are NOT system channels in this layout (C-11
  `injection.channels`: history.user / tool_result), so they are never emitted
  here even if a legacy hook fills them; @name bodies ride the `skill_content`
  slot and go out as ONE user-role message after the system block.

The user message is NOT part of `assemble`'s output: the loop appends the
structured history (which already contains the persisted user message). It is
still carried in `PromptParts.user_message` because C3b's @mention / trigger
matching reads it before assembly.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Sequence

from ..session.models import Message

__all__ = [
    "PromptParts", "SECTION_ORDERS", "SECTION_ALIASES", "STABLE_SECTIONS",
    "CONTEXT_SECTIONS", "LEGACY_SECTIONS", "assemble", "apply_hooks",
]


@dataclass(frozen=True, slots=True)
class PromptParts:
    persona: str
    tool_block: str
    policy: str = ""  # RFC-0003 order 110 (stable); core-owned, empty in v1
    skill_catalog: str = ""
    skill_bodies: str = ""
    session_memory: str = ""
    user_message: str = ""
    activated_skills: tuple[str, ...] = ()
    context: str = ""
    #: RFC-0003 §4.2.3: layered @name bodies, injected as a user-role message
    #: (the `skill_bodies` order-400 channel). Empty in legacy.
    skill_content: str = ""


#: RFC-0003 §4.4 — the central order table. Unique numbers, one writer per
#: section, step 10 leaves insertion room (150 / 250) so a new section never
#: renumbers an existing one. `policy` (110) is new and sits between persona
#: and tool_block; `context` (legacy field name) is the `runtime_facts`
#: section. Same-number fallback is NOT a feature — dsh's 0–3% cache hit rate
#: is what same-number sections buy.
SECTION_ORDERS: dict[str, int] = {
    "persona": 100,
    "policy": 110,
    "tool_block": 200,
    "skill_catalog": 300,
    "skill_bodies": 400,
    "session_memory": 500,
    "runtime_facts": 600,
}

#: legacy field name -> layered section name (C-11 `injection.channels`).
SECTION_ALIASES = {"context": "runtime_facts"}

#: Sections that must stay byte-stable for a whole session (prefix cache).
STABLE_SECTIONS = ("persona", "policy", "tool_block")
#: Sections that may change per turn; they sort after every stable section.
CONTEXT_SECTIONS = ("session_memory", "runtime_facts")

#: The pre-RFC-0003 layout, kept byte-identical under `injection="legacy"`.
LEGACY_SECTIONS = ("persona", "tool_block", "skill_catalog", "skill_bodies", "session_memory", "context")

# Back-compat alias for readers of the old private name.
_SECTIONS = LEGACY_SECTIONS


def _message(order: int, name: str, text: str, *, role: str = "system") -> Message:
    return Message(id=f"prompt-{order}-{name}", session_id="", role=role, content=text, created_at=0)


def assemble(parts: PromptParts, *, injection: str = "legacy") -> list[Message]:
    """Render the system sections in fixed order. Pure by construction.

    `injection="layered"` emits stable sections, then context sections, then —
    if C3b loaded a body via @name — one user-role message with the
    `<skill_content>` blocks. Everything else (catalog message, tool results)
    is appended by the loop around this output.
    """
    if injection == "layered":
        return _assemble_layered(parts)
    out: list[Message] = []
    for index, name in enumerate(LEGACY_SECTIONS):
        text = getattr(parts, name)
        if not text:
            continue
        out.append(
            Message(
                id=f"prompt-{index}-{name}",
                session_id="",
                role="system",
                content=text,
                created_at=0,
            )
        )
    return out


def _assemble_layered(parts: PromptParts) -> list[Message]:
    out: list[Message] = []
    for name in STABLE_SECTIONS:
        text = getattr(parts, name, "")
        if text:
            out.append(_message(SECTION_ORDERS[name], name, text))
    for name in CONTEXT_SECTIONS:
        field = "context" if name == "runtime_facts" else name
        text = getattr(parts, field, "")
        if text:
            out.append(_message(SECTION_ORDERS[name], name, text))
    if parts.skill_content:
        out.append(_message(SECTION_ORDERS["skill_bodies"], "skill_content", parts.skill_content, role="user"))
    return out


async def apply_hooks(parts: PromptParts, hooks: Iterable[Callable[..., Any]]) -> PromptParts:
    """Run C3b's prompt hooks in registration order; sync or async both work."""
    current = parts
    for hook in hooks:
        result = hook(current)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, PromptParts):
            current = result
        elif isinstance(result, dict):
            current = replace(current, **result)
    return current
