"""Retention is wired to the component lifecycle (C5a T3, C-18 `[audit]`).

Three holes a unit test of `RetentionJob` alone cannot see:

* `install()` read a retention key that exists in no C-18 section — so
  ``audit.retention_days`` was silently ignored.
* The store path was hardcoded to ``<data_dir>/audit/audit.db``; ``audit.path``
  did nothing.
* `RetentionJob` was never instantiated, so the day tables grew forever.

These tests drive the real `install()`/`App.shutdown()` pair and the real
`prune()`: a fake job would pass with all three still present.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from vade import audit as audit_component
from vade.app import App
from vade.audit.reader import AuditReader
from vade.audit.writer import day_key
from vade.config import Config, ConfigError, load_config


def _app(tmp_path, **cli):
    return App(load_config(cli={"core.data_dir": str(tmp_path), **cli}))


def _tables(path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        db.close()


def _alive(job) -> bool:
    thread = getattr(job, "_thread", None)
    return thread is not None and thread.is_alive()


def test_install_starts_the_retention_job_and_shutdown_stops_it(tmp_path):
    app = _app(tmp_path)
    audit_component.install(app)
    job = getattr(app, "audit_retention")
    assert job is not None
    assert job.keep_days == 30
    assert _alive(job)

    # Dev hot reload installs twice; a second sweeper would be a second thread
    # pruning the same file.
    audit_component.install(app)
    assert getattr(app, "audit_retention") is job and _alive(job)

    asyncio.run(app.shutdown())
    assert job._stop.is_set()
    assert job._thread is None
    assert not _alive(job)


def test_a_forty_day_old_table_is_dropped_and_the_drop_is_audited(tmp_path):
    app = _app(tmp_path)
    audit_component.install(app)
    writer = getattr(app, "audit_writer")
    # The background loop is a wall-clock timer (03:00 UTC); `run_once` is the
    # unit under test, so stop the timer instead of racing it.
    getattr(app, "audit_retention").stop()
    try:
        old = time.time() - 40 * 86_400
        old_table = f"audit_{day_key(old)}"
        writer.record({"kind": "tool_call", "ts": old, "tool": "com.example/fs.write_file"})
        writer.record({"kind": "tool_call"})
        writer.flush()
        assert old_table in _tables(writer.path)

        dropped = getattr(app, "audit_retention").run_once()

        assert dropped == [old_table]
        assert old_table not in _tables(writer.path)
        # Today's table is inside the 30-day window and survives.
        assert f"audit_{day_key()}" in _tables(writer.path)
        # The drop itself is a row in the current day table, not a silent DDL.
        writer.flush()
        pruned = AuditReader(writer.path).query(kind="audit.pruned")["items"]
        assert pruned and pruned[0]["status"] == "ok"
        assert pruned[0]["extra"]["tables"] == [old_table]
    finally:
        asyncio.run(app.shutdown())
        writer.close()


def test_the_c18_default_path_stays_under_the_data_dir(tmp_path):
    # `load_config()` merges the schema default, so install() always sees
    # "data/audit.sqlite3". Taking that literally would put the store in the
    # daemon's working directory instead of `core.data_dir`.
    app = _app(tmp_path)
    assert app.config.get("audit.path") == "data/audit.sqlite3"
    audit_component.install(app)
    try:
        assert getattr(app, "audit_writer").path == tmp_path / "audit" / "audit.db"
    finally:
        asyncio.run(app.shutdown())
        getattr(app, "audit_writer").close()


def test_audit_path_config_decides_where_the_store_lives(tmp_path):
    target = tmp_path / "custom" / "governance.sqlite3"
    app = _app(tmp_path, **{"audit.path": str(target)})
    audit_component.install(app)
    writer = getattr(app, "audit_writer")
    try:
        assert writer.path == target
        # Still lazy: nothing has been recorded, so no file (and the sweep must
        # not create one just to discover there is nothing to prune).
        assert not target.exists()

        writer.record({"kind": "tool_call"})
        writer.flush()

        assert target.is_file()
        # The legacy hardcoded location must stay empty.
        assert not (tmp_path / "audit" / "audit.db").exists()
    finally:
        asyncio.run(app.shutdown())
        writer.close()


@pytest.mark.parametrize("days", [0, -1])
def test_a_nonpositive_window_refuses_to_start(tmp_path, days):
    # `load_config()` already refuses this through the C-18 schema (`minimum:
    # 1`); the component keeps its own guard for a hand-built config, because
    # the failure mode here is deleting the whole trail.
    with pytest.raises(ConfigError):
        load_config(cli={"core.data_dir": str(tmp_path), "audit.retention_days": days})

    app = App(Config(values={"core": {"data_dir": str(tmp_path)}, "audit": {"retention_days": days}}, schema={}))
    with pytest.raises(ConfigError) as excinfo:
        audit_component.install(app)
    assert "audit.retention_days" in str(excinfo.value)
    assert ">=" in str(excinfo.value)
    assert getattr(app, "audit", None) is None
    assert not hasattr(app, "audit_retention")
