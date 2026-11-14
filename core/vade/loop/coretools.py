"""Core-native model tools (RFC-0003 §4.2.4 / §4.3): `skill_load` and tools search.

Naming rule (§4.2.4, A13): core built-ins use a SINGLE underscore
(`skill_load`, `tools_search`); module tools are `{prefix}__{tool}`
(`router/names.py`) and therefore always carry a double underscore — the two
namespaces are structurally disjoint. `tools/search` stays the C-05/RPC
spelling of the same capability; the model-visible name is `tools_search`
because an LLM function name may not contain `/` (LLM_NAME_RE).

This module is the ONLY place below the loop that knows those names. The engine
stays name-free: it receives `CoreTool` objects and reads results through the
`discovered()` / `requires()` accessors, so the source-grep boundary in
`core/tests/loop/test_engine.py` keeps holding. Handlers are duck-typed — the
skill loader arrives fully built from the session wiring — so `loop` never
imports the skill package.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

__all__ = [
    "SKILL_LOAD_NAME", "TOOLS_SEARCH_NAME", "CoreTool", "build_core_tools",
    "more_tools_line", "resolve_tool_search", "search_digest", "STICKY_K",
]

SKILL_LOAD_NAME = "skill_load"
TOOLS_SEARCH_NAME = "tools_search"

QUERY_MAX = 200
SEARCH_LIMIT_MAX = 8
SEARCH_LIMIT_DEFAULT = 5
DESCRIPTION_MAX = 200
#: §4.3.1 layer 4: per-session sticky cap (discovered / last-successful tools).
STICKY_K = 8
#: §4.2.3/§4.2.4: the per-turn cumulative body budget (== `body_budget_chars`).
BODY_BUDGET_DEFAULT = 12288
_BUDGET_TURNS_KEPT = 64

_MORE_LINE = "+{n} more tools are available; call {search} to find one by name or description."


def more_tools_line(dropped: int) -> str:
    """The `tool_block` trailer after hot-set truncation (§4.3.1).

    Only N changes it, and N does not grow with the module count (the hot set
    is capped), so the stable prefix survives module churn.
    """
    return _MORE_LINE.format(n=int(dropped), search=TOOLS_SEARCH_NAME)


@dataclass(frozen=True, slots=True)
class CoreTool:
    """A core-handled model tool: spec + handler + typed result accessors.

    The engine builds `ToolEntry` projections from the first four fields and
    calls `handler(args, snapshot, session_id=..., turn_id=...)` instead of the
    router. `discovered_fn` / `requires_fn` read a handler result so the loop
    never needs to know what a search result or a loaded skill looks like.
    """

    llm_name: str
    tool_id: str
    description: str
    input_schema: Mapping[str, Any]
    risk: str = "low"
    handler: Callable[..., Awaitable[Mapping[str, Any]]] | None = None
    discovered_fn: Callable[[Mapping[str, Any]], Iterable[str]] | None = None
    requires_fn: Callable[[Mapping[str, Any]], Iterable[str]] | None = None
    meta_fn: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None

    def discovered(self, result: Mapping[str, Any]) -> tuple[str, ...]:
        if self.discovered_fn is None:
            return ()
        try:
            return tuple(self.discovered_fn(result))
        except Exception:  # a broken accessor must not break the step loop
            return ()

    def requires(self, result: Mapping[str, Any]) -> tuple[str, ...]:
        if self.requires_fn is None:
            return ()
        try:
            return tuple(self.requires_fn(result))
        except Exception:
            return ()

    def discovery_meta(self, result: Mapping[str, Any]) -> Mapping[str, Any]:
        """Extra payload for `turn.tool_discovered` (query digest, hit count)."""
        if self.meta_fn is None:
            return {}
        try:
            return dict(self.meta_fn(result) or {})
        except Exception:
            return {}


# ------------------------------------------------------------------- search
def resolve_tool_search(
    entries: Sequence[Any],
    query: str,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Lexical match over the FROZEN snapshot (§4.3.2). No scoring: tiers are
    name exact > name prefix > description substring > module_id substring,
    ties resolved by the entries' name order. Returns `(matches, total)` —
    `total` counts all lexical hits, `matches` is the clipped page. Entries the
    snapshot does not contain are never returned: nothing is searchable that
    could not be called in the same turn.
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return [], 0
    tiers: list[list[Any]] = [[], [], [], []]
    for entry in entries:
        name = str(getattr(entry, "llm_name", "")).lower()
        if not name:
            continue
        if name == needle:
            tiers[0].append(entry)
        elif name.startswith(needle):
            tiers[1].append(entry)
        elif needle in str(getattr(entry, "description", "") or "").lower():
            tiers[2].append(entry)
        elif needle in str(getattr(entry, "module_id", "") or "").lower():
            tiers[3].append(entry)
    hits = [entry for tier in tiers for entry in tier]
    capped = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    matches = [
        {
            "name": entry.llm_name,
            "tool_id": entry.tool_id,
            "description": str(getattr(entry, "description", "") or "")[:DESCRIPTION_MAX],
            "risk": str(getattr(entry, "risk", "high") or "high"),
        }
        for entry in hits[:capped]
    ]
    return matches, len(hits)


def search_digest(matches: Sequence[Mapping[str, Any]]) -> str:
    """First 12 hex of sha256 over the canonical match list (§4.3.2). The same
    query twice must produce byte-identical output — UI dedupe and A6 hang on
    this."""
    canonical = json.dumps(
        sorted((dict(m) for m in matches), key=lambda m: str(m.get("name"))),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ skill_load
class _BodyBudgets:
    """Per-(session, turn) cumulative body budget (§4.2.3 table row 4).

    Keyed by turn so a session's next turn starts fresh; the map is pruned to
    the last N turns, so it can never grow with history.
    """

    def __init__(self, total: int) -> None:
        self.total = max(0, int(total))
        self._used: "OrderedDict[tuple[str, str], int]" = OrderedDict()

    def remaining(self, session_id: str, turn_id: str) -> int:
        return self.total - self._used.get((session_id, turn_id), 0)

    def spend(self, session_id: str, turn_id: str, chars: int) -> None:
        key = (session_id, turn_id)
        self._used[key] = self._used.get(key, 0) + max(0, int(chars))
        self._used.move_to_end(key)
        while len(self._used) > _BUDGET_TURNS_KEPT:
            self._used.popitem(last=False)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message, "result": None}


def _ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "code": None, "message": "", "result": result}


def _payload_of(result: Any) -> Mapping[str, Any]:
    """The accessors see either the raw handler envelope (a Mapping) or the
    `ToolCallResult` the loop built from it — accept both."""
    if isinstance(result, Mapping):
        return (result or {}).get("result") or {}
    return getattr(result, "result", None) or {}


def _resolve_loader(source: Any) -> Any:
    """A loader is either an instance or a zero-arg provider — the session
    component installs BEFORE the skill one (alphabetical), so the wiring must
    not capture `app.skill_loader` at install time."""
    if source is None:
        return None
    if callable(source):
        try:
            return source()
        except Exception:  # a broken provider must not break a turn
            return None
    return source


def build_core_tools(loader_source: Any = None, *, body_budget_chars: int = BODY_BUDGET_DEFAULT) -> dict[str, CoreTool]:
    """Build the two core tools. `loader_source` is a duck-typed `SkillLoader`,
    a zero-arg provider for one, or None: `skill_load` then answers UNAVAILABLE
    instead of failing the turn. A provider is resolved per call, so a loader
    that arrives after wiring (session installs before skill) is still seen."""
    budgets = _BodyBudgets(body_budget_chars)

    async def skill_load_handler(args, snapshot, *, session_id: str = "", turn_id: str = ""):
        loader = _resolve_loader(loader_source)
        if loader is None:
            return _error("UNAVAILABLE", "the skill subsystem is not installed")
        name = str((args or {}).get("name") or "").strip()
        if not name:
            return _error("INVALID_ARGUMENT", "skill_load requires `name` (an exact catalog name)")
        remaining = budgets.remaining(session_id, turn_id)
        if remaining <= 0:
            return _error("BUDGET_EXCEEDED",
                          "the per-turn skill body budget is spent; ask for another skill next turn")
        try:
            body, revision, truncated = loader.load_body(name, budget_chars=remaining)
        except Exception as exc:
            code = getattr(exc, "code", None) or "UNAVAILABLE"
            return _error(code, f"skill_load failed for {name!r}: {exc}")
        budgets.spend(session_id, turn_id, len(body))
        requires = tuple(loader.requires_for((name,)))
        return _ok({
            "skill": name,
            "revision": revision,
            "truncated": bool(truncated),
            "body": body,
            "requires_tools": list(requires),
        })

    async def tools_search_handler(args, snapshot, *, session_id: str = "", turn_id: str = ""):
        query = str((args or {}).get("query") or "")
        if not query.strip() or len(query) > QUERY_MAX:
            return _error("INVALID_ARGUMENT", f"tools_search requires `query` (1..{QUERY_MAX} chars)")
        try:
            limit = int((args or {}).get("limit") or SEARCH_LIMIT_DEFAULT)
        except (TypeError, ValueError):
            limit = SEARCH_LIMIT_DEFAULT
        entries = list(getattr(snapshot, "entries", ()) or ())
        matches, total = resolve_tool_search(entries, query, limit)
        query_digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
        return _ok({"matches": matches, "digest": search_digest(matches),
                    "total": total, "query_digest": query_digest})

    def _load_requires(result):
        return _payload_of(result).get("requires_tools") or ()

    def _search_discovered(result):
        return [str(m.get("name")) for m in _payload_of(result).get("matches") or ()]

    def _search_meta(result):
        payload = _payload_of(result)
        return {"query_digest": str(payload.get("query_digest") or ""),
                "total_matches": int(payload.get("total") or 0)}

    return {
        SKILL_LOAD_NAME: CoreTool(
            llm_name=SKILL_LOAD_NAME,
            tool_id=f"core/{SKILL_LOAD_NAME}",
            description=(
                "Load one skill's full instructions by exact name from this session's "
                "skill catalog. Call it before following a skill's summary."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Exact skill name from the catalog"}},
                "required": ["name"],
            },
            handler=skill_load_handler,
            requires_fn=_load_requires,
        ),
        TOOLS_SEARCH_NAME: CoreTool(
            llm_name=TOOLS_SEARCH_NAME,
            tool_id=f"core/{TOOLS_SEARCH_NAME}",
            description=(
                "Search the tools available this turn by name or description keyword. "
                "Use it before inventing a tool name; a match is callable immediately."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": QUERY_MAX},
                    "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT_MAX,
                              "default": SEARCH_LIMIT_DEFAULT},
                },
                "required": ["query"],
            },
            handler=tools_search_handler,
            discovered_fn=_search_discovered,
            requires_fn=lambda result: (),
            meta_fn=_search_meta,
        ),
    }
