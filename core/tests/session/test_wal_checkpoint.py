"""WAL checkpoint policy: hot path stays off the checkpoint, WAL stays bounded.

Pins the four properties the policy is supposed to have, against real SQLite:

1. `append()` never checkpoints — a burst that crosses SQLite's own 4 MiB /
   1000-frame auto-checkpoint watermark leaves the WAL over it and no
   checkpoint ran, i.e. `wal_autocheckpoint=0` really is in force.
2. 1000 appends through the real manager (`SessionLog.emit`, which is where
   the probe is wired) keep p95 under 10 ms *while* background checkpoints run.
3. The WAL that outgrew the threshold falls back once the checkpoint has run.
4. `close()` truncates the sidecar and a reopen sees every row; concurrent
   readers, writers and a checkpoint never produce "database is locked".
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vade.session.manager import SessionLog
from vade.session.models import Event, Message, Session
from vade.session.replay import ReplayBuffer
from vade.session.sqlite_store import SQLiteStore

#: 1000 appends of this size put ~9 MiB of frames in the WAL: past SQLite's
#: ~4 MiB auto-checkpoint watermark, far past the small thresholds below.
_BODY = "x" * 9000

#: Latency runs use a smaller body so the burst lands right after the default
#: 16 MiB threshold instead of far past it: one background checkpoint fires
#: mid-run, which is the case the p95 bound is supposed to cover.
_LATENCY_BODY = "y" * 2048


def _wal_bytes(path: Path) -> int:
    try:
        return os.path.getsize(str(path) + "-wal")
    except OSError:
        return 0


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * len(ordered))) - 1))
    return ordered[idx]


def _append(store: SQLiteStore, sess_id: str, i: int, body: str = _BODY) -> None:
    seq = store.next_seq(sess_id)
    store.append(
        sess_id,
        [Message(id=f"m{i}", session_id=sess_id, role="assistant", content=body, seq=seq)],
        [Event(session_id=sess_id, seq=seq, type="turn.delta",
               payload={"turn_id": "t1", "text": body})],
    )


def _settle(store: SQLiteStore, timeout: float = 120.0) -> None:
    """Wait for an in-flight background checkpoint (a real page copy is I/O)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and store._ckpt_inflight:
        time.sleep(0.01)


