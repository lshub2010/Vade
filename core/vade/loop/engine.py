"""Agent loop: LLM -> tool calls -> LLM, with a turn-scoped tool snapshot.

Hard rule (22-C3 §T4): this module knows NO concrete tool name, module id or
path. It only sees `snapshot.get(llm_name)` and `router.call(tool_id, args)`.
The source-grep test in `core/tests/loop/test_engine.py` keeps it that way.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from ..errors import VadeError
from ..router.dispatch import Router, digest_args
from ..session.models import Message, Turn, new_id, now_ms
from .context import CompactionConfig, ContextBuilder
from .ports import ApprovalDecision, ApprovalPort, ApprovalRequest, AuditPort, MemoryPort, NullApprovalPort, NullAuditPort
from .prompt import PromptParts, apply_hooks, assemble
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
        snapshot = self._snapshot()
        if self.router is None or not snapshot:
            text = "No capabilities are available: no module is online, so I cannot run tools."
            await self._emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": text})
            return TurnOutcome(assistant_text=text, stopped_reason="no_capability")

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

        usage: dict[str, Any] = {}
        recorded: list[dict[str, Any]] = []
        final_text = ""
        stopped_reason = "stop"

        for step in range(self.config.max_steps):
            text_parts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            async for delta in self.llm.chat(messages, tools=snapshot.schemas(), stream=True):
                if delta.text:
                    text_parts.append(delta.text)
                    await self._emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": delta.text})
                for fragment in delta.tool_calls:
                    slot = calls.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                    if fragment.id:
                        slot["id"] = fragment.id
                    if fragment.name:
                        slot["name"] += fragment.name
                    if fragment.arguments:
                        slot["arguments"] += fragment.arguments
                if delta.usage:
                    for key, value in delta.usage.items():
                        if isinstance(value, int):
                            usage[key] = usage.get(key, 0) + value

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
            first = True
            for index in sorted(calls):
                call = calls[index]
                call_id = call["id"] or f"call-{turn.id[:8]}-{step}-{index}"
                name = call["name"]
                args = _parse_args(call["arguments"])
                entry = snapshot.get(name)
                if first:
                    await self._emit(
                        turn.session_id, "turn.tool_call",
                        {"turn_id": turn.id, "call_id": call_id, "tool": name,
                         "args_digest": digest_args(args)},
                        messages=[assistant],
                    )
                    first = False
                else:
                    await self._emit(
                        turn.session_id, "turn.tool_call",
                        {"turn_id": turn.id, "call_id": call_id, "tool": name,
                         "args_digest": digest_args(args)},
                    )

                if entry is None:
                    message = (
                        f"Tool {name!r} does not exist. Available tools: "
                        + (", ".join(snapshot.names()) or "(none)")
                        + ". Continue without it."
                    )
                    await self._emit(
                        turn.session_id, "turn.tool_result",
                        {"turn_id": turn.id, "call_id": call_id, "ok": False,
                         "summary": f"unknown tool {name!r}"},
                        messages=[Message(id=new_id(), session_id=turn.session_id, turn_id=turn.id,
                                          role="tool", name=name, tool_call_id=call_id,
                                          content=message)],
                    )
                    messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": message})
                    recorded.append({"tool": name, "args_digest": digest_args(args), "ok": False})
                    continue

                decision = await self._check_approval(turn, entry, args)
                if not decision.allowed:
                    message = f"Tool call denied: {decision.reason or 'denied by policy'}. Continue without it."
                    if decision.request_id:
                        await self._emit(
                            turn.session_id, "turn.approval_resolved",
                            {"turn_id": turn.id, "request_id": decision.request_id,
                             "decision": "deny", "by": decision.by or "policy",
                             "scope": decision.scope, "state": "running"},
                        )
                    messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": message})
                    await self._emit(
                        turn.session_id, "turn.tool_result",
                        {"turn_id": turn.id, "call_id": call_id, "ok": False,
                         "summary": "denied"},
                        messages=[Message(id=new_id(), session_id=turn.session_id, turn_id=turn.id,
                                          role="tool", name=name, tool_call_id=call_id,
                                          content=message)],
                    )
                    recorded.append({"tool": name, "args_digest": digest_args(args), "ok": False})
                    continue

                result = await self.router.call(
                    entry.tool_id, args, session_id=turn.session_id, turn_id=turn.id, call_id=call_id
                )
                summary = _summary(result)
                payload = {"turn_id": turn.id, "call_id": call_id, "ok": result.ok, "summary": summary}
                if not result.ok:
                    payload["code"] = result.code
                tool_content = self._tool_content(entry, result)
                await self._emit(
                    turn.session_id, "turn.tool_result", payload,
                    messages=[Message(id=new_id(), session_id=turn.session_id, turn_id=turn.id,
                                      role="tool", name=name, tool_call_id=call_id,
                                      content=tool_content)],
                )
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": tool_content})
                recorded.append({"tool": name, "args_digest": digest_args(args), "ok": result.ok})
                self._audit({"kind": "tool_call", "session_id": turn.session_id, "turn_id": turn.id,
                             "tool": entry.tool_id, "ok": result.ok, "code": result.code})
            else:
                continue

        else:
            # for/else: the last allowed step still produced tool calls
            stopped_reason = "max_steps"
            notice = (f"Stopped after {self.config.max_steps} steps to avoid an endless tool loop. "
                      "Ask me to continue if you want me to keep going.")
            final_text = (final_text + "\n" + notice).strip()
            await self._emit(turn.session_id, "turn.delta", {"turn_id": turn.id, "text": notice})

        self._audit({"kind": "turn_completed", "session_id": turn.session_id, "turn_id": turn.id,
                     "stopped_reason": stopped_reason, "tool_calls": len(recorded)})
        return TurnOutcome(
            usage=usage, assistant_text=final_text, tool_calls=tuple(recorded),
            stopped_reason=stopped_reason,
        )

    # ------------------------------------------------------------------ helpers
    async def _check_approval(self, turn: Turn, entry: Any, args: Mapping[str, Any]) -> ApprovalDecision:
        request = ApprovalRequest(
            session_id=turn.session_id, turn_id=turn.id, tool_id=entry.tool_id, args=args,
            risk=entry.risk, expires_at=now_ms() + self.config.approval_timeout_s * 1000,
            emit=lambda event_type, payload: self._emit(turn.session_id, event_type, payload),
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

    def _tool_content(self, entry: Any, result: Any) -> str:
        if not result.ok:
            return json.dumps({"ok": False, "error": {"code": result.code, "message": result.message}},
                              ensure_ascii=False)
        text = json.dumps(result.result, ensure_ascii=False, default=str)
        limit = self.context.config.max_tool_result_tokens * 4
        if len(text) <= limit:
            return text
        if self.context.blobs is not None:
            blob_id = self.context.blobs.put(text)
            return f"[tool result too large; blob_id={blob_id}] " + text[:500]
        return text[:limit]

    def _audit(self, event: Mapping[str, Any]) -> None:
        try:
            self.audit.record(event)
        except Exception:
            pass


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


def _summary(result: Any) -> str:
    if result.ok:
        text = json.dumps(result.result, ensure_ascii=False, default=str)
        return text[:300]
    return f"{result.code}: {result.message}"[:300]
