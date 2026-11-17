"""C3 component entry point: session store, queue, router, loop, UI RPC.

`install(app)` is called by C2's component scan. It must be safe on a bare App
(no registry, no LLM): the core then starts and answers `ui/submit` with a
readable "no capability" turn instead of crashing (FR-1.4).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from ..errors import VadeError
from ..logging import get_logger

from .manager import SessionLog
from .queue import TurnQueue
from .rpc import SessionRPC
from .sqlite_store import SQLiteStore

__all__ = ["install", "SessionLog", "SQLiteStore", "SessionRPC", "TurnQueue"]

logger = get_logger("vade.session")


class _LazyApproval:
    """Resolves `app.approval_port` at call time (C5a installs after C3)."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _port(self) -> Any:
        port = getattr(self._app, "approval_port", None)
        if port is None:
            from ..loop.ports import NullApprovalPort

            return NullApprovalPort()
        return port

    async def check(self, request: Any) -> Any:
        return await self._port().check(request)

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        """RFC-0007 §5.6: `snapshot.pending_approvals[]` carries `task_id`.

        The port's public list is `pending_requests()` (all sessions, for the
        admin surface); the snapshot wants this session's rows. Without this
        delegation `hasattr(port, "pending")` is false and the snapshot is
        always empty, so the attribution never reaches a reconnecting client.
        """
        port = getattr(self._app, "approval_port", None)
        getter = getattr(port, "pending_requests", None)
        if not callable(getter):
            return []
        try:
            return [row for row in getter() if row.get("session_id") == session_id]
        except Exception:  # pragma: no cover - approval port is C5a's
            return []

    async def close_turn(
        self, session_id: str, turn_id: str, *, reason: str = "turn_interrupted",
    ) -> list[dict[str, Any]] | None:
        """Close + dequeue every approval left open on a turn that is ending.

        Returns the rows to announce, `[]` when there was nothing left, and
        `None` when the installed port has no turn-end closer at all — the
        caller then falls back to `pending()` (a fake/older port in tests).
        Closing the row and announcing it must be one step: `pending()` alone
        left the table advertising a request nobody could answer.
        """
        port = getattr(self._app, "approval_port", None)
        closer = getattr(port, "close_turn", None)
        if not callable(closer):
            return None
        try:
            result: Any = closer(session_id, turn_id, reason=reason)
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # pragma: no cover - approval port is C5a's
            return None
        return [dict(row) for row in (result or ()) if isinstance(row, dict)]

    async def on_session_end(self, session_id: str) -> None:
        port = getattr(self._app, "approval_port", None)
        if port is not None and hasattr(port, "on_session_end"):
            await port.on_session_end(session_id)


class _LazyMemory:
    def __init__(self, app: Any) -> None:
        self._app = app

    def _port(self) -> Any:
        return getattr(self._app, "memory_port", None)

    async def read_session(self, session_id: str, limit: int = 50) -> list[Any]:
        port = self._port()
        if port is None:
            return []
        return await port.read_session(session_id, limit)

    def __getattr__(self, name: str) -> Any:
        port = self._port()
        if port is None:
            async def _missing(*_a: Any, **_k: Any) -> Any:
                raise VadeError("NOT_FOUND", "memory module is not installed")

            return _missing
        return getattr(port, name)


