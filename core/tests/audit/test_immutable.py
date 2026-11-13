"""Audit rows are append-only — enforced by SQLite triggers, not by convention."""

from __future__ import annotations

import sqlite3

import pytest

from vade.audit.reader import AuditReader
from vade.audit.writer import AuditWriter, day_key


def _writer(tmp_path):
    return AuditWriter(tmp_path / "audit" / "audit.db", sync=True)


def _rows(path):
    db = sqlite3.connect(path)
    try:
        table = f"audit_{day_key()}"
        return db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        db.close()


def test_update_is_aborted_and_the_row_is_unchanged(tmp_path):
    writer = _writer(tmp_path)
    writer.record({"kind": "tool_call", "tool": "com.example.fs/write_file", "status": "ok"})
    before = _rows(writer.path)
    db = sqlite3.connect(writer.path)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(f'UPDATE "audit_{day_key()}" SET status = ?', ("tampered",))
    db.rollback()
    db.close()
    assert _rows(writer.path) == before
    [row] = AuditReader(writer.path).query()["items"]
    assert row["status"] == "ok"
    writer.close()


def test_delete_is_aborted(tmp_path):
    writer = _writer(tmp_path)
    writer.record({"kind": "approval", "status": "allowed"})
    db = sqlite3.connect(writer.path)
    with pytest.raises(sqlite3.DatabaseError):
        db.execute(f'DELETE FROM "audit_{day_key()}"')
    db.rollback()
    db.close()
    assert _rows(writer.path) == 1
    writer.close()


def test_pruning_drops_only_tables_outside_the_window(tmp_path):
    writer = _writer(tmp_path)
    writer.record({"kind": "tool_call"})
    old = 1_600_000_000.0
    writer.record({"kind": "tool_call", "ts": old})
    dropped = writer.prune(now=old + 40 * 86_400, keep_days=30)
    assert f"audit_{day_key(old)}" in dropped
    # The drop itself is audited in the current table.
    rows = AuditReader(writer.path).query(kind="audit.pruned")["items"]
    assert rows and rows[0]["status"] == "ok"
    writer.close()