async def _settle_async(store: SQLiteStore, timeout: float = 120.0) -> None:
    """`_settle` for a running loop: never block the thread the task needs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and store._ckpt_inflight:
        await asyncio.sleep(0.01)


# --------------------------------------------------------------- 1. write path
def test_write_path_never_checkpoints(tmp_path):
    """`wal_autocheckpoint=0`: 9 MiB of WAL, zero checkpoints, no shrinking."""
    path = tmp_path / "sessions.db"
    # Threshold deliberately out of reach: this test is about SQLite's own
    # auto-checkpoint, not about ours.
    store = SQLiteStore(path, checkpoint_bytes=64 * 1024 * 1024)
    store.create_session(Session(id="s1", created_at=1))
    for i in range(1000):
        _append(store, "s1", i)

    grows = _wal_bytes(path)
    # SQLite's own default would have checkpointed (and reset) at ~4 MiB; the
    # file is far past that, which is only possible with auto-checkpoint off.
    assert grows > 8 * 1024 * 1024
    assert store._ckpt_runs == 0
    assert store.checkpoint_if_needed() is False  # under the configured threshold
    store.close()


def test_schedule_checkpoint_returns_without_waiting_for_the_page_copy(tmp_path):
    """The trigger is a probe: it schedules, it never checkpoints inline."""
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path, checkpoint_bytes=64 * 1024)
    store.create_session(Session(id="s1", created_at=1))
    for i in range(20):  # ~900 KiB of frames: over the 64 KiB threshold
        _append(store, "s1", i)
    assert _wal_bytes(path) > 64 * 1024

    async def scenario() -> None:
        started = time.perf_counter()
        assert store.schedule_checkpoint() is True
        elapsed = time.perf_counter() - started
        # Scheduling is a stat() + create_task. The copy it hands to a worker
        # thread is orders of magnitude slower than this bound on any disk.
        assert elapsed < 0.05, f"schedule_checkpoint blocked for {elapsed * 1000:.1f} ms"
        assert store._ckpt_inflight is True
        # Second call while one is in flight: no second task.
        assert store.schedule_checkpoint() is False
        await _settle_async(store)
        assert store._ckpt_runs == 1

    asyncio.run(scenario())
    store.close()


# ------------------------------------------------------------------ 2. latency
def test_thousand_appends_keep_p95_under_10ms_while_checkpointing(tmp_path):
    """1000 real `emit`s, a checkpoint firing mid-run, p95 under 10 ms.

    Repeated three times: a p95 that only survives one run is not a fix.
    """
    p95s: list[float] = []
    for run in range(3):
        path = tmp_path / f"sessions{run}.db"
        # Production shape: the default threshold. 1000 x ~40 KiB of frames
        # crosses 16 MiB mid-burst, so a real page copy happens between two of
        # these `emit`s and must not be what pushes the p95 over the bound.
        store = SQLiteStore(path)
        log = SessionLog(store, replay=ReplayBuffer(maxlen=1200))
        session = log.create_session()

        async def scenario(session_id: str) -> tuple[list[float], int]:
            latencies: list[float] = []
            for i in range(1000):
                message = Message(id=f"m{i}", session_id=session_id, role="assistant",
                                  content=_LATENCY_BODY, seq=0)
                started = time.perf_counter()
                await log.emit(session_id, "turn.delta",
                               {"turn_id": "t1", "text": _LATENCY_BODY}, messages=[message])
                latencies.append((time.perf_counter() - started) * 1000.0)
                if i % 50 == 49:
                    await asyncio.sleep(0)  # let a scheduled checkpoint start
            await _settle_async(store)  # inside the loop: the task needs it
            return latencies, store._ckpt_runs

        latencies, checkpoints = asyncio.run(scenario(session.id))
        p95 = _pct(latencies, 0.95)
        p95s.append(p95)
        asyncio.run(log.close())

        assert checkpoints >= 1, "no checkpoint ran during the burst: the test proves nothing"
        assert p95 < 10.0, f"run {run}: p95 {p95:.2f} ms over 1000 appends"

    # Repeatability: every run met the bound, not just the luckiest one.
    assert max(p95s) < 10.0


# ------------------------------------------------------------- 3. fall-back
def test_wal_falls_back_after_the_threshold_triggers_a_checkpoint(tmp_path):
    path = tmp_path / "sessions.db"
    threshold = 64 * 1024
    store = SQLiteStore(path, checkpoint_bytes=threshold, checkpoint_min_interval_s=0.0)
    store.create_session(Session(id="s1", created_at=1))
    for i in range(20):
        _append(store, "s1", i)

    grown = _wal_bytes(path)
    assert grown > threshold, f"WAL did not grow past the threshold: {grown} bytes"

    assert store.checkpoint_if_needed() is True
    assert store._ckpt_runs == 1
    # PASSIVE backfills every frame; the file itself is given back by the next
    # writer's log restart (`journal_size_limit=0`), so poke it once.
    _append(store, "s1", 20)
    after = _wal_bytes(path)

    assert after < grown / 2, f"WAL did not fall back: {grown} -> {after} bytes"
    assert after <= threshold
    # And it keeps converging instead of creeping: another burst plus a
    # checkpoint lands under the threshold again.
    for i in range(21, 41):
        _append(store, "s1", i)
    assert store.checkpoint_if_needed() is True
    _append(store, "s1", 41)
    assert _wal_bytes(path) <= threshold

    assert store.load("s1").messages  # nothing lost by any of the above
    store.close()


# ------------------------------------------------------- 4. close / concurrent
def test_close_truncates_the_wal_and_reopen_returns_every_row(tmp_path):
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path, checkpoint_bytes=64 * 1024)
    store.create_session(Session(id="s1", created_at=1, title="kept"))
    for i in range(40):
        _append(store, "s1", i)
    assert _wal_bytes(path) > 0

    store.close()
    store.close()  # idempotent: a teardown race must not raise
    assert _wal_bytes(path) == 0

    reopened = SQLiteStore(path)
    loaded = reopened.load("s1")
    assert loaded is not None and loaded.title == "kept"
    assert len(loaded.messages) == 40
    assert [e.seq for e in reopened.read_since("s1", 0)] == list(range(1, 41))
    assert reopened.next_seq("s1") == 41  # sequence survives the checkpoint
    reopened.close()


def test_concurrent_reads_writes_and_checkpoints_never_lock(tmp_path):
    """The checkpoint runs on its own connection: no SQLITE_BUSY, no lost rows.

    This is the regression the private connection exists for — a checkpoint
    that shared the write lock would surface here as "database is locked" (or
    as a stalled append) while the writers hammer.
    """
    path = tmp_path / "sessions.db"
    threshold = 64 * 1024
    store = SQLiteStore(path, checkpoint_bytes=threshold, checkpoint_min_interval_s=0.0)
    store.create_session(Session(id="s1", created_at=1))

    errors: list[BaseException] = []
    stop = threading.Event()
    per_writer = 150
    writers = 4

    def writer(slot: int) -> None:
        try:
            for i in range(per_writer):
                _append(store, "s1", slot * per_writer + i)
                if i % 20 == 0:
                    store.schedule_checkpoint()  # no running loop: a no-op probe
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                loaded = store.load("s1")
                assert loaded is not None
                store.read_since("s1", 0)
                store.list_session_rows(limit=10)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def checkpointer() -> None:
        try:
            while not stop.is_set():
                result = store.checkpoint_passive()
                assert result is None or result[0] in (0, 1)  # 1 = a reader won
                time.sleep(0.01)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=writers + 2) as pool:
        futures = [pool.submit(writer, slot) for slot in range(writers)]
        readers = [pool.submit(reader) for _ in range(2)]
        ckpt = pool.submit(checkpointer)
        for future in futures:
            future.result(timeout=120)
        stop.set()
        for future in [*readers, ckpt]:
            future.result(timeout=60)

    assert not errors, f"concurrent access raised: {errors!r}"
    loaded = store.load("s1")
    assert loaded is not None
    assert len(loaded.messages) == writers * per_writer
    assert len(store.read_since("s1", 0)) == writers * per_writer
    store.close()
