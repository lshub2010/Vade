"""`AuditPort` adapter — the only thing C3 sees."""

from __future__ import annotations

from typing import Any, Mapping

from .writer import AuditWriter

__all__ = ["AuditAdapter"]


class AuditAdapter:
    def __init__(self, writer: AuditWriter) -> None:
        self._writer = writer

    @property
    def writer(self) -> AuditWriter:
        return self._writer

    def record(self, event: Mapping[str, Any]) -> None:
        self._writer.record(event)

    def record_event(self, event: Mapping[str, Any]) -> str:
        """Same as `record` but returns the `audit_id` the admin API echoes."""
        return self._writer.record(event)
