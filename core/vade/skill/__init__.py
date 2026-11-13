"""C3b: the skill package (`core/vade/skill/`).

A skill is a **manual**: `SKILL.md` + optional `references/`. It cannot register
a tool, execute a script, or arrive from the network. Loading is text-only and
install-time code never runs (see `docs/design.md` §2.7.4).

`install(app)` is the only entry point core calls. It is idempotent: dev hot
reload may run `install_components` twice and a second hook would render the
catalog twice.
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

from .contract import Contract, load_contract
from .loader import InstallSource, PreviewResult, SkillLoader
from .model import SkillMeta, SkillState, SkillSummary
from .render import Rendered

__all__ = [
    "install", "SkillLoader", "InstallSource", "PreviewResult",
    "SkillMeta", "SkillState", "SkillSummary", "Rendered", "Contract",
]

_SIGHUP_INSTALLED = False


def _tool_snapshot_factory(app: Any) -> Any:
    """`registry.tool_snapshot()` if it exists, else C3's `build_snapshot`.

    `app.tool_snapshot` does not exist (the task book warns about this name):
    the registry owns the live table, `loop.tools` owns the projection.
    """
    registry = getattr(app, "registry", None)
    if registry is not None and callable(getattr(registry, "tool_snapshot", None)):
        return registry.tool_snapshot
    try:
        from ..loop.tools import build_snapshot
    except Exception:  # pragma: no cover - loop is present in every real build
        return lambda: ()
    return lambda: build_snapshot(registry)


def _install_sighup(loader: SkillLoader) -> None:
    """One SIGHUP handler per process: full rescan, no file watching."""
    global _SIGHUP_INSTALLED
    if _SIGHUP_INSTALLED:
        return
    try:
        previous = signal.getsignal(signal.SIGHUP)

        def _handler(signum: int, frame: Any) -> None:
            try:
                loader.reload()
            except Exception:  # a reload must never kill the event loop
                pass
            if callable(previous) and previous not in (signal.SIG_DFL, signal.SIG_IGN):
                previous(signum, frame)  # type: ignore[misc]

        signal.signal(signal.SIGHUP, _handler)
        _SIGHUP_INSTALLED = True
    except (AttributeError, ValueError, OSError):  # non-main thread / Windows
        pass


def install(app: Any) -> None:
    loader = getattr(app, "skill_loader", None)
    if not isinstance(loader, SkillLoader):
        config = getattr(app, "config", None)
        data_dir = Path(str(config.get("core.data_dir", "./data"))) if config is not None else Path("./data")
        loader = SkillLoader(
            skills_dir=data_dir / "skills",
            tool_snapshot=_tool_snapshot_factory(app),
            config=config,
        )
        app.skill_loader = loader
        # A broken skill directory must not stop core from starting.
        try:
            loader.reload()
        except Exception as exc:  # pragma: no cover - defensive
            loader._warn(f"skill reload failed at startup: {type(exc).__name__}")
        _install_sighup(loader)

    hooks = getattr(app, "prompt_hooks", None)
    if hooks is None:
        hooks = []
        app.prompt_hooks = hooks
    if not any(getattr(hook, "__self__", None) is loader for hook in hooks):
        hooks.append(loader.render)

    failure_hooks = getattr(app, "tool_failure_hooks", None)
    if failure_hooks is None:
        failure_hooks = []
        app.tool_failure_hooks = failure_hooks
    if not any(getattr(hook, "__self__", None) is loader for hook in failure_hooks):
        failure_hooks.append(loader.record_tool_failure)
