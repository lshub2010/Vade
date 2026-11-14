"""The loop's only view of the skill catalog (RFC-0003 §4.2.2).

The engine stays skill-free: in layered mode it calls `view()` once per turn
(message text + digest + entry names) and `requires()` for hot-tool resolution
(§4.3.1 layer 2). Both are read-only and stateless, so concurrent sessions
cannot bleed into each other.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..loop.catalog import CatalogEntry, CatalogInfo, catalog_digest, catalog_message

__all__ = ["LayeredCatalogPort"]


class LayeredCatalogPort:
    """Bridges the skill loader to the loop's catalog contract.

    `loader` is a `SkillLoader` instance OR a zero-arg provider for one: the
    session component installs BEFORE the skill one (alphabetical), so real
    wiring resolves `app.skill_loader` lazily, per call. A missing or broken
    loader degrades to the empty-catalog retirement entry — never a failed
    turn.

    `[match]` markers reflect THIS turn's activation (§4.2.3): they are
    deliberately excluded from the digest, so a turn whose only change is which
    triggers fired does NOT resend the message — the marker is best-effort
    signage, the durable contract is names + descriptions + status.
    """

    def __init__(self, loader: Any) -> None:
        self._source = loader

    def _loader(self) -> Any:
        if self._source is None:
            return None
        if callable(self._source):
            try:
                return self._source()
            except Exception:
                return None
        return self._source

    def view(self, user_text: str = "") -> CatalogInfo:
        loader = self._loader()
        if loader is None:
            empty = catalog_digest(())
            return CatalogInfo(text=catalog_message((), empty), digest=empty, names=(), empty=True)
        states = loader.catalog()
        matched: set[str] = set()
        try:
            matched = set(loader.catalog_matches(user_text or ""))
        except Exception:  # a broken matcher must not break the turn
            matched = set()
        entries = [
            CatalogEntry(
                name=str(s.name),
                description=str(s.description),
                marker=_marker(s, str(s.name) in matched),
            )
            for s in states
        ]
        digest = catalog_digest(entries)
        return CatalogInfo(
            text=catalog_message(entries, digest),
            digest=digest,
            names=tuple(e.name for e in entries),
            empty=not entries,
        )

    def requires(self, names: Sequence[str]) -> tuple[str, ...]:
        loader = self._loader()
        if loader is None:
            return ()
        try:
            return tuple(loader.requires_for(names))
        except Exception:
            return ()


def _marker(state: Any, matched: bool) -> str:
    base = str(getattr(state, "marker", "") or "")
    if matched and not base:
        # select() only activates active+available skills, so a matched skill
        # never carries [unavailable]/[stale]/[disabled] here.
        return "[match]"
    return base
