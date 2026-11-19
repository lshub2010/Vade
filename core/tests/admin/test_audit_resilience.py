"""Audit failures must be loud, survivable and temporary (review P0-4 / P1-2).

Three separate silent-failure paths are pinned here:

* `AdminContext.write_audit` used to swallow every exception — a full disk or a
  read-only `audit.db` produced `audit_id: null` on a 200 response with no log
  line and no counter anywhere.
* `AuditWriter._run` had no guard, so one sqlite error killed the flusher (and
  its batch) until some later `record()` noticed the dead thread.
* `_degraded` was set on the first `queue.Full` and never cleared, pinning every
  subsequent write onto the caller's thread — the event loop — forever.

The db is made unopenable by pointing the writer at a *directory*: the suite may
run as root, where `chmod 0444` does not stop sqlite from writing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from vade.admin.app import create_admin_app
from vade.audit.writer import AuditWriter, day_key

from .conftest import client_for, login_with_device, make_context, new_device


def _row(*, tokens: int = 1, ts: float | None = None, purpose: str = "evolve") -> dict:
    return {
        "kind": "llm.call",
        "ts": ts if ts is not None else time.time(),
        "extra": {"purpose": purpose, "tokens": tokens},
    }


def _stored_tokens(path: Path) -> list[int]:
    """Every `tokens` value in the audit store, whatever day table holds it."""
    connection = sqlite3.connect(path)
    try:
        names = [
            r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'audit\\_%' ESCAPE '\\'"
            )
        ]
        out: list[int] = []
        for name in names:
            for (raw,) in connection.execute(f'SELECT extra FROM "{name}"'):
                extra = json.loads(raw) if raw else {}
                if "tokens" in extra:
                    out.append(int(extra["tokens"]))
        return out
    finally:
        connection.close()


def test_an_unwritable_audit_db_is_reported_on_the_write_response(tmp_path):
    ctx = make_context(tmp_path)
    ctx.audit.close()
    blocked = tmp_path / "audit-blocked"
    blocked.mkdir()  # sqlite cannot open a directory as a database file
    ctx.audit = AuditWriter(blocked, sync=True)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.DEBUG)
    logger = logging.getLogger("vade.admin")
    logger.addHandler(handler)
    try:
        async def scenario():
            device, key = new_device(ctx)
            client = client_for(create_admin_app(ctx))
            token = (await login_with_device(client, ctx, device, key)).json()["access_token"]
            response = await client.put(
                "/admin/api/config",
                json={"key": "evolve.enabled", "value": False, "confirm": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200, response.text
            return response.json()

        body = asyncio.run(scenario())
    finally:
        logger.removeHandler(handler)
        ctx.audit.close()

    assert body["audit_id"] is None, body
    assert body["audit_unavailable"] is True, (
        f"a failed audit write must be visible on the response, not just a null id: {body}"
    )
    assert ctx.audit_failures >= 1, "write_audit must count failures"
    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert any("audit write failed" in r.getMessage() for r in errors), (
        f"write_audit must log the failure, got {[r.getMessage() for r in records]}"
    )


def test_a_write_with_no_audit_writer_is_not_reported_as_a_failure(tmp_path):
    """`audit: null` is "not configured"; the flag means "configured, broken"."""
    ctx = make_context(tmp_path)
    ctx.audit = None
    assert ctx.write_audit(kind="x", status="ok") is None
    assert ctx.audit_failures == 0


def test_degraded_mode_ends_when_the_queue_drains(tmp_path):
    path = tmp_path / "audit.db"
    writer = AuditWriter(path, batch_ms=200, max_queue=1)
    writer._ensure_worker = lambda: None  # type: ignore[method-assign] - stalled flusher
    try:
        writer.record(_row(tokens=1))
        assert writer.degraded is False
        writer.record(_row(tokens=2))  # queue holds one row -> Full -> sync + degraded
        assert writer.degraded is True, "queue.Full must degrade"
        assert writer._queue.qsize() == 1

        writer.flush()  # the backlog drains
        assert writer._queue.qsize() == 0

        writer.record(_row(tokens=3))
        assert writer.degraded is False, (
            "degraded must end once the queue is below half; it used to stay set "
            "until process restart, putting every write on the event loop"
        )
        assert writer._queue.qsize() == 1, "after recovery writes go back through the queue"
        writer.flush()
    finally:
        writer.close()

    assert sorted(_stored_tokens(path)) == [1, 2, 3]


def test_the_flusher_survives_a_failed_batch(tmp_path):
    path = tmp_path / "audit.db"
    writer = AuditWriter(path, batch_ms=10, max_queue=100)
    calls: list[list[dict]] = []
    original = writer._write_many

    def flaky(rows: list[dict]) -> None:
        calls.append(list(rows))
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return original(rows)

    writer._write_many = flaky  # type: ignore[method-assign]
    try:
        writer.record(_row(tokens=1))
        deadline = time.monotonic() + 5.0
        while writer.write_failures == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.write_failures == 1, "the failed batch must be counted"
        assert writer._thread is not None and writer._thread.is_alive(), (
            "one sqlite error used to kill the flusher thread"
        )

        writer.record(_row(tokens=2))
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 2, "the flusher stopped consuming after one bad batch"
    finally:
        writer.close()

    assert 2 in _stored_tokens(path), "the batch after the failure must land"


def test_the_writer_reconnects_after_a_failed_batch(tmp_path):
    """A poisoned handle must not poison every later batch."""
    path = tmp_path / "audit.db"
    writer = AuditWriter(path, batch_ms=10)
    writer.record(_row(tokens=1))
    writer.flush()

    writer._reset_db()
    assert writer._db is None
    writer.record(_row(tokens=2))
    writer.flush()
    assert sorted(_stored_tokens(path)) == [1, 2]


def test_day_key_matches_the_table_the_writer_creates(tmp_path):
    """Guard for `sum_extra_int`, which builds the table name itself."""
    path = tmp_path / "audit.db"
    writer = AuditWriter(path, sync=True)
    try:
        now = time.time()
        writer.record(_row(tokens=1, ts=now))
        writer.flush()
        connection = sqlite3.connect(path)
        try:
            names = [r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )]
        finally:
            connection.close()
        assert f"audit_{day_key(now)}" in names, names
    finally:
        writer.close()
