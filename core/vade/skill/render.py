"""Prompt rendering for the two skill slots: L0 catalog, L1 bodies.

Fixed shape (docs/agents/23 §T3):

    Skills (activate with @name; bodies load on activation):
    - kb-guide: project knowledge base rules
    - legacy: old notes [unavailable]

    ## skill: kb-guide
    <body, verbatim>

Determinism is a feature: lines and bodies are emitted in **name order**, the
budget is spent in **priority order** (activated first, then trigger hit count
descending, then name), and no dictionary iteration order ever reaches the
output. Same input -> byte-identical prompt -> the upstream prompt cache keeps
hitting.

Nothing that identifies the host may reach the prompt: a truncation hint says
what was cut, never where the file lives on disk (`state.path` is internal).

Deliberately no relevance re-ranking, no XML wrappers (models follow tags
inconsistently), and no "name-only" fallback for a dropped line: a name without
a description tells the model nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

__all__ = ["CATALOG_HEADER", "Rendered", "estimate_tokens", "render_catalog", "render_bodies", "render_content"]

CATALOG_HEADER = "Skills (activate with @name; bodies load on activation):"
MIN_BODY_CHARS = 400


@dataclass(frozen=True, slots=True)
class Rendered:
    catalog: str = ""
    bodies: str = ""
    activated: tuple[str, ...] = ()
    l0_dropped: tuple[str, ...] = ()
    l1_dropped: tuple[str, ...] = ()


def estimate_tokens(text: str) -> int:
    """Cheap conservative estimate; the exact tokenizer lives in the model module.

    Four characters per token is the usual English ratio and stays *under* the
    real count for CJK, so a budget check never overshoots.
    """
    return max(1, (len(text) + 3) // 4)


def _catalog_line(state: object) -> str:
    marker = getattr(state, "marker", "") or ""
    description = " ".join(str(getattr(state, "description", "")).split())
    line = f"- {getattr(state, 'name')}: {description}"
    return f"{line} {marker}" if marker else line


def _hit_count(hits: Mapping[str, int] | None, name: str) -> int:
    """Trigger-hit count for `name`; 0 when unknown (never a reason to drop)."""
    getter = getattr(hits, "get", None)
    if not callable(getter):
        return 0
    value = getter(name)
    if value is None:
        value = getter(name.lower())
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _priority(
    states: Sequence[object],
    activated: Sequence[str],
    hits: Mapping[str, int] | None = None,
) -> list[object]:
    """Build order: activated (rank order), then hit count desc, then name.

    `hits` is `Activation.hits` (docs/agents/23 §T3). Skills that matched
    triggers but lost the `max_activated` cut keep their hit count here, so the
    L0 budget trims the least relevant line first instead of the last name.
    """
    by_name = {str(getattr(s, "name")): s for s in states}
    ordered: list[object] = []
    seen: set[str] = set()
    for name in activated:
        if name in by_name and name not in seen:
            ordered.append(by_name[name])
            seen.add(name)
    rest = sorted(
        (s for n, s in by_name.items() if n not in seen),
        key=lambda s: (-_hit_count(hits, str(getattr(s, "name"))), str(getattr(s, "name"))),
    )
    ordered.extend(rest)
    return ordered


def render_catalog(
    states: Sequence[object],
    activated: Sequence[str],
    *,
    token_budget: int,
    hits: Mapping[str, int] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Return `(catalog_text, dropped_names)`.

    `states` must be the catalog-visible set (invalid/pending never reach here).
    `activated` is priority-ordered (`Activation.names`); `hits` is
    `Activation.hits`, which is what orders the *non*-activated remainder
    (docs/agents/23 §T3: activated, hit count desc, name).
    """
    if not states:
        return "", ()
    budget = max(0, int(token_budget)) * 4
    header = CATALOG_HEADER
    kept: list[str] = []
    dropped: list[str] = []
    used = len(header) + 1
    for state in _priority(states, activated, hits):
        line = _catalog_line(state)
        cost = len(line) + 1
        if used + cost > budget:
            dropped.append(str(getattr(state, "name")))
            continue
        kept.append(line)
        used += cost
    if not kept:
        return "", tuple(sorted(dropped))
    # Output in name order, independent of the order the budget was spent in.
    kept.sort(key=lambda line: line[2:].split(":", 1)[0])
    return "\n".join([header, *kept]), tuple(sorted(dropped))


def render_bodies(
    states: Sequence[object],
    activated: Sequence[str],
    *,
    budget_chars: int,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return `(bodies_text, injected_priority_order, dropped_names)`.

    Bodies are concatenated in name order but the budget is spent in rank order,
    so the most relevant skill is never the one that gets cut.
    """
    by_name = {str(getattr(s, "name")): s for s in states}
    ordered = [by_name[n] for n in activated if n in by_name]
    if not ordered:
        return "", (), ()
    budget = max(0, int(budget_chars))
    chunks: dict[str, str] = {}
    dropped: list[str] = []
    used = 0
    for state in ordered:
        name = str(getattr(state, "name"))
        header = f"## skill: {name}\n"
        body = str(getattr(state, "body", ""))
        cost = len(header) + len(body) + 2  # + blank line between sections
        if used + cost <= budget:
            chunks[name] = header + body
            used += cost
            continue
        room = budget - used - len(header) - 2
        if room >= MIN_BODY_CHARS and len(body) > room:
            # Never interpolate `state.path` here: the prompt is not the place
            # to disclose the host's filesystem layout (or the data dir).
            hint = "\n\n[... truncated, remaining text omitted ...]"
            cut = body[: max(0, room - len(hint))].rstrip()
            chunks[name] = header + cut + hint
            used = budget
            continue
        dropped.append(name)
    if not chunks:
        return "", (), tuple(sorted(dropped))
    text = "\n\n".join(chunks[name] for name in sorted(chunks))
    return text, tuple(sorted(chunks)), tuple(sorted(dropped))


def render_content(
    states: Sequence[object],
    mentioned: Sequence[str],
    *,
    budget_chars: int,
) -> str:
    """RFC-0003 §4.2.3: @name bodies as `<skill_content>` blocks.

    Layered layout: the body leaves the system prompt and arrives as one
    user-role message (the `skill_content` slot), wrapped per skill with its
    revision so the model can tell versions apart. Budget rule follows
    `render_bodies` — most-relevant first, overflow dropped, never re-ranked.
    """
    by_name = {str(getattr(s, "name")): s for s in states}
    budget = max(0, int(budget_chars))
    blocks: list[str] = []
    used = 0
    for name in mentioned:
        state = by_name.get(str(name))
        if state is None:
            continue
        body = str(getattr(state, "body", ""))
        revision = int(getattr(state, "revision", 0) or 0)
        text = f'<skill_content name="{name}" revision="{revision}">\n{body}\n</skill_content>'
        if used + len(text) > budget and blocks:
            break
        if len(text) > budget:
            continue
        blocks.append(text)
        used += len(text) + 2
    return "\n\n".join(blocks)
