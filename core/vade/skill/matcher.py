"""Activation: @mention, triggers (substring only), explicit disable.

No embeddings, no model call, no relevance re-ranking — v1's rule is that the
same message must always activate the same skills in the same order, otherwise
the prompt cache and the audit trail both become noise.

Ranking (docs/agents/23 §T2): `(mentioned first, trigger hit count desc, name)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = ["Activation", "mention_re", "find_mentions", "select", "MENTION_LIMIT"]

MENTION_LIMIT = 3

#: `@name` / `@!name`. The lookarounds keep `user@host.tld` from counting and
#: `@!` marks an explicit opt-out (it wins over any trigger hit).
_MENTION = re.compile(r"(?<![\w.+-])@(?P<disable>!)?(?P<name>[A-Za-z][A-Za-z0-9-]{1,31})(?![\w.-])")


@dataclass(frozen=True, slots=True)
class Activation:
    #: Activated names in priority order; already capped at the limit.
    names: tuple[str, ...] = ()
    #: Activated names sorted for output (stable catalog/body order).
    ordered: tuple[str, ...] = ()
    mentioned: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    hits: Mapping[str, int] = field(default_factory=dict)

    def hit_count(self, name: str) -> int:
        return int(self.hits.get(name, 0))

    def __bool__(self) -> bool:
        return bool(self.names)


def mention_re() -> re.Pattern[str]:
    return _MENTION


def find_mentions(user_message: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return `(mentioned, disabled)` lowercased, order preserved, de-duped."""
    mentioned: list[str] = []
    disabled: list[str] = []
    for match in _MENTION.finditer(user_message or ""):
        name = match.group("name").lower()
        bucket = disabled if match.group("disable") else mentioned
        if name not in bucket:
            bucket.append(name)
    return tuple(mentioned), tuple(disabled)


def select(
    user_message: str,
    states: Iterable[object],
    *,
    limit: int = MENTION_LIMIT,
) -> Activation:
    """Pick at most `limit` injectable skills for this message.

    `states` items need `.name`, `.triggers`, `.status`, `.available`.
    Ineligible skills (stale/disabled/unavailable) are never activated; they stay
    in the catalog with a marker. An explicit `@!name` removes a skill even when
    a trigger matched it.
    """
    mentioned, disabled = find_mentions(user_message)
    message = (user_message or "").lower()
    disabled_set = set(disabled)

    hits: dict[str, int] = {}
    candidates: dict[str, int] = {}
    for state in states:
        name = str(getattr(state, "name", "")).lower()
        if not name or name in disabled_set:
            continue
        if str(getattr(state, "status", "active")) != "active":
            continue
        if not bool(getattr(state, "available", True)):
            continue
        count = 0
        for trigger in getattr(state, "triggers", ()) or ():
            needle = str(trigger).strip().lower()
            if needle and needle in message:
                count += 1
        hits[name] = count
        if name in mentioned or count:
            candidates[name] = 0 if name in mentioned else 1

    ranked = sorted(candidates, key=lambda n: (candidates[n], -hits.get(n, 0), n))
    chosen = tuple(ranked[: max(1, int(limit))])
    return Activation(
        names=chosen,
        ordered=tuple(sorted(chosen)),
        mentioned=tuple(n for n in mentioned if n in chosen),
        disabled=tuple(n for n in disabled if n),
        hits={name: hits.get(name, 0) for name in hits},
    )
