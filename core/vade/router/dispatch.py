"""Instance selection + tools/call + deadline/error mapping.

The router is the only place that knows `module_id/tool`; the agent loop only
ever sees a snapshot entry and calls `call(tool_id, args)`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from ..errors import ERROR_CODES, VadeError
from .breaker import CircuitBreaker

__all__ = ["Router", "ToolCallResult", "ModuleCaller", "Target", "validate_arguments",
           "MAX_DEADLINE_MS", "MAX_TOOL_CALL_PLAINTEXT"]

MAX_DEADLINE_MS = 600_000
#: C-01: one transport frame carries at most 65519 bytes of plaintext. The
#: tools/call JSON must fit with its JSON-RPC envelope; reserve 256 bytes.
MAX_TOOL_CALL_PLAINTEXT = 65_519 - 256


class ModuleCaller(Protocol):
    """Sends one core->module RPC and awaits the reply."""

    async def call(self, target: "Target", method: str, params: Mapping[str, Any],
                   deadline_ms: int) -> Mapping[str, Any]: ...

    async def cancel(self, target: "Target", call_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Target:
    module_id: str
    instance_id: str
    connection_id: str | None
    tool: str
    injects: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    timeout_ms: int = 30_000
    concurrency: str = "exclusive"
    last_frame_at: float | None = None


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    ok: bool
    tool_id: str
    result: Any = None
    code: str | None = None
    message: str = ""
    retriable: bool = False
    duration_ms: int = 0
    instance_id: str | None = None

    def to_tool_result(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, "result": self.result, "truncated": False, "duration_ms": self.duration_ms}
        return {
            "ok": False,
            "error": {"code": self.code or "INTERNAL", "message": self.message,
                      "retriable": self.retriable},
            "duration_ms": self.duration_ms,
        }


def validate_arguments(schema: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    """Minimal JSON-Schema check for the fields that matter at the boundary.

    Returns an error message or None. Deliberately not a full validator: the
    module still validates its own input; this exists so `additionalProperties:
    false` actually rejects a model-invented `session_id`.
    """
    if not isinstance(args, Mapping):
        return "args must be an object"
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for name in required:
        if name not in args:
            return f"missing required argument {name!r}"
    if schema.get("additionalProperties") is False:
        extra = sorted(set(args) - set(properties))
        if extra:
            return f"unknown argument(s): {', '.join(extra)}"
    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, Mapping):
            continue
        expected = spec.get("type")
        if expected is None:
            continue
        if not _type_ok(expected, value):
            return f"argument {name!r} must be {expected}"
    return None


def _type_ok(expected: Any, value: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_ok(e, value) for e in expected)
    table = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "object": lambda v: isinstance(v, Mapping),
        "array": lambda v: isinstance(v, (list, tuple)),
        "null": lambda v: v is None,
    }
    check = table.get(str(expected))
    return True if check is None else check(value)


def digest_args(args: Mapping[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Router:
    def __init__(
        self,
        caller: ModuleCaller,
        registry: Any,
        *,
        breaker: CircuitBreaker | None = None,
        max_deadline_ms: int = MAX_DEADLINE_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._caller = caller
        self._registry = registry
        self.breaker = breaker or CircuitBreaker()
        self._max_deadline_ms = max_deadline_ms
        self._clock = clock
        self._inflight: dict[tuple[str, str], int] = {}
        # Target carries a dict (input_schema) so it is not hashable: keep a list.
        self._calls: dict[str, list[tuple[Target, str]]] = {}
        self._cancelled_turns: set[str] = set()
        #: `cancel_turn` and the task cancellation in `call()` both fire a
        #: cancel for the same call; the module must see exactly one.
        self._cancel_fired: set[tuple[str, str, str]] = set()

    # ------------------------------------------------------------------ targets
    def _targets(self, tool_id: str) -> list[Target]:
        if "/" not in tool_id:
            return []
        module_id, tool = tool_id.split("/", 1)
        targets: list[Target] = []
        for record in self._registry.snapshot():
            if record.module_id != module_id or not record.enabled or record.manifest is None:
                continue
            spec = record.manifest.tool(tool)
            if spec is None or record.connection_id is None:
                continue
            health = getattr(record.health, "value", str(record.health))
            if health in ("offline", "rejected"):
                continue
            targets.append(Target(
                module_id=module_id, instance_id=record.instance_id,
                connection_id=record.connection_id, tool=tool,
                injects=tuple(record.manifest.injects),
                input_schema=spec.input_schema, timeout_ms=spec.timeout_ms,
                concurrency=record.manifest.concurrency, last_frame_at=record.last_frame_at,
            ))
        # `pooled` prefers least in-flight and, on ties, the freshest heartbeat.
        targets.sort(key=lambda t: (self._inflight.get((t.module_id, t.instance_id), 0),
                                    -(t.last_frame_at or 0.0), t.instance_id))
        return targets

    def _pick(self, tool_id: str) -> list[Target]:
        targets = self._targets(tool_id)
        if not targets:
            raise VadeError("UNAVAILABLE", f"no online instance for {tool_id!r}")
        concurrency = targets[0].concurrency
        if concurrency == "mirrored":
            raise VadeError("UNAVAILABLE", f"{tool_id!r} belongs to a mirrored module and is not routable")
        if concurrency == "pooled":
            healthy = [t for t in targets if t.connection_id and self.breaker.allow(t.module_id, t.instance_id)]
            if not healthy:
                raise VadeError("UNAVAILABLE", f"all instances of {tool_id!r} are circuit-open")
            return healthy
        target = targets[0]
        if not self.breaker.allow(target.module_id, target.instance_id):
            raise VadeError("UNAVAILABLE", f"{tool_id!r} is circuit-open")
        return [target]

    # ------------------------------------------------------------------ call
    async def call(
        self,
        tool_id: str,
        args: Mapping[str, Any] | None,
        *,
        session_id: str,
        turn_id: str | None = None,
        call_id: str | None = None,
        deadline_ms: int | None = None,
    ) -> ToolCallResult:
        started = self._clock()
        call_id = call_id or f"call-{int(started * 1000)}-{id(args) & 0xFFFF}"
        try:
            target = self._pick(tool_id)[0]
        except VadeError as exc:
            return ToolCallResult(False, tool_id, code=exc.code, message=exc.message,
                                  retriable=exc.retriable, duration_ms=0)

        payload_args, error = self._prepare_args(target, args or {}, session_id)
        if error is not None:
            return ToolCallResult(False, tool_id, code="INVALID_ARGUMENT", message=error,
                                  duration_ms=int((self._clock() - started) * 1000))

        params: dict[str, Any] = {
            "call_id": call_id,
            "tool": target.tool,
            "args": payload_args,
            "deadline_ms": self._effective_deadline(target, deadline_ms),
        }
        if len(json.dumps(params, ensure_ascii=False).encode("utf-8")) > MAX_TOOL_CALL_PLAINTEXT:
            # No v1 fragmentation: refuse before the frame is built instead of
            # letting the transport fail with a confusing PROTOCOL_MISMATCH.
            return ToolCallResult(False, tool_id, code="PAYLOAD_TOO_LARGE",
                                  message="tool arguments exceed the 65519-byte frame limit",
                                  duration_ms=int((self._clock() - started) * 1000))
        if "session_id" in target.injects:
            # Core-filled; a model-supplied value was overwritten above.
            params["ctx"] = {"session_id": session_id}

        key = (target.module_id, target.instance_id)
        self._inflight[key] = self._inflight.get(key, 0) + 1
        if turn_id:
            self._calls.setdefault(turn_id, []).append((target, call_id))
        timeout_s = params["deadline_ms"] / 1000.0
        try:
            reply = await asyncio.wait_for(
                self._caller.call(target, "tools/call", params, params["deadline_ms"]),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            self._fire_cancel(target, call_id)
            self.breaker.record_failure(target.module_id, target.instance_id, timeout=True)
            return ToolCallResult(False, tool_id, code="TIMEOUT",
                                  message=f"{target.tool} timed out after {params['deadline_ms']} ms",
                                  retriable=True, duration_ms=int((self._clock() - started) * 1000),
                                  instance_id=target.instance_id)
        except asyncio.CancelledError:
            self._fire_cancel(target, call_id)
            raise
        except VadeError as exc:
            self.breaker.record_failure(target.module_id, target.instance_id)
            return ToolCallResult(False, tool_id, code=exc.code, message=exc.message,
                                  retriable=exc.retriable,
                                  duration_ms=int((self._clock() - started) * 1000),
                                  instance_id=target.instance_id)
        except Exception:
            self.breaker.record_failure(target.module_id, target.instance_id)
            return ToolCallResult(False, tool_id, code="INTERNAL", message="module call failed",
                                  duration_ms=int((self._clock() - started) * 1000),
                                  instance_id=target.instance_id)
        finally:
            self._inflight[key] = max(0, self._inflight.get(key, 1) - 1)
            if turn_id:
                pending = self._calls.get(turn_id, [])
                if (target, call_id) in pending:
                    pending.remove((target, call_id))

        duration_ms = int((self._clock() - started) * 1000)
        if not isinstance(reply, Mapping):
            self.breaker.record_failure(target.module_id, target.instance_id)
            return ToolCallResult(False, tool_id, code="INTERNAL", message="malformed tool result",
                                  duration_ms=duration_ms, instance_id=target.instance_id)
        if reply.get("ok"):
            self.breaker.record_success(target.module_id, target.instance_id)
            return ToolCallResult(True, tool_id, result=reply.get("result"),
                                  duration_ms=int(reply.get("duration_ms", duration_ms)),
                                  instance_id=target.instance_id)
        error = reply.get("error") or {}
        code = str(error.get("code", "INTERNAL"))
        if code not in ERROR_CODES:
            # A module may only return C-04 codes; anything else is a protocol bug.
            code, message = "INTERNAL", f"module returned unknown error code {code!r}"
        else:
            message = str(error.get("message", ""))[:300]
        self.breaker.record_failure(target.module_id, target.instance_id)
        return ToolCallResult(False, tool_id, code=code, message=message,
                              retriable=bool(error.get("retriable", False)),
                              duration_ms=duration_ms, instance_id=target.instance_id)

    def _prepare_args(
        self, target: Target, args: Mapping[str, Any], session_id: str
    ) -> tuple[dict[str, Any], str | None]:
        prepared = dict(args)
        if "session_id" in target.injects and "session_id" in prepared:
            # The model cannot forge the session: overwrite whatever it sent.
            # (If it did not send one we must NOT add it: the module reads ctx,
            # and an invented extra arg would fail additionalProperties.)
            prepared["session_id"] = session_id
        error = validate_arguments(target.input_schema, prepared)
        return prepared, error

    def _effective_deadline(self, target: Target, requested: int | None) -> int:
        deadline = target.timeout_ms
        if requested is not None:
            deadline = min(deadline, int(requested))
        return max(100, min(deadline, self._max_deadline_ms))

    # ------------------------------------------------------------------ cancel
    def cancel_turn(self, turn_id: str) -> None:
        """Sync, fire-and-forget: queue interrupt must not await modules."""
        self._cancelled_turns.add(turn_id)
        for target, call_id in list(self._calls.get(turn_id, ())):
            self._fire_cancel(target, call_id)
        self._calls.pop(turn_id, None)

    def _fire_cancel(self, target: Target, call_id: str) -> None:
        key = (target.module_id, target.instance_id, call_id)
        if key in self._cancel_fired:
            return  # cancel_turn already told the module; a second frame is noise
        self._cancel_fired.add(key)
        try:
            task = asyncio.ensure_future(self._caller.cancel(target, call_id))
        except RuntimeError:  # pragma: no cover - no running loop
            return
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def was_cancelled(self, turn_id: str) -> bool:
        return turn_id in self._cancelled_turns

    # ------------------------------------------------------------------ state
    def inflight(self, module_id: str, instance_id: str) -> int:
        return self._inflight.get((module_id, instance_id), 0)

    def breaker_state(self, module_id: str, instance_id: str) -> str:
        return self.breaker.state(module_id, instance_id)

    def degraded(self) -> tuple[tuple[str, str, str], ...]:
        return self.breaker.degraded()
