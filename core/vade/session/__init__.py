"""C3 component entry point: session store, queue, router, loop, UI RPC.

`install(app)` is called by C2's component scan. It must be safe on a bare App
(no registry, no LLM): the core then starts and answers `ui/submit` with a
readable "no capability" turn instead of crashing (FR-1.4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import VadeError

from .manager import SessionLog
from .queue import TurnQueue
from .rpc import SessionRPC
from .sqlite_store import SQLiteStore

__all__ = ["install", "SessionLog", "SQLiteStore", "SessionRPC", "TurnQueue"]


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
    approval = _LazyApproval(app)
    memory = _LazyMemory(app)
    engine = LoopEngine(
        router, llm,
        lambda: build_snapshot(getattr(app, "registry", None)),
        log.emit,
        history=_history_of(log),
        approval=approval,
        memory=memory,
        prompt_hooks=lambda: getattr(app, "prompt_hooks", []),
        config=LoopConfig(max_steps=int(cfg.get("policy.max_steps", 8)),
                          approval_timeout_s=int(cfg.get("policy.approval_timeout_s", 60))),
        compaction=compaction,
        blob_store=BlobStore(data_dir / "blobs"),
    )
    log.set_executor(engine.run_turn)
    log.set_cancel_hook(lambda turn_id: router.cancel_turn(turn_id) if router is not None else None)
    log.set_cancel_clear_hook(
        lambda turn_id: router.clear_cancelled(turn_id) if router is not None else None
    )
    log.approval_port = approval

    rpc = SessionRPC(log, approval=lambda: getattr(app, "approval_port", None))
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


def _history_of(log: Any) -> Any:
    def history(session_id: str) -> tuple[Any, ...]:
        session = log.get_session(session_id)
        return session.messages if session is not None else ()

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
