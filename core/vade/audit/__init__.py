"""Audit component (C5a): `install(app)` wires C3's `AuditPort`.

Only the port object is created here — the SQLite file is opened on the first
record, so importing/starting a core with no admin traffic creates nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapter import AuditAdapter
from .reader import AuditReader
from .retention import RetentionJob
from .writer import AuditWriter, new_audit_id

__all__ = ["install", "AuditWriter", "AuditAdapter", "AuditReader", "RetentionJob", "new_audit_id"]


def build_writer(data_dir: Path | str, *, keep_days: int = 30, sync: bool = False) -> AuditWriter:
    return AuditWriter(Path(data_dir) / "audit" / "audit.db", keep_days=keep_days, sync=sync)


def install(app: Any) -> None:
    if getattr(app, "audit", None) is not None:
        return
    data_dir = Path(str(app.config.get("core.data_dir", "./data")))
    writer = build_writer(data_dir, keep_days=int(app.config.get("audit.keep_days", 30)))
    app.audit = AuditAdapter(writer)
    app.audit_writer = writer
    app.audit_reader = AuditReader(writer.path)
