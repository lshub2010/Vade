"""Agent loop: LLM -> tool calls -> LLM, with a turn-scoped tool snapshot.

Hard rule (22-C3 §T4): this module knows NO concrete tool name, module id or
path. It only sees `snapshot.get(llm_name)` and `router.call(tool_id, args)`.
The source-grep test in `core/tests/loop/test_engine.py` keeps it that way.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from ..errors import VadeError
from ..router.dispatch import Router, digest_args
from ..session.models import Message, Turn, new_id, now_ms
from .context import CompactionConfig, ContextBuilder
from .ports import ApprovalDecision, ApprovalPort, ApprovalRequest, AuditPort, MemoryPort, NullApprovalPort, NullAuditPort
from .prompt import PromptParts, apply_hooks, assemble
from .toolcard import build_args_preview
from .tools import ToolSnapshot

__all__ = ["LoopEngine", "LoopConfig", "TurnOutcome", "DEFAULT_PERSONA"]


DEFAULT_PERSONA = (
    "You are Vade, a local-first assistant. Use the available tools when needed. "
    "Never invent tool names; if a tool is missing, say so and continue."
)


@dataclass(frozen=True, slots=True)
class LoopConfig:
    max_steps: int = 8
    ctx_tokens: int = 32_000
    approval_timeout_s: int = 60
    persona: str = DEFAULT_PERSONA


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    usage: Mapping[str, Any] = field(default_factory=dict)
    assistant_text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    stopped_reason: str | None = None


class _PartCursors:
    """RFC-0006 §7: `part_index` allocation for `turn.delta`.

    One segment per assistant text block: the first delta of a model step takes
    the next index and every later delta of that step reuses it, so
    "explain → call → explain" becomes 0,1,2 without a `turn.part.started`
    event. A step that streams no text allocates none (indexes may skip).
    """

    def __init__(self) -> None:
        self._next = 0
        self._step: int | None = None

    def begin_step(self) -> None:
        self._step = None

    def text(self) -> int:
        if self._step is None:
            self._step = self._next
            self._next += 1
        return self._step

    def fresh(self) -> int:
        self._step = None
        index = self._next
        self._next += 1
        return index


class LoopEngine:
    def __init__(
        self,
        router: Router | None,
        llm: Any,
        snapshot_factory: Callable[[], ToolSnapshot],
        emit: Callable[..., Any],
        *,
        history: Callable[[str], Sequence[Message]] | None = None,
        approval: ApprovalPort | None = None,
        audit: AuditPort | None = None,
        memory: MemoryPort | None = None,
        prompt_hooks: Callable[[], Sequence[Callable[..., Any]]] | None = None,
        config: LoopConfig | None = None,
        compaction: CompactionConfig | None = None,
        blob_store: Any = None,
    ) -> None:
        self.router = router
        self.llm = llm
        self._snapshot = snapshot_factory
        self._emit = emit
        self._history = history or (lambda session_id: ())
        self.approval = approval or NullApprovalPort()
        self.audit = audit or NullAuditPort()
        self.memory = memory
        self._prompt_hooks = prompt_hooks or (lambda: ())
        self.config = config or LoopConfig()
        self.context = ContextBuilder(compaction or CompactionConfig(), blob_store=blob_store)

    async def run_turn(self, turn: Turn, content: Sequence[Mapping[str, Any]]) -> TurnOutcome:
        if self.llm is None:
            raise VadeError("UNAVAILABLE", "no LLM provider is configured")
        # A turn can be interrupted before its executor even starts (the queue
        # flags it, then creates this coroutine's task). Do not spend a step.
        self._raise_if_interrupted(turn)
        snapshot = self._snapshot()
        if self.router is None or not snapshot:
            text = "No capabilities are available: no module is online, so I cannot run tools."
            await self._turn_emit(
                turn, "turn.delta",
                {"turn_id": turn.id, "text": text, "part_index": 0, "kind": "notice"},
            )
            return TurnOutcome(assistant_text=text, stopped_reason="no_capability",
                               usage=_usage_record(turn.model_id, {}, reported=False, steps=0))

        memory_text = await self._read_memory(turn.session_id)
        parts = PromptParts(
            persona=self.config.persona,
            tool_block=snapshot.render_block(),
            session_memory=memory_text,
            user_message=turn.user_text,
        )
        parts = await apply_hooks(parts, self._prompt_hooks())
        system_messages = assemble(parts)

        history = list(self._history(turn.session_id))
        fitted = self.context.fit(history, ctx_tokens=self.config.ctx_tokens, current_turn_id=turn.id)
        messages: list[dict[str, Any]] = [m.to_llm() for m in system_messages]
        messages.extend(m.to_llm() for m in fitted)

        usage: dict[str, int] = {}
        recorded: list[dict[str, Any]] = []
        final_text = ""
        stopped_reason = "stop"
        reported = False
        llm_calls = 0
        parts = _PartCursors()

        for step in range(self.config.max_steps):
            parts.begin_step()
            text_parts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            call_usage: dict[str, int] = {}
            attribution: dict[str, Any] = {}
            llm_calls += 1
            started = time.perf_counter()
            stream = self._chat(messages, snapshot.schemas(), turn.model_id, attribution)
            try:
                async for delta in stream:
                    # A provider whose `chat()` swallows the cancellation (or answers
                    # after the stream was cancelled) must not append a delta to a
                    # turn the UI already closed. Checked on the write path, so the
                    # invariant does not depend on `llm.chat` re-raising.
                    self._raise_if_interrupted(turn)
                    if delta.text:
                        text_parts.append(delta.text)
                        await self._turn_emit(
                            turn, "turn.delta",
                            {"turn_id": turn.id, "text": delta.text,
                             # RFC-0006 §7: same index for every delta of this step.
                             "part_index": parts.text(), "kind": "text"},
                        )
                    for fragment in delta.tool_calls:
                        slot = calls.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                        if fragment.id:
                            slot["id"] = fragment.id
                        if fragment.name:
                            slot["name"] += fragment.name
                        if fragment.arguments:
                            slot["arguments"] += fragment.arguments
                    if delta.usage:
                        reported = True
                        for key, value in delta.usage.items():
                            if isinstance(value, int):
                                call_usage[key] = call_usage.get(key, 0) + value
                                usage[key] = usage.get(key, 0) + value
            except BaseException as exc:
                # The audit is the only place an operator can see that tokens
                # were spent on a call that then failed (§6.2).
                self._audit_llm_call(turn, attribution, call_usage, started,
                                     status="error", code=_error_code(exc))
                raise
            self._audit_llm_call(turn, attribution, call_usage, started, status="ok", code=None)

            text = "".join(text_parts)
            if text:
                final_text = text
            if not calls:
                break

            assistant = Message(
                id=new_id(), session_id=turn.session_id, turn_id=turn.id,
                role="assistant", content=text,
                tool_calls=tuple(_openai_call(c) for _, c in sorted(calls.items())),
            )
            messages.append(assistant.to_llm())
            # RFC-0006 §5.3: one id per assistant model step. It means "same
            # batch", never "parallel" — v1 awaits these calls one by one.
            batch_id = "bat_" + new_id()[:16]
            first = True
            for index in sorted(calls):
                call = calls[index]
                call_id = call["id"] or f"call-{turn.id[:8]}-{step}-{index}"
                name = call["name"]
                args = _parse_args(call["arguments"])
                entry = snapshot.get(name)
                # An unknown tool has no manifest, so no risk level was ever
                # vetted for it: preview it as high (shape only) rather than
                # guessing `low` and echoing arguments nobody approved.
                risk = getattr(entry, "risk", None) or "high"
                tool_call_payload = {
                    "turn_id": turn.id, "call_id": call_id, "tool": name,
                    # The digest is untouched by RFC-0006: it stays the audit
                    # and approval anchor, and `args_preview` never feeds it.
                    "args_digest": digest_args(args),
                    "started_at": now_ms(),
                    "batch_id": batch_id,
                    **build_args_preview(args, risk),
                }
                # `turn.tool_call` is a write into the turn stream like any
                # other, and it is the last one before the side effect: a
                # provider that ate the cancel and ended its stream lands right
                # here, so this guarded emit is what keeps the tool from
                # running inside a turn the UI already closed.
                if first:
                    await self._turn_emit(
                        turn, "turn.tool_call", tool_call_payload, messages=[assistant]
                    )
                    first = False
                else:
                    await self._turn_emit(turn, "turn.tool_call", tool_call_payload)

                if entry is None:
                    self._raise_if_interrupted(turn)
                    message = (
                        f"Tool {name!r} does not exist. Available tools: "
                        + (", ".join(snapshot.names()) or "(none)")
                        + ". Continue without it."
                    )
                    tool_message = Message(id=new_id(), session_id=turn.session_id,
                                           turn_id=turn.id, role="tool", name=name,
                                           tool_call_id=call_id, content=message)
                    await self._turn_emit(
                        turn, "turn.tool_result",
                        {"turn_id": turn.id, "call_id": call_id, "ok": False,
                         "summary": f"unknown tool {name!r}",
                         # Not executed, so no duration: `null`, never `0ms`.
                         "duration_ms": None, "truncated": False,
                         "result_ref": _result_ref(tool_message)},
                        messages=[tool_message],
                    )
                    messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": message})
                    recorded.append({"tool": name, "args_digest": digest_args(args), "ok": False})
                    continue

                decision = await self._check_approval(turn, entry, args)
                if not decision.allowed:
                    self._raise_if_interrupted(turn)
                    message = f"Tool call denied: {decision.reason or 'denied by policy'}. Continue without it."
                    if decision.request_id:
                        await self._turn_emit(
                            turn, "turn.approval_resolved",
                            {"turn_id": turn.id, "request_id": decision.request_id,
                             "decision": "deny", "by": decision.by or "policy",
                             "scope": decision.scope, "state": "running"},
                        )
                    messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": message})
                    tool_message = Message(id=new_id(), session_id=turn.session_id,
                                           turn_id=turn.id, role="tool", name=name,
                                           tool_call_id=call_id, content=message)
                    await self._turn_emit(
                        turn, "turn.tool_result",
                        {"turn_id": turn.id, "call_id": call_id, "ok": False,
                         "summary": "denied",
                         "duration_ms": None, "truncated": False,
                         "result_ref": _result_ref(tool_message)},
                        messages=[tool_message],
                    )
                    recorded.append({"tool": name, "args_digest": digest_args(args), "ok": False})
                    continue

                result = await self.router.call(
                    entry.tool_id, args, session_id=turn.session_id, turn_id=turn.id, call_id=call_id
                )
                # The side effect already happened (the module ran), so audit it
                # BEFORE the interruption guard below can stop the turn: after
                # the guard there is no code path left that could write it, and
                # an interrupted turn would leave no trace of a real tool call.
                audit_event: dict[str, Any] = {
                    "kind": "tool_call", "session_id": turn.session_id, "turn_id": turn.id,
                    "tool": entry.tool_id, "ok": result.ok, "code": result.code,
                }
                interrupted = self._is_interrupted(turn)
                if interrupted:
                    audit_event["aborted"] = "interrupted"
                self._audit(audit_event)
                if interrupted:
                    raise asyncio.CancelledError
                summary, summary_cut = _summary(result)
                tool_content, content_cut = self._tool_content(entry, result)
                tool_message = Message(id=new_id(), session_id=turn.session_id,
                                       turn_id=turn.id, role="tool", name=name,
                                       tool_call_id=call_id, content=tool_content)
                payload = {
                    "turn_id": turn.id, "call_id": call_id, "ok": result.ok,
                    "summary": summary,
                    # RFC-0006 §5.3: measured by the router at call return,
                    # excluding approval wait and queueing.
                    "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
                    # Either the 300-char summary window cut it, or the full
                    # text overflowed to the blob store.
                    "truncated": bool(summary_cut or content_cut),
                    "result_ref": _result_ref(tool_message),
                }
                if not result.ok:
                    payload["code"] = result.code
                await self._turn_emit(
                    turn, "turn.tool_result", payload, messages=[tool_message],
                )
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": tool_content})
                recorded.append({"tool": name, "args_digest": digest_args(args), "ok": result.ok})
            else:
                continue

        else:
            # for/else: the last allowed step still produced tool calls
            self._raise_if_interrupted(turn)
            stopped_reason = "max_steps"
            notice = (f"Stopped after {self.config.max_steps} steps to avoid an endless tool loop. "
                      "Ask me to continue if you want me to keep going.")
            final_text = (final_text + "\n" + notice).strip()
            # RFC-0006 §7.2: the notice is its own segment, so a client can
            # render it as a system-ish line instead of appending it to the
            # model's final answer.
            await self._turn_emit(
                turn, "turn.delta",
                {"turn_id": turn.id, "text": notice,
                 "part_index": parts.fresh(), "kind": "notice"},
            )

        if self._is_interrupted(turn):
            # The stream ended (or the step budget ran out) after the user
            # interrupted, so `queue.interrupt` already wrote the terminal
            # event: this turn did NOT complete and must not be audited as one
            # — the audit is the only place an operator can tell the two apart.
            self._audit({"kind": "turn_interrupted", "session_id": turn.session_id,
                         "turn_id": turn.id, "stopped_reason": stopped_reason,
                         "tool_calls": len(recorded)})
            raise asyncio.CancelledError
        self._audit({"kind": "turn_completed", "session_id": turn.session_id, "turn_id": turn.id,
                     "stopped_reason": stopped_reason, "tool_calls": len(recorded)})
        return TurnOutcome(
            usage=_usage_record(turn.model_id, usage, reported=reported, steps=llm_calls),
            assistant_text=final_text, tool_calls=tuple(recorded),
            stopped_reason=stopped_reason,
        )

    # ------------------------------------------------------------------ helpers
    def _chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        model_id: str | None,
        attribution: dict[str, Any],
    ) -> AsyncIterator[Any]:
        """One provider call, with the RFC-0005 append applied by capability.

        `chat(..., model=)` is the internal (non-contract) protocol change of
        §7.1 row 9: it pins the model a turn resolved. A provider that predates
        it — a test double, a third-party one — must keep working, so the kwarg
        is passed only when the signature accepts it. `attribution` is filled by
        a module provider with the target it actually used, which is what makes
        `llm.call` attributable to exactly one module (§6.2).
        """
        fn = self.llm.chat
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            params = {}
        kwargs: dict[str, Any] = {"tools": tools, "stream": True}
        if "model" in params:
            kwargs["model"] = model_id
        if "attribution" in params:
            kwargs["attribution"] = attribution
        return fn(messages, **kwargs)

    def _audit_llm_call(
        self,
        turn: Turn,
        attribution: Mapping[str, Any],
        call_usage: Mapping[str, int],
        started: float,
        *,
        status: str,
        code: str | None,
    ) -> None:
        """One `llm.call` row per core-initiated model call (§6.2).

        Counts and model name only: no prompt, no completion, no upstream body,
        no `base_url` (it can carry credentials, §10). `extra.tokens` is kept
        because `admin/app.py` already reads it.
        """
        provider = turn.model_id.split("/", 1)[0] if turn.model_id else None
        total = int(call_usage.get("total_tokens", 0) or 0)
        self._audit({
            "kind": "llm.call",
            "session_id": turn.session_id,
            "turn_id": turn.id,
            "module_id": attribution.get("module_id"),
            "instance_id": attribution.get("instance_id"),
            "method": "chat",
            "status": status,
            "code": code,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "extra": {
                "model": turn.model_id,
                "provider": provider,
                "purpose": "chat",
                "prompt_tokens": int(call_usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(call_usage.get("completion_tokens", 0) or 0),
                "total_tokens": total,
                "tokens": total,
                "source": "provider" if call_usage else "none",
            },
        })

    def _is_interrupted(self, turn: Turn) -> bool:
        """True once `queue.interrupt` has flagged this turn on the router.

        Every write into the turn stream goes through `_turn_emit`, and
        the `turn_completed` audit consults this predicate directly, so
        "nothing is written after `turn.interrupted`" is an invariant of THIS
        module — `turn.delta`, `turn.tool_result`, `turn.tool_call` and the
        max_steps notice included. It deliberately does not rely on an awaited
        call re-raising the cancellation: two producers can resume a turn the
        UI already closed — a module that swallows `tools/cancel`, so
        `router.call` returns a normal result, and an LLM provider whose
        `chat()` swallows it, so the stream keeps yielding. The check sits on
        the write path, not on the await that was supposed to raise, so
        neither can append to a closed turn.
        """
        probe = getattr(self.router, "was_cancelled", None)
        return bool(callable(probe) and probe(turn.id))

    def _raise_if_interrupted(self, turn: Turn) -> None:
        """Stop the turn iff `queue.interrupt` already closed it."""
        if self._is_interrupted(turn):
            raise asyncio.CancelledError

    async def _turn_emit(
        self,
        turn: Turn,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        messages: Sequence[Message] = (),
    ) -> None:
        """The ONE way this module writes into a turn's event stream.

        The guard lives on the emit, not next to the await that produced the
        data, so a write site cannot be added without it: the producers that
        outlive `turn.interrupted` (a module or provider that swallows the
        cancellation) would otherwise keep appending to a closed turn.

        The name is `_turn_emit` and not something longer on purpose:
        `tools/check_contracts.py` finds emitted event names by scanning calls
        of the form `emit(<session or turn>, "<event name literal>")`, so every
        guarded write must stay visible to that C-06 gate.
        """
        self._raise_if_interrupted(turn)
        await self._emit(turn.session_id, event_type, payload, messages=messages)

    async def _check_approval(self, turn: Turn, entry: Any, args: Mapping[str, Any]) -> ApprovalDecision:
        request = ApprovalRequest(
            session_id=turn.session_id, turn_id=turn.id, tool_id=entry.tool_id, args=args,
            risk=entry.risk, expires_at=now_ms() + self.config.approval_timeout_s * 1000,
            emit=lambda event_type, payload: self._turn_emit(turn, event_type, payload),
        )
        try:
            return await self.approval.check(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail closed: a broken approver must never silently allow a call.
            return ApprovalDecision(False, reason="approval_error")

    async def _read_memory(self, session_id: str) -> str:
        if self.memory is None:
            return ""
        try:
            items = await self.memory.read_session(session_id)
        except Exception:
            return ""  # M4a absent must not block a turn
        return "\n".join(getattr(item, "text", "") for item in items if getattr(item, "text", ""))

    def _tool_content(self, entry: Any, result: Any) -> tuple[str, bool]:
        """`(content, truncated)` — truncated means the full text left the message.

        `ToolCallResult.to_tool_result()` reports `truncated=false` unconditionally
        (it has no idea what the context window can hold), so the flag has to be
        computed here, at the site that decides to write a blob (RFC-0006 §5.3).
        """
        if not result.ok:
            return json.dumps({"ok": False, "error": {"code": result.code, "message": result.message}},
                              ensure_ascii=False), False
        text = json.dumps(result.result, ensure_ascii=False, default=str)
        limit = self.context.config.max_tool_result_tokens * 4
        if len(text) <= limit:
            return text, False
        if self.context.blobs is not None:
            blob_id = self.context.blobs.put(text)
            return f"[tool result too large; blob_id={blob_id}] " + text[:500], True
        return text[:limit], True

    def _audit(self, event: Mapping[str, Any]) -> None:
        try:
            self.audit.record(event)
        except Exception:
            pass


def _usage_record(
    model_id: str | None,
    usage: Mapping[str, Any],
    *,
    reported: bool,
    steps: int,
) -> dict[str, Any]:
    """`turn.completed.usage` — the three frozen keys plus RFC-0005 §6.1.

    The three existing keys keep their names, types and meaning; everything
    else is appended. `source="none"` means "the provider never reported", in
    which case every count is 0 — core does not estimate tokens.
    """
    provider = model_id.split("/", 1)[0] if model_id else None

    def count(key: str) -> int:
        if not reported:
            return 0
        value = usage.get(key, 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    return {
        "prompt_tokens": count("prompt_tokens"),
        "completion_tokens": count("completion_tokens"),
        "total_tokens": count("total_tokens"),
        "model_id": model_id,
        "provider": provider,
        "source": "provider" if reported else "none",
        "steps": int(steps),
        "cached_tokens": count("cached_tokens"),
        "reasoning_tokens": count("reasoning_tokens"),
    }


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "CANCELLED"
    return getattr(exc, "code", None) or "INTERNAL"


def _parse_args(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"__raw__": raw}
    return value if isinstance(value, dict) else {"value": value}


def _openai_call(call: Mapping[str, str]) -> dict[str, Any]:
    return {
        "id": call.get("id") or "",
        "type": "function",
        "function": {"name": call.get("name", ""), "arguments": call.get("arguments", "")},
    }


def _summary(result: Any) -> tuple[str, bool]:
    """`(summary, truncated)` — truncated when the 300-char window cut it."""
    if result.ok:
        text = json.dumps(result.result, ensure_ascii=False, default=str)
    else:
        text = f"{result.code}: {result.message}"
    return text[:300], len(text) > 300


def _result_ref(message: Message) -> str:
    """`msg:<hex>` pointer a UI can resolve in `snapshot.messages[]`."""
    return f"msg:{message.id}"
