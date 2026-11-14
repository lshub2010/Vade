"""Built-in tools the loop engine offers beside module tools (RFC-0008 §14).

`ask_user` is the only member in v1. The engine stays name-blind (22-C3 §T4):
it sees a `BuiltinRegistry` and dispatches whatever the model called into the
matching handler — the concrete name lives only here, one layer *above*
`engine.py`, which the source-grep test in `core/tests/loop/test_engine.py`
keeps free of tool-name literals.

The handler talks to the `InputPort` protocol (`.ports`), never to
`vade.input`: the loop must stay free of C5a's module, the same way
`approval.py` implements `ApprovalPort` from the policy side. Availability and
argument validation happen HERE, fail-closed, because the tool-level contract
must hold for ANY port implementation:

* `NullInputPort` / no port / `ui_attached == 0` -> immediate `UNAVAILABLE`
  tool result (RFC-0008 §8.4: never burn the timeout on a question nobody can
  answer — the same shape as approval's `no_approver` deny);
* a `kind`/`options` combination that does not parse -> `INVALID_ARGUMENT`
  tool result and the turn continues (§4.4 item 4), never a best-effort guess.

`InputManager` re-validates on its own side (defense in depth, §4.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence, cast

from ..errors import VadeError
from ..router.dispatch import ToolCallResult, validate_arguments
from .ports import InputPrompt, NullInputPort

__all__ = ["BuiltinTool", "BuiltinContext", "BuiltinRegistry", "default_builtins",
           "ASK_USER_NAME"]

#: RFC-0008 §4: the four controls and the bounds the tool schema declares.
ASK_USER_NAME = "ask_user"
_KINDS = ("free_text", "single_choice", "multi_choice", "confirm")
_CHOICE_KINDS = ("single_choice", "multi_choice")
_PROMPT_MAX = 500
_OPTIONS_MIN = 2
_OPTIONS_MAX = 20
_OPTION_ID_MAX = 64
_OPTION_LABEL_MAX = 120

#: C-05 `tool_result.result` for `ask_user`: statuses the model can see.
STATUS_ANSWERED = "answered"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"

ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "prompt"],
    "properties": {
        "kind": {"type": "string",
                 "enum": ["free_text", "single_choice", "multi_choice", "confirm"]},
        "prompt": {"type": "string", "maxLength": _PROMPT_MAX},
        "options": {"type": "array", "minItems": _OPTIONS_MIN, "maxItems": _OPTIONS_MAX,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["id", "label"],
                        "properties": {
                            "id": {"type": "string", "maxLength": _OPTION_ID_MAX},
                            "label": {"type": "string", "maxLength": _OPTION_LABEL_MAX},
                            "description": {"type": "string"},
                        },
                    }},
        # UI prefill only. On timeout the model gets value:null, never this.
        "default": {},
        "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600},
    },
}

ASK_USER_DESCRIPTION = (
    "Ask the user a question and block until they answer, cancel, or the "
    "timeout elapses. Use it when you need information only the user can "
    "provide before you can continue (a missing filename, a choice between "
    "options, a yes/no). Do NOT use it to confirm things that are already "
    "obvious, and do NOT use it to request permission for an action — "
    "approvals have their own channel. Kinds: free_text (a text answer), "
    "single_choice/multi_choice (pick from `options`, 2..20 items), "
    "confirm (true/false)."
)


@dataclass(frozen=True, slots=True)
class BuiltinContext:
    """What the engine hands a builtin handler for ONE call.

    `emit` is the session's raw event writer (persist-before-broadcast). It is
    deliberately NOT the guarded `_turn_emit`: the input manager must be able
    to publish `turn.input_resolved` while the turn is already flagged
    interrupted (S5 — the resolution closes the dialog BEFORE
    `turn.interrupted`, and the guard would swallow exactly that write).
    """

    session_id: str
    turn_id: str
    emit: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None


@dataclass(frozen=True, slots=True)
class BuiltinTool:
    """A core-owned tool: schema, prompt text and the local executor."""

    llm_name: str
    tool_id: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any], BuiltinContext], Awaitable[Any]]
    risk: str = "low"
    timeout_ms: int = 600_000
    idempotent: bool = True


class BuiltinRegistry:
    """The engine's view of built-in tools: lookup, LLM schemas, prompt block.

    Module tools always win a name lookup: `self.get` is only consulted when
    the turn snapshot has no such entry. (Module llm names are always
    `prefix__tool`, so a bare builtin name cannot collide anyway.)
    """

    def __init__(self, tools: Sequence[BuiltinTool]) -> None:
        self._tools: dict[str, BuiltinTool] = {t.llm_name: t for t in tools}

    def get(self, llm_name: str) -> BuiltinTool | None:
        return self._tools.get(llm_name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __bool__(self) -> bool:
        return bool(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI `tools` entries, deterministic order (sorted by name)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.llm_name,
                    "description": tool.description,
                    "parameters": dict(tool.input_schema),
                },
            }
            for _, tool in sorted(self._tools.items())
        ]

    def render_block(self) -> str:
        lines = ["Always-available core tools (call by exact name):"]
        for _, tool in sorted(self._tools.items()):
            lines.append(f"- {tool.llm_name}: {tool.description} [risk={tool.risk}]")
        return "\n".join(lines)


def default_builtins(input_port_provider: Callable[[], Any]) -> BuiltinRegistry:
    """The registry the session wiring installs: `ask_user` over `InputPort`.

    `input_port_provider` is resolved at CALL time (the input component may
    install after the loop is constructed); `None` means the tool answers
    `UNAVAILABLE` immediately instead of pretending a dialog was shown.
    """

    async def handler(args: Mapping[str, Any], ctx: BuiltinContext) -> Any:
        question = _validate_question(args)
        port = input_port_provider()
        if port is None or isinstance(port, NullInputPort):
            return _unavailable("no ask-user channel is installed")
        attached = _ui_attached(port, ctx.session_id)
        if attached is not None and attached <= 0:
            return _unavailable("no UI is attached to answer questions")
        prompt = InputPrompt(
            session_id=ctx.session_id, turn_id=ctx.turn_id,
            kind=question["kind"], prompt=question["prompt"],
            options=tuple(question["options"]),
            timeout_s=question["timeout_s"], default=question["default"],
            source={"tool": ASK_USER_NAME, "depth": 0},
            emit=ctx.emit,
        )
        answer = await port.ask(prompt)
        return _map_answer(answer)

    return BuiltinRegistry([BuiltinTool(
        llm_name=ASK_USER_NAME,
        tool_id=f"core/{ASK_USER_NAME}",
        description=ASK_USER_DESCRIPTION,
        input_schema=ASK_USER_SCHEMA,
        handler=handler,
        risk="low",
    )])


# ------------------------------------------------------------------- mapping
def _unavailable(message: str) -> ToolCallResult:
    """§8.4: nobody can be asked — say so NOW, do not burn the timeout."""
    return ToolCallResult(False, f"core/{ASK_USER_NAME}", code="UNAVAILABLE",
                          message=message)


def _map_answer(answer: Any) -> ToolCallResult:
    """`InputAnswer` -> tool result. `value` is DATA returned to the model.

    It is never executed, interpolated into arguments or pre-authorized (§9.1):
    the mapping below only repacks it, and the audit rows the manager wrote
    carry `value_digest`/`value_chars`, not the value itself (§9.4).
    """
    if answer.ok:
        return ToolCallResult(True, f"core/{ASK_USER_NAME}",
                              result={"status": STATUS_ANSWERED, "value": answer.value})
    if getattr(answer, "reason", "") == "no_ui":
        return _unavailable(answer.message or "the user interface is not available")
    status = getattr(answer, "status", STATUS_CANCELLED)
    return ToolCallResult(
        False, f"core/{ASK_USER_NAME}",
        code="CANCELLED",
        message=answer.message or "the user did not answer",
        # The structured outcome carries status/value even though `ok` is
        # False: tests and callers can read `{status: timeout|cancelled,
        # value: null}` directly from the result field. The model-visible
        # content stays the C-05 error shape (`{"ok": false, "error": ...}`),
        # and `value` is null — the `default` never appears here (§8.1).
        result={"status": status, "value": None},
    )


def _ui_attached(port: Any, session_id: str) -> int | None:
    probe = getattr(port, "ui_attached", None)
    if not callable(probe):
        return None
    probe_fn = cast(Callable[[str], int], probe)
    try:
        return max(int(probe_fn(session_id)), 0)
    except Exception:
        return 0  # a broken probe means "cannot ask" — fail closed (§8.4)


# ---------------------------------------------------------------- validation
def _validate_question(args: Mapping[str, Any]) -> dict[str, Any]:
    """Fail-closed sender-side validation (§4.4 item 4).

    Any problem raises `INVALID_ARGUMENT` — the tool result is an error and
    the turn CONTINUES; nothing is emitted, no state changes. Shape/type
    checks ride the same `validate_arguments` the module tools get at the
    router boundary; the kind×options combination is checked explicitly here
    because a wrong pairing must be refused, never guessed around.
    """
    problem = validate_arguments(ASK_USER_SCHEMA, args)
    if problem:
        raise VadeError("INVALID_ARGUMENT", problem, data={"field": "args"})

    kind = args["kind"]
    if kind not in _KINDS:
        raise VadeError("INVALID_ARGUMENT", f"kind must be one of {list(_KINDS)}",
                        data={"field": "kind", "expected": list(_KINDS)})

    prompt = args["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise VadeError("INVALID_ARGUMENT", "prompt must be a non-empty string",
                        data={"field": "prompt", "expected": "non-empty string"})
    if len(prompt) > _PROMPT_MAX:
        raise VadeError("INVALID_ARGUMENT", f"prompt exceeds {_PROMPT_MAX} characters",
                        data={"field": "prompt", "limit": _PROMPT_MAX, "actual": len(prompt)})

    raw_options = args.get("options") or []
    options: list[dict[str, Any]] = []
    if kind in _CHOICE_KINDS:
        if not isinstance(raw_options, list) or not (_OPTIONS_MIN <= len(raw_options) <= _OPTIONS_MAX):
            raise VadeError(
                "INVALID_ARGUMENT",
                f"{kind} requires {_OPTIONS_MIN}..{_OPTIONS_MAX} options",
                data={"field": "options", "expected": f"{_OPTIONS_MIN}..{_OPTIONS_MAX}",
                      "actual": len(raw_options) if isinstance(raw_options, list) else None},
            )
        seen: set[str] = set()
        for item in raw_options:
            option_id = item.get("id") if isinstance(item, Mapping) else None
            label = item.get("label") if isinstance(item, Mapping) else None
            if not isinstance(option_id, str) or not option_id.strip():
                raise VadeError("INVALID_ARGUMENT", "each option needs a non-empty string id",
                                data={"field": "options", "expected": "id:string"})
            if not isinstance(label, str) or not label.strip():
                raise VadeError("INVALID_ARGUMENT", "each option needs a non-empty string label",
                                data={"field": "options", "expected": "label:string"})
            if option_id in seen:
                raise VadeError("INVALID_ARGUMENT", f"duplicate option id {option_id!r}",
                                data={"field": "options", "expected": "unique ids"})
            seen.add(option_id)
            options.append({"id": option_id, "label": label,
                            "description": item.get("description")})
    elif raw_options:
        # Fail-closed on the combination: free_text/confirm take no options.
        raise VadeError("INVALID_ARGUMENT", f"{kind} takes no options",
                        data={"field": "options", "expected": "empty array"})

    timeout_s = args.get("timeout_s")
    if timeout_s is not None and (isinstance(timeout_s, bool) or not isinstance(timeout_s, int)):
        raise VadeError("INVALID_ARGUMENT", "timeout_s must be an integer",
                        data={"field": "timeout_s", "expected": "int"})
    # Out-of-range values are clamped by the channel (§8.1: 越界不报错).

    return {
        "kind": kind,
        "prompt": prompt,
        "options": options,
        "timeout_s": timeout_s,
        "default": args.get("default"),
    }
