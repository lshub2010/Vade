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


def test_a_failed_batch_is_retried_before_anything_is_lost(tmp_path):
    """One sqlite error must not cost the batch — and must not kill the thread.

    The old loop logged "batch dropped" and moved on, so a transient
    `SQLITE_BUSY` silently destroyed every row whose `audit_id` the caller
    already held (review P1-4). A retry on a fresh handle is what that failure
    actually needed.
    """
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
        while writer.write_retries == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.write_retries == 1, "the failed batch was never retried"
        assert len(calls) >= 2, "the batch reached `_write_many` only once"
        assert writer.write_failures == 0, "a batch that landed on retry is not lost"
        assert writer._thread is not None and writer._thread.is_alive(), (
            "one sqlite error used to kill the flusher thread"
        )

        writer.record(_row(tokens=2))
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) >= 3, "the flusher stopped consuming after one bad batch"
    finally:
        writer.close()

    assert sorted(_stored_tokens(path)) == [1, 2], "the retried row must be on disk"
    assert writer.write_failures == 0
    assert writer.dead_letters == 0


def test_a_batch_that_fails_twice_is_dead_lettered_with_its_audit_ids(tmp_path):
    """Permanent failure: the rows go to `audit.deadletter.jsonl`, not nowhere.

    The caller already has these `audit_id`s, so the loss has to be enumerable:
    one JSON line per row, with the id and the original row, and a counter that
    `/admin/api/health` reports (review P1-4).
    """
    path = tmp_path / "audit.db"
    writer = AuditWriter(path, batch_ms=10, max_queue=100)
    attempts: list[int] = []

    def broken(rows: list[dict]) -> None:
        attempts.append(len(rows))
        raise sqlite3.OperationalError("database or disk is full")

    writer._write_many = broken  # type: ignore[method-assign]

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.DEBUG)
    logger = logging.getLogger("vade.audit")
    logger.addHandler(handler)
    try:
        ids = [writer.record(_row(tokens=n)) for n in (1, 2, 3)]
        deadline = time.monotonic() + 5.0
        while writer.dead_letters < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.write_failures >= 1, "the lost batch must be counted"
        assert writer.dead_letters == 3, "every row must be parked in the dead letter"
        assert len(attempts) >= 2, "the batch was not retried before being parked"
        assert writer._thread is not None and writer._thread.is_alive()

        parked = writer.dead_letter_rows()
        assert [entry["audit_id"] for entry in parked] == ids, parked
        assert [entry["row"]["extra"]["tokens"] for entry in parked] == [1, 2, 3]
        assert all("disk is full" in entry["error"] for entry in parked), parked
        assert all(entry["batch_id"] for entry in parked), (
            "every parked row must name the batch it came from"
        )
        assert len({entry["batch_id"] for entry in parked}) <= len(attempts), parked
        assert writer.dead_letters == 3
        assert len(_stored_tokens(path)) == 0 or not path.exists(), (
            "nothing may have landed in the audit database"
        )
    finally:
        logger.removeHandler(handler)
        writer.close()

    errors = [r.getMessage() for r in records if r.levelno >= logging.ERROR]
    assert any("dead-lettered" in message for message in errors), errors
    assert any(ids[0] in message for message in errors), (
        f"the log must carry the lost audit ids, got {errors}"
    )
    # The ids stay queryable from the file, which is what an operator needs.
    assert writer.deadletter_path.is_file()
    assert writer.deadletter_path.name == "audit.deadletter.jsonl"


def test_an_async_audit_failure_is_visible_on_health(tmp_path):
    """Production shape (`sync=False`): nothing raises, so health must report it.

    `record()` returns an ULID as soon as the row is queued, which means the
    sqlite error happens on the flusher thread and `write_audit`'s `except` never
    fires. Before `/admin/api/health` published the writer's counters, the only
    trace of a dead audit chain was a log line (review P1-3).
    """
    ctx = make_context(tmp_path)
    ctx.audit.close()
    blocked = tmp_path / "audit-blocked"
    blocked.mkdir()  # sqlite cannot open a directory as a database file
    writer = AuditWriter(blocked, batch_ms=10, max_queue=100)  # sync defaults to False
    ctx.audit = writer
    app = create_admin_app(ctx)
    try:
        audit_id = ctx.write_audit(kind="admin.config", status="ok")
        assert audit_id is not None, "the async path hands out the id before the write"
        deadline = time.monotonic() + 5.0
        while writer.write_failures == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer.write_failures >= 1, "the flusher never hit the blocked database"
        assert ctx.audit_failures == 0, (
            "sync=False failures never reach write_audit — that is the whole bug"
        )

        async def scenario():
            local = await client_for(app).get("/admin/api/health")
            remote = await client_for(app, ip="203.0.113.9").get("/admin/api/health")
            return local, remote

        local, remote = asyncio.run(scenario())
        assert local.status_code == 200, local.text
        audit = local.json()["audit"]
        assert audit["installed"] is True
        assert audit["writer_failures"] >= 1, audit
        assert audit["dead_letters"] >= 1, audit
        assert audit["total_failures"] == audit["request_failures"] + audit["writer_failures"]
        assert audit["request_failures"] == 0
        assert str(audit["deadletter_path"]).endswith(".deadletter.jsonl")
        assert local.json()["ok"] is True, "a degraded audit must not fail liveness"
        # Remote probes still get the bare document: no counters, no path.
        assert remote.json() == {"ok": True}
    finally:
        writer.close()


def test_health_reports_the_request_side_counter_too(tmp_path):
    """`ctx.audit_failures` (a *synchronous* failure) reaches health as well."""
    ctx = make_context(tmp_path)

    class _Broken:
        path = tmp_path / "audit" / "audit.db"
        write_failures = 0
        write_retries = 0
        dead_letters = 0
        degraded = False

        def record(self, event):  # noqa: ANN001 - writer stand-in
            raise sqlite3.OperationalError("database is locked")

    ctx.audit = _Broken()  # type: ignore[assignment]
    assert ctx.write_audit(kind="x", status="ok") is None
    assert ctx.audit_failures == 1

    async def scenario():
        return await client_for(create_admin_app(ctx)).get("/admin/api/health")

    response = asyncio.run(scenario())
    audit = response.json()["audit"]
    assert audit["request_failures"] == 1
    assert audit["writer_failures"] == 0
    assert audit["total_failures"] == 1


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
