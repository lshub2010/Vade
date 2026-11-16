"""The skill-catalog message: one durable user-role reminder per session (RFC-0003 §4.2).

The catalog lives in HISTORY, not in the system prompt: appending a message never
touches the stable prefix, and the resend rule (§4.2.2) is decided against the
session log, not against memory — a restart, reconnect or compaction therefore
cannot make the loop believe a catalog is visible when it is not.

This module owns the ENVELOPE, core-side, for one reason: the loop must be able
to recognise the last catalog message in a log and parse its digest back out.
C3b supplies entries; it never rewords the shell. The digest covers the entries
only (`name` + clipped `description` + status/match marker, name order) —
rewording the envelope never triggers a resend (§4.2.1 rule 3), and the full
replacement rule means a resent message always carries every entry again.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "CatalogEntry", "CatalogInfo", "DESCRIPTION_MAX", "catalog_digest",
    "catalog_message", "is_catalog_message", "parse_catalog_digest",
]

#: Descriptions are clipped to the C-11 maximum before they enter the digest:
#: a longer frontmatter line must not change catalog bytes.
DESCRIPTION_MAX = 200

MARKER = "<system-reminder>"
_DIGEST_LINE = re.compile(r"^digest: ([0-9a-f]{64})$", re.MULTILINE)

_HEADER = "Skills available in this session (names and one-line summaries only):"
_FOOTER = (
    "Call `skill_load` with an exact name before following a skill's instructions.\n"
    "Do not infer a skill's body from its summary.\n"
    "If a skill was invoked with @name, its body is already in this conversation — do not load it again.\n"
    "Later catalog messages replace this list; the last one wins."
)
_EMPTY_NOTE = (
    "No skills are available in this session; ignore any skill names from "
    "earlier in this conversation."
)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One catalog line: name, one-line summary, status/`[match]` marker."""

    name: str
    description: str = ""
    marker: str = ""


@dataclass(frozen=True, slots=True)
class CatalogInfo:
    """What the loop needs per turn: the message text (if any), its digest,
    the entry names and whether the catalog is empty (retirement entry)."""

    text: str
    digest: str
    names: tuple[str, ...] = ()
    empty: bool = True


def _clip(text: object) -> str:
    return " ".join(str(text or "").split())[:DESCRIPTION_MAX]


def entry_line(entry: CatalogEntry) -> str:
    line = f"- `{entry.name}`: {_clip(entry.description)}"
    return f"{line} {entry.marker}" if entry.marker else line


def catalog_digest(entries: Iterable[CatalogEntry]) -> str:
    """sha256 over the canonical entry text, name order (§4.2.1 rule 3)."""
    lines = [entry_line(e) for e in sorted(entries, key=lambda e: e.name)]
    canonical = "\n".join(lines)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def catalog_message(entries: Sequence[CatalogEntry], digest: str) -> str:
    """The full replacement message (§4.2.1). Empty catalogs still produce one —
    the retirement entry — so stale skill names from earlier turns expire."""
    ordered = sorted(entries, key=lambda e: e.name)
    lines = [MARKER, _HEADER, f"digest: {digest}", "<available_skills>"]
    if ordered:
        lines.extend(entry_line(e) for e in ordered)
    lines.append("</available_skills>")
    if not ordered:
        lines.append(_EMPTY_NOTE)
    lines.append(_FOOTER)
    lines.append("</system-reminder>")
    return "\n".join(lines)


def is_catalog_message(content: object) -> bool:
    """True for messages THIS module built. Deliberately narrow: a user who
    literally types `<system-reminder>` does not produce a parseable digest
    line, so the legacy filter (§7) cannot swallow their words."""
    text = str(content or "")
    return text.startswith(MARKER) and "<available_skills>" in text and bool(_DIGEST_LINE.search(text))


def parse_catalog_digest(content: object) -> str | None:
    if not is_catalog_message(content):
        return None
    match = _DIGEST_LINE.search(str(content))
    return match.group(1) if match else None
