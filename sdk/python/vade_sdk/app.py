"""ModuleApp: the one object a module author builds.

Responsibilities: hold the tool table, generate the manifest (the SAME function
feeds `--print-manifest`, `--check` and `module/register`), dispatch calls, and
let `run()` pick standalone vs core from the CLI.

Nothing here imports the transport at module import time: `--standalone` must
work on a machine where `websockets` and the Noise binding are not installed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import copy
import functools
import inspect
import json
import os
import re
import signal
import sys
import traceback
from typing import Any, Callable, Iterable, Mapping, Sequence

from .errors import ToolError
from .logging import JsonLogger
from .spec import MAX_TOOLS, RISKS, TIMEOUT_MS_MAX, TIMEOUT_MS_MIN, ToolSpec, validate_args

__all__ = ["ModuleApp", "run", "PROTOCOL", "DEFAULT_MAX_PAYLOAD"]

PROTOCOL = "vade/v1"
DEFAULT_MAX_PAYLOAD = 65519  # C-01 §7.5

MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_-]*)+$")
INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
CONCURRENCY = ("exclusive", "pooled", "mirrored")
ROLES = ("tool", "evolve", "ui")
CAPABILITY_TYPES = ("tools",)
#: C-03 `$defs.page` id pattern (same string core's `uipage.PAGE_ID_RE` uses).
PAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: The four keys C-03 marks `required` in `$defs.page`. Nothing else is checked
#: here — see `_check_ui_pages` for why the rest belongs to core.
PAGE_REQUIRED_KEYS = ("id", "title", "version", "schema")

_RESULT_TYPES = (dict, list, str, int, float, bool, type(None))

#: Per-call core context (`params["ctx"]`). Modules can read it without the SDK
#: injecting core-specific tool arguments, and sync tools keep it because the
#: executor runnable copies the context before entering the worker thread.
_CALL_CONTEXT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "vade_call_context", default={}
)


def current_call_context() -> Mapping[str, Any]:
    """The context core attached to the current tool call, or `{}`."""
    return _CALL_CONTEXT.get()


class ModuleApp:
    """A collection of tools plus the lifecycle needed to serve them.

    Frozen keyword surface (0.1.x): module_id, version, concurrency,
    display_name, priority, capabilities. The rest are optional and additive:
    instance_id, role, max_concurrency, max_payload, protocol, logger,
    env_prefix, ui_pages.

    `ui_pages` declares C-19 pages (RFC-0009 §9.2) — the manifest entry that
    makes a module visible in `ui/modules` at all. The SDK checks only the
    shape every page must have to be addressable: the four keys C-03 marks
    `required` (`id`, `title`, `version`, `schema`) plus `id` matching
    ``^[a-z][a-z0-9_-]{0,31}$``. A typo therefore fails in your own
    constructor, loudly and naming the page, instead of in core's log.

    Everything else is deliberately NOT re-implemented here: the `icon` enum,
    the closed node tree / `uiProps` set, `x-` escape hatches, the page count
    and cross-page `navigate` targets are judged by core's `validate_ui()`,
    which *drops* a bad page into `Manifest.ui_warnings` and registers the
    module's tools anyway (RFC-0009 §7.1). A page this constructor accepts can
    still be dropped by core; that asymmetry is the point — one owner per rule.
    """

    def __init__(
        self,
        module_id: str,
        version: str,
        concurrency: str = "exclusive",
        *,
        display_name: str | None = None,
        priority: int = 0,
        capabilities: Sequence[str] = ("tools",),
        instance_id: str | None = None,
        role: str = "tool",
        max_concurrency: int = 4,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        protocol: str = PROTOCOL,
        logger: JsonLogger | None = None,
        env_prefix: str | None = None,
        ui_pages: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if MODULE_ID_RE.match(module_id) is None:
            raise ValueError(
                f"module_id {module_id!r} is not reverse-DNS lowercase (^[a-z][a-z0-9]*(\\.[a-z][a-z0-9_-]*)+$)"
            )
        if VERSION_RE.match(version) is None:
            raise ValueError(f"version {version!r} is not SemVer (e.g. 1.0.0)")
        if concurrency not in CONCURRENCY:
            raise ValueError(f"concurrency must be one of {list(CONCURRENCY)}, got {concurrency!r}")
        if role not in ROLES:
            raise ValueError(f"role must be one of {list(ROLES)}, got {role!r}")
        if not (0 <= int(priority) <= 100):
            raise ValueError(f"priority must be within [0, 100], got {priority}")
        if not (1 <= int(max_concurrency) <= 64):
            raise ValueError(f"max_concurrency must be within [1, 64], got {max_concurrency}")
        if not (1024 <= int(max_payload) <= DEFAULT_MAX_PAYLOAD):
            raise ValueError(f"max_payload must be within [1024, {DEFAULT_MAX_PAYLOAD}], got {max_payload}")
        unknown = [c for c in capabilities if c not in CAPABILITY_TYPES]
        if unknown:
            # Fail at startup: a module that claims a capability the SDK cannot
            # build would register a manifest that promises prompts/events it
            # can never answer for.
            raise ValueError(
                f"unsupported capability types {unknown}; vade-sdk 0.1 can only declare ['tools']"
            )
        checked_ui_pages = _check_ui_pages(ui_pages)

        self.module_id = module_id
        self.version = version
        self.concurrency = concurrency
        self.display_name = display_name
        self.priority = int(priority)
        self.role = role
        self.max_concurrency = int(max_concurrency)
        self.max_payload = int(max_payload)
        self.protocol = protocol
        self.instance_id = instance_id or os.environ.get("VADE_INSTANCE_ID") or "main"
        if INSTANCE_ID_RE.match(self.instance_id) is None:
            raise ValueError(f"instance_id {self.instance_id!r} violates ^[a-z0-9][a-z0-9-]{{0,31}}$")
        self._env_prefix = (env_prefix or module_id.split(".")[-1]).upper().replace("-", "_")
        self.env_prefix = self._env_prefix
        self.log = logger or JsonLogger(module_id, self.instance_id)
        self._tools: dict[str, ToolSpec] = {}
        #: C-19 pages, already shape-checked and deep-copied: a caller that
        #: mutates its own dict after construction must not rewrite the manifest.
        self._ui_pages: tuple[dict[str, Any], ...] = checked_ui_pages
        self._transport: Any = None  # set by transport.serve()
        self._draining = False
        self._inflight = 0
        self._cancelled: set[str] = set()
        self._tasks: dict[str, asyncio.Future[Any]] = {}
        self._semaphore: asyncio.Semaphore | None = None
        self._exit_code = 0
        self._stopped: asyncio.Event | None = None
        #: Optional private RPC surface. Core calls these by method name; they
        #: are not tools and never appear in the manifest. A module author must
        #: register only handlers it owns (for example `memory/admin_list`).
        self._rpc_handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {}
        #: Optional event subscriber callback for `events/deliver`. The default
        #: behavior remains an acknowledgement; a module that declares
        #: subscriptions can opt in without changing the transport.
        self._event_handler: Callable[[str, Any], Any] | None = None

    # ---------------------------------------------------------- private RPC
    def on_rpc(self, method: str, handler: Callable[[Mapping[str, Any]], Any]) -> "ModuleApp":
        """Register a non-tool RPC handler (for example `memory/read_session`).

        The handler receives the params object and may be sync or async. It is
        deliberately separate from `add_tool`: private RPCs are never exposed
        to the model and must not appear in the capability table.
        """
        if not isinstance(method, str) or not method.strip():
            raise ValueError("RPC method must be a non-empty string")
        if not callable(handler):
            raise TypeError(f"handler for {method!r} must be callable")
        self._rpc_handlers[method] = handler
        return self

    async def dispatch_rpc(self, method: str, params: Any = None) -> Any:
        """Dispatch one private RPC or raise `ToolError(NOT_FOUND)`."""
        handler = self._rpc_handlers.get(method)
        if handler is None:
            raise ToolError("NOT_FOUND", f"module {self.module_id!r} has no private RPC {method!r}")
        payload = params if isinstance(params, Mapping) else {}
        result = handler(payload)
        if inspect.isawaitable(result):
            result = await result
        return result

    def on_event(self, handler: Callable[[str, Any], Any]) -> "ModuleApp":
        """Register an `events/deliver` handler; may be sync or async."""
        if not callable(handler):
            raise TypeError("event handler must be callable")
        self._event_handler = handler
        return self

    async def dispatch_event(self, topic: str, payload: Any = None) -> Any:
        if self._event_handler is None:
            return {"ok": True}
        result = self._event_handler(topic, payload)
        if inspect.isawaitable(result):
            result = await result
        return result if result is not None else {"ok": True}

    # ------------------------------------------------------------------ tools
    def add_tool(self, spec: ToolSpec) -> "ModuleApp":
        """Add one tool. Only valid before registration — use replace_tools later."""
        if not isinstance(spec, ToolSpec):
            raise TypeError(f"add_tool expects a ToolSpec, got {type(spec).__name__}")
        if self._transport is not None:
            raise RuntimeError("add_tool() cannot change a live capability set; use `await app.replace_tools([...])`")
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name {spec.name!r}")
        if len(self._tools) >= MAX_TOOLS:
            raise ValueError(f"a module may declare at most {MAX_TOOLS} tools")
        self._tools[spec.name] = spec
        return self

    def add_tools(self, specs: Iterable[ToolSpec]) -> "ModuleApp":
        for spec in specs:
            self.add_tool(spec)
        return self

    @property
    def tools(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    async def replace_tools(self, specs: Iterable[ToolSpec]) -> dict[str, Any]:
        """Atomically replace the capability set (`module/capabilities.update`).

        The local table is swapped ONLY after core accepts. A rejection raises
        `ToolError("MANIFEST_INVALID")` and the old table stays routable — the
        SDK must never make the module look healthier than core thinks it is.
        """
        new_tools = self._build_tools(specs)
        manifest = self._manifest_with(new_tools)
        if self._transport is None:
            self._tools = new_tools
            return {"accepted": True, "cap_epoch": 0, "tool_names": list(new_tools)}
        try:
            result = await self._transport.request("module/capabilities.update", manifest, timeout=15.0)
        except Exception as exc:  # VadeError from the RPC layer
            code = getattr(exc, "code", "UNAVAILABLE")
            raise ToolError("MANIFEST_INVALID", f"core rejected capabilities.update ({code}): {exc}") from None
        if not isinstance(result, Mapping) or not result.get("accepted"):
            raise ToolError("MANIFEST_INVALID", f"core rejected capabilities.update: {result!r}")
        self._tools = new_tools
        self.log.info("capabilities updated", code=None, cap_epoch=result.get("cap_epoch"), tools=len(new_tools))
        return dict(result)

    def _build_tools(self, specs: Iterable[ToolSpec]) -> dict[str, ToolSpec]:
        tools: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec):
                raise TypeError(f"replace_tools expects ToolSpec objects, got {type(spec).__name__}")
            if spec.name in tools:
                raise ValueError(f"duplicate tool name {spec.name!r}")
            tools[spec.name] = spec
        if len(tools) > MAX_TOOLS:
            raise ValueError(f"a module may declare at most {MAX_TOOLS} tools")
        return tools

    # ------------------------------------------------------------------ manifest
    def manifest(self) -> dict[str, Any]:
        """The registration payload. One generator for print/check/register."""
        return self._manifest_with(self._tools)

    def _manifest_with(self, tools: Mapping[str, ToolSpec]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "module_id": self.module_id,
            "instance_id": self.instance_id,
            "version": self.version,
            "protocol": self.protocol,
            "role": self.role,
            "concurrency": self.concurrency,
            "priority": self.priority,
            "capabilities": {"tools": [spec.manifest_entry() for spec in tools.values()]},
            "limits": {"max_concurrency": self.max_concurrency, "max_payload": self.max_payload},
        }
        if self.display_name:
            payload["display_name"] = self.display_name
        if self._ui_pages:
            # Only when non-empty. FR-8.79 §8.2: a module without a `ui` block
            # has no entry ANYWHERE — no empty tab, no greyed row — so an empty
            # declaration must leave the payload byte-for-byte as it was before
            # `ui_pages` existed (hence: no `"ui": {"pages": []}`).
            payload["ui"] = {"pages": [copy.deepcopy(page) for page in self._ui_pages]}
        return payload

    def manifest_json(self) -> str:
        return json.dumps(self.manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    # ------------------------------------------------------------------ checks
    def check(self) -> list[str]:
        """Local validation for `--check` (exit code 2 on any problem)."""
        problems: list[str] = []
        try:
            manifest = self.manifest()
        except Exception as exc:
            return [f"manifest: {exc}"]
        if not manifest["capabilities"]["tools"]:
            problems.append("capabilities.tools is empty: a module with no tools cannot be routed to")
        if len(manifest["capabilities"]["tools"]) > MAX_TOOLS:
            problems.append(f"{len(manifest['capabilities']['tools'])} tools > {MAX_TOOLS}")
        seen: set[str] = set()
        for index, entry in enumerate(manifest["capabilities"]["tools"]):
            pointer = f"/capabilities/tools/{index}"
            name = entry["name"]
            if name in seen:
                problems.append(f"{pointer}/name: duplicate tool name {name!r}")
            seen.add(name)
            if not entry["description"].strip():
                problems.append(f"{pointer}/description: must not be blank")
            if entry["risk"] not in RISKS:
                problems.append(f"{pointer}/risk: {entry['risk']!r} not in {list(RISKS)}")
            if not (TIMEOUT_MS_MIN <= int(entry["timeout_ms"]) <= TIMEOUT_MS_MAX):
                problems.append(f"{pointer}/timeout_ms: {entry['timeout_ms']} outside [100, 600000]")
            schema = entry["input_schema"]
            if schema.get("type") != "object":
                problems.append(f"{pointer}/input_schema/type: must be 'object'")
            props = schema.get("properties", {})
            for required in schema.get("required", []):
                if required not in props:
                    problems.append(f"{pointer}/input_schema/required: {required!r} is not declared in properties")
        if manifest["limits"]["max_payload"] > DEFAULT_MAX_PAYLOAD:
            problems.append(f"/limits/max_payload: {manifest['limits']['max_payload']} exceeds {DEFAULT_MAX_PAYLOAD}")
        return problems

    # ------------------------------------------------------------------ dispatch
    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any] | None,
        *,
        call_id: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one tool and return the C-05 tool_result envelope.

        `context` is core's out-of-band `params["ctx"]`. It is exposed through
        `current_call_context()` rather than injected into the tool's JSON
        arguments, preserving each tool's declared schema.

        Never raises: every failure becomes `{"ok": false, "error": {...}}` so a
        buggy tool cannot take the connection down.
        """
        started = asyncio.get_running_loop().time()
        spec = self._tools.get(name)
        if spec is None:
            return _error_envelope("NOT_FOUND", f"unknown tool {name!r}", started)
        if self._draining:
            return _error_envelope("MODULE_DRAINING", "module is draining", started)
        payload = dict(args or {})
        problems = validate_args(spec.input_schema, payload)
        if problems:
            return _error_envelope("INVALID_ARGUMENT", "; ".join(problems), started)

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._inflight += 1
        token = _CALL_CONTEXT.set(dict(context or {}))
        try:
            async with self._semaphore:
                result = await self._execute(spec, payload, call_id)
        except ToolError as exc:
            self.log.info("tool failed", tool=name, duration_ms=_ms(started), code=exc.code)
            return {"ok": False, "error": exc.to_dict(), "duration_ms": _ms(started)}
        except asyncio.CancelledError:
            self.log.info("tool cancelled", tool=name, duration_ms=_ms(started), code="CANCELLED")
            return _error_envelope("CANCELLED", "call cancelled", started)
        except Exception as exc:  # a tool bug is INTERNAL; traceback stays in the log
            self.log.error(
                "tool crashed",
                tool=name,
                duration_ms=_ms(started),
                code="INTERNAL",
                traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:],
            )
            return _error_envelope("INTERNAL", f"{type(exc).__name__} in tool {name!r}", started)
        finally:
            _CALL_CONTEXT.reset(token)
            self._inflight -= 1
            self._tasks.pop(call_id, None)
            self._cancelled.discard(call_id)

        self.log.info("tool ok", tool=name, duration_ms=_ms(started))
        return {"ok": True, "result": result, "truncated": False, "duration_ms": _ms(started)}

    async def _execute(self, spec: ToolSpec, args: Mapping[str, Any], call_id: str) -> Any:
        loop = asyncio.get_running_loop()
        if spec.is_async:
            task: asyncio.Future[Any] = asyncio.ensure_future(spec.fn(**args))
        else:
            # A sync tool runs in a worker thread: one blocking tool must not
            # stall heartbeats or every other in-flight call. Copy the context
            # so `current_call_context()` survives the thread hop.
            ctx = contextvars.copy_context()
            task = loop.run_in_executor(None, ctx.run, functools.partial(spec.fn, **args))
        if call_id:
            self._tasks[call_id] = task
        try:
            result = await asyncio.wait_for(task, timeout=spec.timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            raise ToolError("TIMEOUT", f"tool {spec.name!r} exceeded {spec.timeout_ms} ms") from None
        except asyncio.CancelledError:
            raise
        if call_id in self._cancelled:
            raise ToolError("CANCELLED", f"call {call_id} was cancelled before it returned")
        _check_result(spec.name, result)
        return result

    def cancel(self, call_id: str) -> bool:
        """`tools/cancel`: async tools are really cancelled, sync ones are marked."""
        self._cancelled.add(call_id)
        task = self._tasks.get(call_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    def begin_drain(self, grace_ms: int) -> None:
        self._draining = True
        self.log.info("draining", code="MODULE_DRAINING", grace_ms=grace_ms, inflight=self._inflight)

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def inflight(self) -> int:
        return self._inflight

    def load(self) -> float:
        if self.max_concurrency <= 0:
            return 0.0
        return min(1.0, self._inflight / self.max_concurrency)

    def request_stop(self, code: int = 0) -> None:
        self._exit_code = code
        if self._stopped is not None:
            self._stopped.set()

    # ------------------------------------------------------------------ run
    def run(self, argv: Sequence[str] | None = None) -> int:
        """CLI entry point: `--standalone` OR `--core`, never both."""
        parser = _build_parser(self)
        args = parser.parse_args(list(argv) if argv is not None else None)

        if args.print_manifest:
            sys.stdout.write(self.manifest_json())
            return 0
        if args.check:
            problems = self.check()
            for problem in problems:
                sys.stderr.write(f"{problem}\n")
            if problems:
                return 2
            sys.stdout.write(self.manifest_json())
            return 0
        if args.instance_id:
            if INSTANCE_ID_RE.match(args.instance_id) is None:
                sys.stderr.write("instance id violates ^[a-z0-9][a-z0-9-]{0,31}$\n")
                return 2
            self.instance_id = args.instance_id
            self.log.instance_id = args.instance_id

        if args.standalone:
            if args.core:
                sys.stderr.write("--standalone and --core are mutually exclusive\n")
                return 2
            from .standalone import run_standalone  # lazy: no transport import

            return run_standalone(self, args.standalone, args.args)

        core_url = args.core or os.environ.get("VADE_CORE_URL")
        pair_code = args.pair_code or os.environ.get("VADE_PAIR_CODE")
        if not core_url:
            parser.print_usage(sys.stderr)
            sys.stderr.write("one of --standalone / --core (or VADE_CORE_URL) is required\n")
            return 2
        if args.standalone:
            sys.stderr.write("--standalone and --core are mutually exclusive\n")
            return 2
        if self._draining:
            sys.stderr.write("cannot connect while draining\n")
            return 2
        from .transport import serve  # lazy: only the core path needs websockets + Noise

        try:
            return asyncio.run(serve(self, core_url, pair_code=pair_code))
        except KeyboardInterrupt:
            return 0


def _ms(started: float) -> int:
    try:
        return max(0, int((asyncio.get_running_loop().time() - started) * 1000))
    except RuntimeError:  # pragma: no cover - outside a loop
        return 0


def _error_envelope(code: str, message: str, started: float) -> dict[str, Any]:
    from .errors import ERROR_CODES

    return {
        "ok": False,
        "error": {"code": code, "message": message[:300], "retriable": ERROR_CODES.get(code, False)},
        "duration_ms": _ms(started),
    }


def _check_result(tool: str, result: Any) -> None:
    """Only JSON-native shapes; `bytes` is rejected on purpose (base64 it)."""
    if isinstance(result, bytes):
        raise ToolError("INTERNAL", f"tool {tool!r} returned bytes; base64-encode it before returning")
    if not isinstance(result, _RESULT_TYPES):
        raise ToolError(
            "INTERNAL",
            f"tool {tool!r} returned {type(result).__name__}; allowed: dict, list, str, int, float, bool, None",
        )
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ToolError("INTERNAL", f"tool {tool!r} returned a non-JSON value: {exc}") from None


def _check_ui_pages(pages: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Minimal C-19 page shape check; core's `validate_ui` stays the judge.

    Only what makes a page addressable is checked: the four keys C-03
    `$defs.page` marks `required`, and the page-id pattern. Everything else —
    the `icon` enum, the closed node tree and `uiProps` set, the 8-page /
    8 KiB budgets, duplicate ids, `navigate` targets — is validated by core,
    which DROPS a bad page into `Manifest.ui_warnings` instead of raising. That
    division is deliberate: a second copy of `manifest.schema.json` here would
    drift, and "the SDK accepted it" is not "core accepted it".

    Failures are loud and name the page. Silent dropping is exactly what this
    check exists to prevent: core drops with a warning the author never reads,
    and the symptom on screen is a module with no entry at all.
    """
    checked: list[dict[str, Any]] = []
    for index, raw in enumerate(pages):
        where = f"ui_pages[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where}: page must be a mapping, got {type(raw).__name__}")
        page = dict(raw)
        page_id = page.get("id")
        if isinstance(page_id, str) and page_id:
            where = f"{where} (page id {page_id!r})"
        missing = [key for key in PAGE_REQUIRED_KEYS if key not in page]
        if missing:
            raise ValueError(
                f"{where}: missing required key(s) {missing}; "
                f"C-03 $defs.page requires {list(PAGE_REQUIRED_KEYS)}"
            )
        if not isinstance(page_id, str) or PAGE_ID_RE.match(page_id) is None:
            raise ValueError(f"{where}: page id {page_id!r} violates ^[a-z][a-z0-9_-]{{0,31}}$")
        checked.append(copy.deepcopy(page))
    return tuple(checked)


def _build_parser(app: ModuleApp) -> argparse.ArgumentParser:
    prog = f"python -m {app.module_id.split('.')[-1]}"
    parser = argparse.ArgumentParser(prog=prog, description=f"{app.module_id} {app.version}")
    parser.add_argument("--print-manifest", action="store_true", help="print the exact registration manifest and exit")
    parser.add_argument("--check", action="store_true", help="validate the manifest and tool signatures (exit 2 on error)")
    parser.add_argument("--standalone", metavar="TOOL", help="run one tool locally; no core, no transport import")
    parser.add_argument("--args", default="{}", help="JSON object passed to --standalone TOOL")
    parser.add_argument("--core", metavar="URL", help="wss://host:8443 (or VADE_CORE_URL)")
    parser.add_argument("--pair-code", metavar="CODE", help="pairing code (see PYTHON_SDK.md §5: currently unsupported)")
    parser.add_argument("--instance-id", metavar="ID", help="instance id (default: VADE_INSTANCE_ID or 'main')")
    parser.add_argument("--version", action="version", version=f"%(prog)s {app.version}")
    return parser


def run(app: ModuleApp, argv: Sequence[str] | None = None) -> int:
    """Convenience alias for `ModuleApp.run` (exported for `if __name__` blocks)."""
    return app.run(argv)
