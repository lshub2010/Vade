"""RFC-0014 §4: session/search semantics — one place, testable without SQLite.

The search is a parameterized LIKE scan over `sessions.title` and the live
`messages.content` rows (RFC-0006: rows with `superseded_by` are edit history,
not live content). Everything the contract promises about matching (ASCII case
folding, literal `%`/`_`, CJK exactness), ordering (title tier > body tier,
§4.4), snippet clipping (<= SNIPPET_MAX code points, §4.7) and injection safety
(§4.10) is defined here; `sqlite_store.py` only glues it into SQL.

Injection posture (§4.10): the store binds every value — the escaped pattern
included — through `?` placeholders. `escape_like` touches exactly the three
LIKE metacharacters (`%`, `_`, `\`); a single quote and `--` are NOT LIKE
metacharacters, so escaping them would corrupt honest queries ("it's", "a--b")
— parameterization is what keeps those literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import VadeError

__all__ = [
    "QUERY_MAX", "LIMIT_MAX", "LIMIT_DEFAULT", "SNIPPET_MAX", "SNIPPET_CONTEXT",
    "escape_like", "validate_query", "find_ascii_ci", "make_snippet", "SearchHit",
]

#: §4.6: the query cap, aligned with the `session/rename` title cap (1..200):
#: a search box is not a paste target, and the LIKE scan cost is linear in
#: |query|. Counting unit is the code point, like TEXT_LIMIT (RFC-0006 §8).
QUERY_MAX = 200

#: §4.5: page bounds. Search rows carry snippets (~10x the weight of a
#: session/list light row), so the cap sits below session/list's 200.
LIMIT_MAX = 100
LIMIT_DEFAULT = 20

#: §4.7: a snippet is a hard-clipped window around the FIRST match, ellipses
#: included in the budget. Bodies reach 32 000 code points (RFC-0006 §8); a
#: full-body hit row would be a payload bug, not a feature.
SNIPPET_MAX = 120
#: Code points of lead-in context before the match inside the window.
SNIPPET_CONTEXT = 40


def escape_like(text: str) -> str:
    """Escape the three LIKE metacharacters; `ESCAPE '\\'` restores them.

    `%` and `_` are wildcards, `\` is the escape itself. A single quote and
    `--` are deliberately untouched (§4.10): they are not LIKE metacharacters,
    and parameters make them literal already.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def validate_query(raw: Any) -> str:
    """§4.6: strip, then demand 1..QUERY_MAX code points. Empty is an error.

    There is no "empty query means everything" mode: a search box that returns
    the whole workspace on Enter is a UX bug with an unbounded scan behind it.
    """
    if not isinstance(raw, str):
        raise VadeError("INVALID_ARGUMENT", "query must be a string",
                        data={"field": "query"})
    query = raw.strip()
    if not query:
        raise VadeError("INVALID_ARGUMENT", "query must not be blank",
                        data={"field": "query"})
    if len(query) > QUERY_MAX:
        raise VadeError("INVALID_ARGUMENT",
                        f"query must be at most {QUERY_MAX} code points",
                        data={"field": "query", "limit": QUERY_MAX, "actual": len(query)})
    return query


def _ascii_fold(ch: str) -> str:
    """ASCII-only case folding — the exact mirror of SQLite LIKE semantics.

    Python's `str.lower()` folds far more (ß, İ, full-width forms); using it
    here would make `find_ascii_ci` disagree with the SQL that matched the row
    in the first place (§4.3).
    """
    return ch.lower() if "A" <= ch <= "Z" else ch


def find_ascii_ci(haystack: str, needle: str) -> int:
    """First index where `needle` occurs under SQLite LIKE folding, else -1.

    LIKE folds ASCII letters only; everything else compares exactly (§4.3).
    This is what points `snippet_offset` at the match the SQL actually found —
    a Unicode-aware finder could point at a match LIKE never made.
    """
    n = len(needle)
    if n == 0 or n > len(haystack):
        return -1
    first = _ascii_fold(needle[0])
    for i in range(len(haystack) - n + 1):
        if _ascii_fold(haystack[i]) != first:
            continue
        for j in range(1, n):
            if _ascii_fold(haystack[i + j]) != _ascii_fold(needle[j]):
                break
        else:
            return i
    return -1


def make_snippet(content: str, match_offset: int) -> str:
    """§4.7: a <= SNIPPET_MAX window around the match, `…` where clipped.

    `match_offset` is the code-point index of the match start in the ORIGINAL
    content (that index is what goes on the wire as `snippet_offset`); the
    ellipses count against the budget so the whole snippet never exceeds
    SNIPPET_MAX code points.
    """
    start = max(0, match_offset - SNIPPET_CONTEXT)
    raw = content[start:start + SNIPPET_MAX]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + len(raw) < len(content) else ""
    window = raw[:SNIPPET_MAX - len(prefix) - len(suffix)]
    return f"{prefix}{window}{suffix}"


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result row — one SESSION, deduped (title dominates body, §4.4)."""

    session_id: str
    title: str | None
    updated_at: int
    matched_in: str  # "title" | "body"
    message_id: str | None = None
    snippet: str | None = None
    snippet_offset: int | None = None
    archived: bool = False

    def to_wire(self) -> dict[str, Any]:
        """The C-05 hit-row shape (RFC-0014 §5; mirrored by schema/rpc.json)."""
        return {
            "session_id": self.session_id,
            "title": self.title,
            "updated_at": self.updated_at,
            "matched_in": self.matched_in,
            "message_id": self.message_id,
            "snippet": self.snippet,
            "snippet_offset": self.snippet_offset,
            "archived": self.archived,
        }
