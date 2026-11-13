"""Prompt assembly: the single place C3b can hook into.

`assemble` is a pure function — same parts in, byte-identical messages out — so
C3b's snapshot tests are meaningful. Fixed order (design §2.7.3):
persona -> tool_block -> skill_catalog -> skill_bodies -> session_memory -> context.

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

__all__ = ["PromptParts", "assemble", "apply_hooks"]


@dataclass(frozen=True, slots=True)
class PromptParts:
    persona: str
    tool_block: str
    skill_catalog: str = ""
    skill_bodies: str = ""
    session_memory: str = ""
    user_message: str = ""
    activated_skills: tuple[str, ...] = ()
    context: str = ""


_SECTIONS = ("persona", "tool_block", "skill_catalog", "skill_bodies", "session_memory", "context")


def assemble(parts: PromptParts) -> list[Message]:
    """Render the system sections in fixed order. Pure by construction."""
    out: list[Message] = []
    for index, name in enumerate(_SECTIONS):
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