class _LazyBlobs:
    """Resolves `app.blob` at submit time (RFC-0004 §6.4 step 4).

    Component install order is alphabetical (`blob` < `session`), but relying on
    that would break the moment a facade is rebuilt or the upload channel is
    disabled: the session component must install either way. `None` is a real
    answer — image/file parts are then refused with UNSUPPORTED, not silently
    accepted with an unverifiable id.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    def __call__(self) -> Any:
        blob = getattr(self._app, "blob", None)
        if blob is None:
            return None
        return getattr(blob, "service", blob)


def install(app: Any) -> Any:
    from ..loop.builtins import default_builtins
    from ..loop.context import BlobStore, CompactionConfig
    from ..loop.engine import LoopConfig, LoopEngine
    from ..loop.tools import build_snapshot
    from ..router.dispatch import Router
    from ..session.manager import SessionLog
    from ..session.parts import PartLimits
    from ..session.replay import ReplayBuffer
    from ..session.rpc import SessionRPC
    from ..session.sqlite_store import SQLiteStore

    cfg = app.config
    data_dir = Path(str(cfg.get("core.data_dir", "./data")))
    store = SQLiteStore(data_dir / "sessions.db")
    # The queue is memory-only: a turn the previous process left in
    # `queued`/`running`/`waiting_*` can never finish, so every snapshot would
    # advertise a turn that runs forever and the version chain would answer
    # `turn_active` to `regenerate`/`edit` for good. Repair it here, before a
    # single client can attach. `install()` is sync, so this is the store call
    # directly; `SessionLog.reconcile_restarts()` wraps the same method for a
    # caller that already has a loop (and is what a non-SQLite store plugs
    # into). Same one-time-per-process policy, same `reason`.
    try:
        reconciled = store.reconcile_unfinished_turns(reason="restart")
    except Exception as exc:  # pragma: no cover - startup must not fail here
        reconciled = 0
        logger.warning("turn reconcile failed", extra={"code": "INTERNAL", "msg": str(exc)})
    if reconciled:
        logger.warning("turns left running by a previous process", extra={
            "code": None, "msg": f"reconciled={reconciled} state=interrupted reason=restart",
        })
    replay = ReplayBuffer()
    log = SessionLog(
        store,
        replay=replay,
        # RFC-0004: `ui/submit` judges parts against `[attach]` and verifies
        # `file_id`s against the upload channel; both are resolved lazily so the
        # two components can be installed in either order (and disabled).
        blobs=_LazyBlobs(app),
        limits=PartLimits.from_config(cfg),
    )

    router = None
    caller = getattr(app, "module_caller", None)
    if caller is not None and getattr(app, "registry", None) is not None:
        router = Router(caller, app.registry)

    compaction_kwargs: dict[str, Any] = {}
    for key in ("threshold", "target_ratio", "protect_last_n", "protect_first_n",
                "tail_mode", "proactive_prune_tokens", "min_result_chars"):
        value = cfg.get(f"compaction.{key}")
        if value is not None:
            compaction_kwargs[key] = value
    max_result = cfg.get("policy.max_tool_result_tokens")
    if max_result is not None:
        compaction_kwargs["max_tool_result_tokens"] = int(max_result)
    if cfg.get("compaction.enabled") is False:
        compaction_kwargs["threshold"] = 1.0
        compaction_kwargs["target_ratio"] = 0.99
    try:
        compaction = CompactionConfig(**compaction_kwargs)
    except ValueError:
        compaction = CompactionConfig()

    llm = _build_llm(cfg, router=router, registry=getattr(app, "registry", None))
    registry = getattr(app, "registry", None)
    # RFC-0005: the catalog/resolver are read-only views over the registry +
    # `[llm]`; the queue asks the resolver at dequeue (§5.3), never at submit.
    resolver = _build_model_resolver(cfg, registry)
    log.set_model_resolver(resolver)
    log.set_usage_accounting(_build_usage(cfg, store, log, data_dir))
    approval = _LazyApproval(app)
    memory = _LazyMemory(app)

    # RFC-0003 §7: the injection switch is a restart item, read once here.
    # Layered wires the core tools + catalog port; legacy leaves both empty and
    # every loop path stays the pre-RFC one (byte-identical, golden-pinned).
    # `app.skill_loader` may not exist yet (session installs before skill), so
    # both resolve it lazily, at call time.
    injection = "layered" if str(cfg.get("prompt.injection", "legacy") or "").strip().lower() == "layered" else "legacy"
    core_tools = None
    catalog_port = None
    if injection == "layered":
        from ..loop.coretools import build_core_tools
        from ..skill.port import LayeredCatalogPort

        def _skill_loader():
            return getattr(app, "skill_loader", None)

        core_tools = build_core_tools(_skill_loader)
        catalog_port = LayeredCatalogPort(_skill_loader)

    engine = LoopEngine(
        router, llm,
        lambda: build_snapshot(getattr(app, "registry", None)),
        log.emit,
        history=_history_of(log, layered=(injection == "layered")),
        approval=approval,
        memory=memory,
        # RFC-0008 §14: `ask_user` rides the input component's port; the
        # provider is resolved per call, so install order (input < session)
        # and a disabled input component both degrade to an immediate
        # `UNAVAILABLE` tool result, never a hang.
        builtins=default_builtins(lambda: getattr(app, "input_port", None)),
        prompt_hooks=lambda: getattr(app, "prompt_hooks", []),
        audit=getattr(app, "audit", None),
        config=LoopConfig(max_steps=int(cfg.get("policy.max_steps", 8)),
                          approval_timeout_s=int(cfg.get("policy.approval_timeout_s", 60)),
                          injection=injection,
                          hot_tools=tuple(cfg.get("tools.hot") or ()),
                          hot_max=int(cfg.get("tools.hot_max", 12))),
        compaction=compaction,
        blob_store=BlobStore(data_dir / "blobs"),
        core_tools=core_tools,
        catalog=catalog_port,
    )
    log.set_executor(engine.run_turn)
    log.set_cancel_hook(lambda turn_id: router.cancel_turn(turn_id) if router is not None else None)
    log.set_cancel_clear_hook(
        lambda turn_id: router.clear_cancelled(turn_id) if router is not None else None
    )
    log.approval_port = approval
    log.input_port = getattr(app, "input_port", None)
    # `app.on_session_end` is the slot every component appends to (C3d's
    # `session.closed` beat, C5a's grant cleanup). Without this line the list
    # was never traversed, so `SessionLog.close()` published nothing. It is
    # kept by REFERENCE on purpose: `skill`/`subagents` install after `session`
    # (alphabetical scan) and may append their own hooks later.
    hooks = getattr(app, "on_session_end", None)
    if hooks is None:
        hooks = []
        app.on_session_end = hooks
    log.set_session_end_hooks(hooks)

    rpc = SessionRPC(log, approval=lambda: getattr(app, "approval_port", None),
                     input_port=lambda: getattr(app, "input_port", None))
    # `module.status` rides the same seq bus; the registry (C2) is the trigger.
    from .status import ModuleStatusPublisher

    status = ModuleStatusPublisher(log)
    if getattr(app, "registry", None) is not None:
        status.attach(app.registry)
    app.module_status = status
    app.session_store = store
    app.session = log
    app.session_log = log
    app.router = router
    app.loop = engine
    app.llm = llm
    app.models = resolver
    app.usage = log.usage
    app.session_rpc = rpc
    app.session_handlers = {
        name: (lambda conn, params, _name=name: rpc.dispatch(conn, _name, params))
        for name in rpc.handlers()
    }
    # A component that installs *before* this one cannot write into the dict
    # above (install_components is alphabetical: events < session). It publishes
    # a provider instead and this is the single place that drains them, so
    # `events/subscribe` and `skill/proposal` are reachable on a real socket.
    for provider in tuple(getattr(app, "handler_providers", ()) or ()):
        for name, handler in (provider() or {}).items():
            app.session_handlers.setdefault(name, handler)
    return app


def _build_model_resolver(cfg: Any, registry: Any) -> Any:
    from ..llm.catalog import ModelResolver

    return ModelResolver(cfg, registry)


def _build_usage(cfg: Any, store: Any, log: Any, data_dir: Path) -> Any:
    from ..llm.usage import UsageAccounting

    reader = getattr(log, "_audit_reader", None)
    if reader is None:
        try:
            from ..audit.reader import AuditReader

            # Constructed lazily by path (the reader returns [] when the file
            # does not exist yet), so `session/usage.get` works on a core whose
            # audit component is not installed instead of reporting 0 silently
            # because it could not find one.
            reader = AuditReader(data_dir / "audit" / "audit.db")
        except Exception:  # pragma: no cover - audit package absent
            reader = None
    return UsageAccounting(config=cfg, store=store, audit_reader=reader, emit=log.emit)


def _history_of(log: Any, *, layered: bool = False) -> Any:
    from ..loop.catalog import is_catalog_message
    from ..loop.context import filter_superseded

    def history(session_id: str) -> tuple[Any, ...]:
        session = log.get_session(session_id)
        if session is None:
            return ()
        # RFC-0006 §6.6: drop replaced versions before anything fits the window.
        kept = filter_superseded(session.messages, session.turns)
        if layered:
            # RFC-0003: the catalog message is part of the model's context.
            return tuple(kept)
        # RFC-0003 §7: a session that ran layered keeps its catalog messages in
        # the log (seq and replay stay intact); legacy FILTERS them out of the
        # model request instead of deleting them, so switching back never
        # resurrects a durable reminder as user speech.
        return tuple(
            m for m in kept
            if not (m.role == "user" and is_catalog_message(m.content))
        )

    return history


def _build_llm(cfg: Any, *, router: Any = None, registry: Any = None) -> Any:
    from ..llm.mock import MockLLMProvider

    provider = str(cfg.get("llm.provider", "openai") or "openai")
    if provider == "mock":
        return MockLLMProvider([])
    builtin = _build_builtin(cfg)
    if provider == "module":
        from ..llm.module import ModuleLLMProvider

        # `fallback_to_builtin` is read with a default because C-18 does not
        # declare it yet (see RESULT: config schema gap, not a C3 decision).
        fallback = builtin if cfg.get("llm.fallback_to_builtin", True) else None
        return ModuleLLMProvider(
            router,
            registry,
            model_module=cfg.get("llm.model_module"),
            fallback=fallback,
        )
    return builtin


def _build_builtin(cfg: Any) -> Any:
    base_url = cfg.get("llm.base_url")
    if not base_url:
        return None
    from ..llm.openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(
        base_url=str(base_url),
        model=str(cfg.get("llm.model", "gpt-4o-mini")),
        api_key_env=str(cfg.get("llm.api_key_env", "VADE_LLM_API_KEY")),
    )
