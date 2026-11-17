"""Agent loop: LLM -> tool calls -> LLM, with a turn-scoped tool snapshot.

Hard rule (22-C3 §T4): this module knows NO concrete tool name, module id or
path. It only sees `snapshot.get(llm_name)` and `router.call(tool_id, args)`.
The source-grep test in `core/tests/loop/test_engine.py` keeps it that way.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import time
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

from ..errors import VadeError
from ..router.dispatch import Router, ToolCallResult, digest_args
from ..session.models import Message, Turn, new_id, now_ms
from .builtins import BuiltinContext, BuiltinRegistry
from .catalog import is_catalog_message, parse_catalog_digest
from .context import CompactionConfig, ContextBuilder
from .coretools import STICKY_K, more_tools_line
from .ports import ApprovalDecision, ApprovalPort, ApprovalRequest, AuditPort, MemoryPort, NullApprovalPort, NullAuditPort
from .prompt import PromptParts, apply_hooks, assemble
from .toolcard import build_args_preview
from .tools import ToolEntry, ToolSnapshot, resolve_hot

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
    #: RFC-0003 §7: prompt.injection — "legacy" (default) keeps the pre-RFC
    #: assembly byte-identical; "layered" enables stable/context + catalog
    #: message + hot/cold tools. A restart item on purpose (§7: no hot switch).
    injection: str = "legacy"
    #: RFC-0003 §4.3: tools.hot_max (C-18, default 12) and tools.hot (default []).
    hot_max: int = 12
    hot_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    usage: Mapping[str, Any] = field(default_factory=dict)
    assistant_text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    stopped_reason: str | None = None
    #: RFC-0002: skill NAMES the prompt hooks activated this turn — verbatim
    #: pass-through of `PromptParts.activated_skills` (the loader is the
    #: normalization point: sorted names, never bodies; the loop never re-sorts,
    #: dedups or truncates). `()` when no hook activated anything. The queue
    #: emits it as `turn.completed.skills` (always present, `[]` when empty).
    activated_skills: tuple[str, ...] = ()


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
        core_tools: Mapping[str, Any] | None = None,
        catalog: Any = None,
        builtins: BuiltinRegistry | None = None,
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
        # RFC-0003 §4.2/§4.3: core-native tools and the catalog port. Both are
        # wired by the session component in layered mode only; in legacy they
        # stay empty and every code path below is the pre-RFC one.
        self._core_tools: dict[str, Any] = dict(core_tools or {})
        self._catalog = catalog
        self._session_hot: dict[str, list[str]] = {}
        self._layered = self.config.injection == "layered"
        # RFC-0008 §14: core-owned tools (`ask_user`) that need no module. The
        # engine only sees the registry interface — never a concrete name.
        self.builtins = builtins

    async def run_turn(self, turn: Turn, content: Sequence[Mapping[str, Any]]) -> TurnOutcome:
        if self.llm is None:
            raise VadeError("UNAVAILABLE", "no LLM provider is configured")
        # A turn can be interrupted before its executor even starts (the queue
        # flags it, then creates this coroutine's task). Do not spend a step.
        self._raise_if_interrupted(turn)
        snapshot = self._snapshot()
        layered = self._layered
        if layered and self._core_tools:
            # Core-native entries join the frozen table BEFORE the no-capability
            # gate (§4.2.4: the core tools are resident hot tools): in layered
            # mode a modules-less core still carries the catalog and the core
            # tools, so the notice must not swallow them. Legacy never grafts
            # anything, so its gate (and golden bytes) stay untouched.
            snapshot = snapshot.with_core(self._core_entries())
        # Built-ins (`ask_user`, RFC-0008 §14) need no module: a turn with an
        # empty module snapshot can still ask the user a question.
        if self.router is None or (not snapshot and not self.builtins):
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
            tool_block=self._tool_block(snapshot),
            session_memory=memory_text,
            user_message=turn.user_text,
            context=_runtime_facts(turn.model_id) if layered else "",
        )
        initial_block = parts.tool_block
        parts = await apply_hooks(parts, self._prompt_hooks())
        # RFC-0002 §2: record the hook-activated skill names NOW. `parts` is
        # rebound to the part-index cursor further down, so this is the only
        # point where PromptParts.activated_skills is still reachable.
        activated_skills = tuple(parts.activated_skills or ())
        visible: set[str] = set()
        if layered and parts.tool_block == initial_block:
            # Hot set after hooks, so layer 2 (a just-activated skill's
            # requires_tools) is included (§4.3.1). A hook that customized
            # tool_block wins over the hot projection.
            hot, dropped = self._resolve_hot(turn, parts, snapshot)
            visible = set(hot)
            block = snapshot.select(hot).render_block()
            if dropped:
                block = f"{block}\n{more_tools_line(dropped)}" if block else more_tools_line(dropped)
            parts = replace(parts, tool_block=block)
        elif layered:
            visible = {entry.llm_name for entry in snapshot.entries}
        system_messages = assemble(parts, injection="layered" if layered else "legacy")

        history = list(self._history(turn.session_id))
        if layered and self._catalog is not None:
            history = await self._inject_catalog(turn, history)
        # RFC-0003 Q5: the catalog reminder is semantically load-bearing, so it
        # must survive fitting; in legacy no catalog message can be in history
        # (the session wiring filters them out), so the predicate stays off and
        # the legacy path is byte-identical to pre-Q5 behaviour.
        fitted = self.context.fit(
            history, ctx_tokens=self.config.ctx_tokens, current_turn_id=turn.id,
            protect=self._catalog_keeper if layered else None,
        )
        messages: list[dict[str, Any]] = [m.to_llm() for m in system_messages]
        messages.extend(m.to_llm() for m in fitted)

        # One frozen tool surface per turn: module snapshot + built-in tools,
        # in that order (the snapshot is per-turn; built-ins are static).
        tools = list(snapshot.schemas())
        if self.builtins:
            tools.extend(self.builtins.schemas())

        usage: dict[str, int] = {}
        recorded: list[dict[str, Any]] = []
        final_text = ""
        stopped_reason = "stop"
        reported = False
        llm_calls = 0
        parts = _PartCursors()
        start_visible = frozenset(visible)

        for step in range(self.config.max_steps):
            parts.begin_step()
            text_parts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            call_usage: dict[str, int] = {}
            attribution: dict[str, Any] = {}
            llm_calls += 1
            started = time.perf_counter()
            tools = list(snapshot.schemas())
            if layered:
                # Hot ∪ discovered so far; every entry still comes from the
                # frozen snapshot, and a discovery joins at the NEXT step
                # boundary (§4.3.3 — no extra round trip, one schema change).
                tools = list(snapshot.select(visible).schemas())
            if self.builtins:
                # RFC-0008 §14: core-owned tools are appended after the frozen
                # module table, so their position never shifts a module entry.
                tools.extend(self.builtins.schemas())
            stream = self._chat(messages, tools, turn.model_id, attribution)
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
                # Built-ins are consulted only when the module snapshot has no
                # such name; the engine stays name-blind either way.
                builtin = self.builtins.get(name) if self.builtins else None
                target = entry if entry is not None else builtin
                # An unknown tool has no manifest, so no risk level was ever
                # vetted for it: preview it as high (shape only) rather than
                # guessing `low` and echoing arguments nobody approved.
                risk = getattr(target, "risk", None) or "high"
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

                if target is None:
                    self._raise_if_interrupted(turn)
                    available = list(snapshot.names())
                    if self.builtins:
                        available.extend(self.builtins.names())
                    message = (
                        f"Tool {name!r} does not exist. Available tools: "
                        + (", ".join(available) or "(none)")
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

                decision = await self._check_approval(turn, target, args)
                if not decision.allowed:
                    self._raise_if_interrupted(turn)
                    message = f"Tool call denied: {decision.reason or 'denied by policy'}. Continue without it."
                    if decision.request_id:
                        # Same payload shape as the allow path
                        # (`ApprovalManager.decide`): a device that missed the
                        # prompt matches the card by `session_id` + the §7.1
                        # attribution, and a deny used to publish only
                        # `turn_id`, so it could not close the right card in a
                        # subagent's parent session.
                        await self._turn_emit(
                            turn, "turn.approval_resolved",
                            {"turn_id": turn.id, "request_id": decision.request_id,
                             "decision": "deny", "by": decision.by or "policy",
                             "scope": decision.scope, "state": "running",
                             "session_id": turn.session_id,
                             **_approval_attribution(turn)},
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

                if builtin is not None:
                    # Core-owned tool: executed here, not through the router.
                    # The manager owns blocking, timeout, cancel and the
                    # `turn.input_required`/`turn.input_resolved` pair.
                    # RFC-0003's layered bookkeeping (`core_tool`) does not apply:
                    # a builtin never joins the snapshot nor the discovery set.
                    result = await self._call_builtin(turn, builtin, args)
                    core_tool = None
                else:
                    result, core_tool = await self._call_tool(turn, name, entry, args, snapshot, layered, call_id)
                # The side effect already happened (the module ran), so audit it
                # BEFORE the interruption guard below can stop the turn: after
                # the guard there is no code path left that could write it, and
                # an interrupted turn would leave no trace of a real tool call.
                audit_event: dict[str, Any] = {
                    "kind": "tool_call", "session_id": turn.session_id, "turn_id": turn.id,
                    "tool": target.tool_id, "ok": result.ok, "code": result.code,
                }
                interrupted = self._is_interrupted(turn)
                if interrupted:
                    audit_event["aborted"] = "interrupted"
                self._audit(audit_event)
                if interrupted:
                    raise asyncio.CancelledError
                summary, summary_cut = _summary(result)
                tool_content, content_cut = self._tool_content(target, result)
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
                if layered and core_tool is not None:
                    # Step-boundary expansion (§4.3.3): a discovery or a loaded
                    # skill's requires_tools join the NEXT step's schemas.
                    await self._absorb(turn, core_tool, result, snapshot, visible, start_visible)
                elif layered and result.ok:
                    # §4.3.1 layer 4: last successful module calls stay sticky.
                    self._sticky_add(turn.session_id, name)
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
            activated_skills=activated_skills,
        )

    # ------------------------------------------------------------------ helpers
    def _core_entries(self) -> tuple[ToolEntry, ...]:
        """ToolEntry projections of the core-native tools (§4.2.4). The loop
        never names them: the specs live in `loop.coretools`."""
        return tuple(
            ToolEntry(
                llm_name=tool.llm_name, tool_id=tool.tool_id, module_id="core",
                description=tool.description, input_schema=dict(tool.input_schema),
                risk=tool.risk, timeout_ms=0, idempotent=True,
            )
            for tool in self._core_tools.values()
        )

    def _resolve_hot(self, turn: Turn, parts: PromptParts, snapshot: ToolSnapshot) -> tuple[tuple[str, ...], int]:
        """§4.3.1: four-layer hot set, resolved against the frozen snapshot."""
        requires: tuple[str, ...] = ()
        loaded = tuple(parts.activated_skills or ())
        if loaded and self._catalog is not None:
            try:
                requires = tuple(self._catalog.requires(loaded))
            except Exception:  # a broken catalog port must not break the turn
                requires = ()
        known = tuple(self._session_hot.get(turn.session_id, ()))
        return resolve_hot(
            snapshot=snapshot,
            core_names=tuple(self._core_tools),
            config_hot=self.config.hot_tools,
            session_known=known,
            skill_requires=requires,
            hot_max=self.config.hot_max,
        )

    def _entry_for_name(self, snapshot: ToolSnapshot, name: str) -> ToolEntry | None:
        """Resolve an LLM name OR a protocol tool_id (skills may declare either)."""
        from .tools import normalize_tool_key

        key = normalize_tool_key(name)
        if not key:
            return None
        for entry in snapshot.entries:
            if normalize_tool_key(entry.llm_name) == key or normalize_tool_key(entry.tool_id) == key:
                return entry
        return None

    def _sticky_add(self, session_id: str, name: str) -> None:
        """§4.3.1 layer 4: session-sticky tools, most recent last, capped at K."""
        known = self._session_hot.setdefault(session_id, [])
        if name in known:
            known.remove(name)
        known.append(name)
        del known[:-STICKY_K]
        while len(self._session_hot) > 128:  # prune idle sessions, oldest first
            self._session_hot.pop(next(iter(self._session_hot)))

    async def _absorb(
        self,
        turn: Turn,
        core_tool: Any,
        result: Any,
        snapshot: ToolSnapshot,
        visible: set[str],
        start_visible: frozenset[str],
    ) -> None:
        """Discover / require-enable tools for the next step (§4.3.3).

        Everything is sourced from the frozen snapshot; `turn.tool_discovered`
        fires once per tool that was NOT resident when the turn began.
        """
        meta = core_tool.discovery_meta(result)
        for raw_name in (*core_tool.discovered(result), *core_tool.requires(result)):
            entry = self._entry_for_name(snapshot, str(raw_name))
            if entry is None or entry.llm_name in visible:
                continue
            visible.add(entry.llm_name)
            self._sticky_add(turn.session_id, entry.llm_name)
            if entry.llm_name in start_visible:
                continue
            await self._turn_emit(turn, "turn.tool_discovered", {
                "turn_id": turn.id, "tool": entry.llm_name, "tool_id": entry.tool_id,
                "query_digest": str(meta.get("query_digest") or ""),
                "total_matches": int(meta.get("total_matches") or 0),
            })

    async def _call_tool(
        self,
        turn: Turn,
        name: str,
        entry: Any,
        args: Mapping[str, Any],
        snapshot: ToolSnapshot,
        layered: bool,
        call_id: str,
    ) -> tuple[Any, Any]:
        """Router dispatch, or the core handler for a core-native tool (§4.2.4).

        A core tool never reaches the router; its handler is duck-typed and
        returns an ok/code/message envelope that maps onto `ToolCallResult`, so
        the audit / result / blob path below stays shared. Returns
        `(result, core_tool_or_None)` — the loop needs the CoreTool afterwards
        to absorb discoveries and skill requirements.
        """
        core_tool = self._core_tools.get(name) if layered else None
        if core_tool is None:
            if self.router is None:  # unreachable past the no-capability gate
                raise VadeError("UNAVAILABLE", "no router is configured")
            return (
                await self.router.call(
                    entry.tool_id, args, session_id=turn.session_id, turn_id=turn.id, call_id=call_id
                ),
                None,
            )
        try:
            raw = await core_tool.handler(args, snapshot, session_id=turn.session_id, turn_id=turn.id)
        except Exception as exc:  # fail closed, like a broken approver
            return (
                ToolCallResult(ok=False, tool_id=entry.tool_id, code=getattr(exc, "code", None) or "INTERNAL",
                               message=str(exc)),
                core_tool,
            )
        return (
            ToolCallResult(
                ok=bool(raw.get("ok")), tool_id=core_tool.tool_id, result=raw.get("result"),
                code=raw.get("code") or None, message=str(raw.get("message") or ""),
            ),
            core_tool,
        )

    async def _inject_catalog(self, turn: Turn, history: list[Message]) -> list[Message]:
        """§4.2.2: resend decision against the LOG, not memory.

        The baseline is the last visible catalog message in the session log, so
        a restart / reconnect / compaction cannot make the loop think a catalog
        is still in front of the model. `None` baseline = first turn = always
        send; same digest = send nothing; new digest or empty catalog = one full
        replacement message (user role, persisted with its own seq).
        """
        info = self._catalog.view(turn.user_text or "")
        last: str | None = None
        for message in reversed(history):
            digest = parse_catalog_digest(getattr(message, "content", ""))
            if digest is not None and getattr(message, "role", "") == "user":
                last = digest
                break
        if last == info.digest:
            return history
        message = Message(
            id=new_id(), session_id=turn.session_id, turn_id=turn.id,
            role="user", content=info.text, created_at=now_ms(),
        )
        await self._turn_emit(
            turn, "turn.skill_catalog",
            {"turn_id": turn.id, "digest": info.digest, "entries": list(info.names),
             "empty": bool(info.empty)},
            messages=[message],
        )
        history.append(message)
        return history

    def _catalog_keeper(self, message: Message) -> bool:
        """RFC-0003 Q5 (§4.2.2): which messages compaction must never lose.

        The catalog reminder is an ordinary user-role message; once it sits
        deeper than `protect_last_n` in the history, `fit()` would drop it from
        the fitted view while `_inject_catalog`'s resend decision — made
        against the RAW log, which still holds it — sees the digest as
        unchanged and sends nothing. The model would then run with no visible
        catalog until the digest next changes, and every turn in between is
        broken. Protecting EVERY catalog message (not just the last) also
        keeps the fitted bytes stable across turns: a drop anywhere shifts the
        whole history after it, which a provider prefix cache would pay for.
        The cost is bounded by how often digests change (install / uninstall /
        enable / disable), not by session length.
        """
        return message.role == "user" and is_catalog_message(message.content)

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
            **_approval_attribution(turn),
        )
        try:
            return await self.approval.check(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail closed: a broken approver must never silently allow a call.
            return ApprovalDecision(False, reason="approval_error")

    def _tool_block(self, snapshot: Any) -> str:
        """Prompt tool list: module tools first, then the always-available core tools."""
        block = snapshot.render_block()
        if self.builtins:
            builtin_block = self.builtins.render_block()
            block = f"{block}\n{builtin_block}" if block else builtin_block
        return block

    async def _call_builtin(self, turn: Turn, builtin: Any, args: Mapping[str, Any]) -> Any:
        """Run a core-owned tool and normalize the outcome to `ToolCallResult`.

        The handler is a loop-package object (`builtins.py`); a `VadeError`
        from it — `INVALID_ARGUMENT` on a malformed question, §4.4 item 4 —
        becomes an error tool result and the turn CONTINUES; any other
        exception is `INTERNAL` for the same reason. `CancelledError` always
        propagates: an interrupted turn must die, and the manager has already
        claimed + emitted `turn.input_resolved` on its way out (S5).

        `ctx.emit` is the RAW session writer on purpose: the manager's
        interrupt-time resolution must land BEFORE `turn.interrupted`, and the
        guarded `_turn_emit` would swallow exactly that write (the turn is
        already flagged when the cancellation unwinds through the wait).
        """
        self._raise_if_interrupted(turn)
        started = time.perf_counter()
        ctx = BuiltinContext(
            session_id=turn.session_id, turn_id=turn.id,
            emit=lambda event_type, payload: self._emit(turn.session_id, event_type, payload),
        )
        try:
            result = await builtin.handler(dict(args), ctx)
        except asyncio.CancelledError:
            raise
        except VadeError as exc:
            result = ToolCallResult(False, builtin.tool_id, code=exc.code, message=exc.message)
        except Exception as exc:  # noqa: BLE001 - a builtin must not fail the turn
            result = ToolCallResult(False, builtin.tool_id, code="INTERNAL",
                                    message=f"built-in tool {builtin.llm_name} failed: {exc}")
        duration_ms = int((time.perf_counter() - started) * 1000)
        if not getattr(result, "duration_ms", 0):
            result = ToolCallResult(
                ok=result.ok, tool_id=result.tool_id, result=result.result,
                code=result.code, message=result.message, retriable=result.retriable,
                duration_ms=max(duration_ms, 1),
            )
        return result

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


def _approval_attribution(turn: Turn) -> dict[str, Any]:
    """RFC-0007 §7.1 attribution for an approval this turn raises.

    `depth == 0` / `task_id is None` is a main turn, which is the only kind
    `LoopEngine` runs today (subagents are not wired through this module), but
    the fields are read off the turn rather than hard-coded so a spawned turn
    carrying the triple reaches both the prompt and the resolution.

    One helper for both directions on purpose: `_check_approval` puts the
    triple on the request and the deny path publishes the same triple on
    `turn.approval_resolved`, which is what lets a device that missed the
    prompt close the right card. Keeping two copies is how the deny payload
    ended up with only the six required fields.
    """
    return {
        "task_id": getattr(turn, "task_id", None),
        "task_name": getattr(turn, "task_name", None),
        "depth": int(getattr(turn, "depth", 0) or 0),
    }


def _runtime_facts(model_id: str | None) -> str:
    """RFC-0003 §4.4: per-turn facts, written ONLY into the context section.

    The hard rule is one-directional: facts that may change per turn are banned
    from `stable`, they do not have to exist. There is deliberately no
    wall-clock line here — a timestamp would break the turn-to-turn context
    stability that A2 pins, and nothing in v1 consumes it.
    """
    lines = ["Runtime facts (may change between turns):"]
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    if cwd:
        lines.append(f"- cwd: {cwd}")
    lines.append(f"- model: {model_id or 'unset'}")
    try:
        device = socket.gethostname()
    except OSError:
        device = ""
    if device:
        lines.append(f"- device: {device}")
    return "\n".join(lines)


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
