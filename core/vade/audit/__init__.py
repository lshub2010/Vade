"""Audit component (C5a): `install(app)` wires C3's `AuditPort` and retention.

Two things are created here: the port object and the daily retention sweep.
The SQLite file itself is still opened lazily — on the first record, or on the
first sweep that finds an existing store — so starting a core with no admin
traffic leaves no empty `audit.db` behind.

Config (C-18 `[audit]`):

* ``audit.path`` — where the store lives. Left at its C-18 default the store
  stays at the pre-C-18 ``<core.data_dir>/audit/audit.db``: state belongs under
  the data dir (a relative path would drop ``audit.sqlite3`` in whatever
  directory the daemon happened to start from) and that file is where every
  existing trail already lives, so upgrading must not strand it. Any other
  value is used as given; a relative one is resolved against the process
  working directory, like ``core.data_dir``.
* ``audit.retention_days`` — day tables older than this are dropped. ``0`` or a
  negative value would drop every table (including today's) on the next sweep,
  so startup is refused instead of silently destroying the trail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import ConfigError
from .adapter import AuditAdapter
from .reader import AuditReader, AuditReadError
from .retention import RetentionJob
from .writer import AuditWriter, new_audit_id

__all__ = [
    "install", "AuditWriter", "AuditAdapter", "AuditReader", "AuditReadError",
    "RetentionJob", "new_audit_id",
]

#: Mirrors the C-18 default; retention.py's own default is the same number.
DEFAULT_RETENTION_DAYS = 30
#: C-18 `audit.path` default. `load_config()` merges schema defaults, so this
#: exact string reaches `install()` whether or not anyone configured it.
DEFAULT_AUDIT_PATH = "data/audit.sqlite3"


def audit_path(config: Any) -> Path:
    """`audit.path` when it was really set, else `<data_dir>/audit/audit.db`.

    A path equal to the C-18 default carries no operator intent, and taking it
    literally would move the store out of `core.data_dir` (a daemon started
    from `/` would write `/data/audit.sqlite3`) and orphan every trail written
    before the key existed. So the default resolves to the legacy location;
    anything else is honoured.
    """
    configured = str(config.get("audit.path") or "").strip()
    if not configured or configured == DEFAULT_AUDIT_PATH:
        return Path(str(config.get("core.data_dir", "./data"))) / "audit" / "audit.db"
    return Path(configured).expanduser()


def retention_days(config: Any) -> int:
    """`audit.retention_days`, refusing a window that would drop everything.

    `load_config()` already rejects < 1 through the C-18 schema; this is the
    second layer for a component assembled with a hand-built config, and the
    one that turns a destructive value into a refusal to start rather than a
    sweep that deletes today's table on the next pass.
    """
    raw = config.get("audit.retention_days", DEFAULT_RETENTION_DAYS)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(
            "audit.retention_days", f"expected an integer, got {raw!r}", code="INVALID_ARGUMENT",
        )
    if raw < 1:
        raise ConfigError(
            "audit.retention_days",
            f"must be >= 1, got {raw}: every day table would be dropped on the next sweep",
            code="INVALID_ARGUMENT",
        )
    return raw


class _StoreAwareRetention(RetentionJob):
    """Retention that never creates a store just to find nothing to prune.

    The module contract is that a core with no admin traffic creates no audit
    file. `AuditWriter.prune()` opens SQLite (which creates the DB) before it
    can discover there are no day tables, so the sweep is skipped while the
    file does not exist. A store that exists is swept exactly as before.
    """

    def run_once(self) -> list[str]:
        if not self._writer.path.exists():
            return []
        return super().run_once()


def build_writer(
    path: Path | str, *, retention_days: int = DEFAULT_RETENTION_DAYS, sync: bool = False,
) -> AuditWriter:
    return AuditWriter(Path(path), keep_days=retention_days, sync=sync)


def install(app: Any) -> None:
    if getattr(app, "audit", None) is not None:
        return
    days = retention_days(app.config)
    writer = build_writer(audit_path(app.config), retention_days=days)
    app.audit = AuditAdapter(writer)
    app.audit_writer = writer
    app.audit_reader = AuditReader(writer.path)
    # Started here, stopped in `App.shutdown()`. The sweep is a daemon thread
    # that sleeps 300s between looks, so an idle core pays nothing for it.
    job = _StoreAwareRetention(writer, keep_days=days)
    app.audit_retention = job
    job.start()
