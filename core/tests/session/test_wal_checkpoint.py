"""WAL checkpoint policy: hot path stays off the checkpoint, WAL stays bounded.

Pins the properties the policy is supposed to have, against real SQLite:

1. `append()` never checkpoints — a burst that crosses SQLite's own 4 MiB /
   1000-frame auto-checkpoint watermark leaves the WAL over it and no
   checkpoint ran, i.e. `wal_autocheckpoint=0` really is in force.
2. 1000 appends through the real manager (`SessionLog.emit`, which is where
   the probe is wired) keep p95 under 10 ms *while* background checkpoints run.
3. The WAL that outgrew the threshold falls back once the checkpoint has run.
4. `close()` truncates the sidecar and a reopen sees every row; concurrent
   readers, writers and a checkpoint never produce "database is locked".
5. A *sustained* stream (2 KB deltas at 100/s, 20 s) keeps the WAL under the
   regression line for this workload — the property PASSIVE silently lost,
   because a writer that never stops meant the log was never reset.
6. `close()` returns with no thread of this process writing the file: the
   background checkpoint thread it may have started is joined, and the closing
   TRUNCATE runs inline (60 MiB WAL, immediate same-process reopen).
7. `aclose()` waits for an in-flight checkpoint without blocking the loop, and
   the thread is gone when it returns.
8. A `Thread.start` that fails (shutdown) does not latch the checkpoint off.
9. `_ckpt_runs` counts a checkpoint that reset the log, not one that came back
   `busy=1` with nothing truncated.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vade.session import sqlite_store
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

#: The audit's "close cannot finish the copy in its budget" size. At the 3-8
#: MB/s this disk checkpoints at, one TRUNCATE of 60 MiB is the slow case.
_BIG_WAL = 60 * 1024 * 1024

#: Body for building a big WAL quickly: the close has to copy *bytes*, and
#: 400 KB of frames per append gets to 60 MiB in a few hundred appends.
_BIG_BODY = "z" * (200 * 1024)


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


def _fill_wal(store: SQLiteStore, path: Path, target: int) -> int:
    """Append until the `-wal` sidecar is at least `target` bytes.

    Returns the number of messages written, so the caller can check a reopen
    against it. Big targets use `_BIG_BODY`: the WAL byte count is what the
    close has to copy, and the frame count per append does not matter.
    """
    body = _BIG_BODY if target > 8 * 1024 * 1024 else _BODY
    i = 0
    while _wal_bytes(path) < target:
        seq = store.next_seq("s1")
        store.append(
            "s1",
            [Message(id=f"big{i}", session_id="s1", role="assistant", content=body, seq=seq)],
            [Event(session_id="s1", seq=seq, type="turn.delta", payload={"text": body})],
        )
        i += 1
        assert i < 5000, f"the WAL never reached {target} bytes ({_wal_bytes(path)})"
    return i


def _wal_writer_threads() -> list[threading.Thread]:
    """Threads of this process that a WAL checkpoint could be running on."""
    return [t for t in threading.enumerate() if t.name.startswith("vade-wal-")]


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
    # PASSIVE + RESTART backfill every frame and reset the log; the file itself
    # is given back by the next writer's log restart (`journal_size_limit=0`),
    # so poke it once.
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
                result = store.checkpoint_restart()
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


# -------------------------------------- 5. sustained stream (the audit shape)
def test_sustained_streaming_keeps_the_wal_under_the_cap(tmp_path):
    """2 KB deltas at 100/s for 20 s: the WAL must come back, not climb.

    This is the audit workload. PASSIVE reached 41.4 MiB here and was still
    climbing, with every checkpoint reporting success: a continuous writer
    meant the log was never reset, so the sidecar was append-only. RESTART
    copies *and* resets, so the peak is `checkpoint_bytes` + write rate x copy
    duration — 16 MiB + ~2.1 MiB/s x up to ~3 s of copying on this disk, which
    measured 17-21 MiB in three runs. 24 MiB is the regression line for *this*
    workload and disk, not a design ceiling: a faster writer or a slower disk
    moves it.
    """
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path)
    log = SessionLog(store, replay=ReplayBuffer(maxlen=40))
    session = log.create_session()
    body = "x" * 2000

    async def scenario() -> tuple[int, list[float], int, int]:
        latencies: list[float] = []
        peak = 0
        started = time.perf_counter()
        i = 0
        while time.perf_counter() - started < 20.0:
            at = time.perf_counter()
            await log.emit(session.id, "turn.delta",
                           {"turn_id": "t1", "text": body, "part_index": 0, "kind": "text"})
            latencies.append((time.perf_counter() - at) * 1000.0)
            i += 1
            peak = max(peak, _wal_bytes(path))
            # Pace to 100/s on absolute time: a slow emit must not drag the
            # rate down, or the test would quietly stop pressing the WAL.
            await asyncio.sleep(max(0.0, started + i / 100 - time.perf_counter()))
        peak = max(peak, _wal_bytes(path))
        await _settle_async(store)
        return i, latencies, peak, store._ckpt_runs

    events, latencies, peak, checkpoints = asyncio.run(scenario())
    assert checkpoints >= 1, "no checkpoint ran during 20 s: the test proves nothing"
    assert events >= 1800, f"the stream ran too slowly to mean anything: {events} deltas"
    assert peak < 24 * 1024 * 1024, f"WAL peaked at {peak / 1048576:.1f} MiB"
    p95 = _pct(latencies, 0.95)
    assert p95 < 10.0, f"p95 {p95:.2f} ms over {events} deltas"

    asyncio.run(log.close())
    assert _wal_bytes(path) == 0  # and shutdown leaves no sidecar behind


# ------------------------------------- 6. close: no writer may outlive it
def test_close_after_a_large_wal_leaves_no_lock_for_an_immediate_reopen(tmp_path):
    """P1-1: a 60 MiB WAL, `close()`, then an immediate same-process reopen.

    The audit's reproduction: the old close gave its TRUNCATE a throwaway
    thread, waited 5 s, interrupted it and returned — with that thread still
    copying and holding SQLite handles. The next `SQLiteStore(path)` in the
    same process then failed with `database is locked` for as long as the
    thread lived (5.6 s measured). Now `close()` joins the thread it started
    and truncates inline, so when it returns nothing of ours is on the file.
    """
    path = tmp_path / "sessions.db"
    # No background checkpoint: this test is about the closing path itself.
    store = SQLiteStore(path, checkpoint_bytes=None)
    store.create_session(Session(id="s1", created_at=1))
    written = _fill_wal(store, path, _BIG_WAL)
    grown = _wal_bytes(path)
    assert grown >= _BIG_WAL, f"WAL only reached {grown / 1048576:.1f} MiB"

    started = time.perf_counter()
    store.close()
    elapsed = time.perf_counter() - started

    # 2) nothing of ours is left: no handle, no thread, no name on the thread
    #    list (the old code leaked `vade-wal-close-*` here).
    assert store._ckpt_thread is None
    assert _wal_writer_threads() == []
    # 1) the open/write that used to fail now needs no retry and no waiting:
    #    `timeout=0` means any lock at all fails instantly.
    probe = sqlite3.connect(str(path), timeout=0.0, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("COMMIT")
    finally:
        probe.close()
    assert _wal_bytes(path) == 0, "the inline TRUNCATE did not empty the sidecar"

    reopened = SQLiteStore(path)
    loaded = reopened.load("s1")
    assert loaded is not None
    assert len(loaded.messages) == written
    assert [m.id for m in loaded.messages] == [f"big{i}" for i in range(written)]
    assert reopened.next_seq("s1") == written + 1
    reopened.close()
    print(f"[60MiB close] {elapsed:.2f}s for {grown / 1048576:.1f} MiB")


def test_close_runs_a_slow_truncate_inline_on_the_closing_thread(tmp_path, monkeypatch):
    """The closing TRUNCATE is not handed to a thread that can be abandoned.

    Simulates the audit's slow-TRUNCATE case (a big copy that outlives any
    shutdown budget) by delaying the pragma itself, and pins what the old
    `_bounded_checkpoint` got wrong: the pragma runs on the thread that called
    `close()` (no `vade-wal-close-*` thread is spawned), `close()` does not
    return until it has finished, and what it leaves behind is a truncated
    sidecar rather than a copy in progress.
    """
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path, checkpoint_bytes=None)
    store.create_session(Session(id="s1", created_at=1))
    written = _fill_wal(store, path, _BIG_WAL)

    delay = 3.0
    callers: list[threading.Thread] = []
    real_private = sqlite_store._private_connection

    class _SlowTruncate:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, *args):
            if "TRUNCATE" in sql:
                callers.append(threading.current_thread())
                time.sleep(delay)
            return self._conn.execute(sql, *args)

        def close(self) -> None:
            self._conn.close()

    def slow_private(p, *, busy_timeout_s):
        conn = real_private(p, busy_timeout_s=busy_timeout_s)
        return _SlowTruncate(conn) if busy_timeout_s == 0 else conn

    monkeypatch.setattr(sqlite_store, "_private_connection", slow_private)
    before = threading.active_count()
    started = time.perf_counter()
    store.close()
    elapsed = time.perf_counter() - started

    assert callers == [threading.current_thread()], "the TRUNCATE did not run inline"
    assert elapsed >= delay, f"close() returned in {elapsed:.2f}s, before the copy finished"
    assert threading.active_count() <= before, "close() left a thread behind"
    assert _wal_writer_threads() == []
    assert _wal_bytes(path) == 0
    assert store._ckpt_thread is None

    reopened = SQLiteStore(path)
    loaded = reopened.load("s1")
    assert loaded is not None and len(loaded.messages) == written
    reopened.close()


def test_close_keeps_the_wal_when_the_truncate_is_busy(tmp_path):
    """`busy_timeout=0`: a contended file costs one failed pragma, not a wait.

    Shutdown must not queue behind another connection. The TRUNCATE says so
    immediately, the WAL is left in place (durable, recovered on the next
    open), and `close()` still returns with no thread of ours on the file.
    """
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path, checkpoint_bytes=None)
    store.create_session(Session(id="s1", created_at=1))
    written = _fill_wal(store, path, 2 * 1024 * 1024)
    grown = _wal_bytes(path)

    # An open read transaction pins the WAL: TRUNCATE cannot reset it while
    # the reader's snapshot exists.
    reader = sqlite3.connect(str(path), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM messages").fetchone()

    started = time.perf_counter()
    store.close()
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, f"close() waited {elapsed:.1f}s on a pinned WAL"
    assert _wal_bytes(path) >= grown, "a busy TRUNCATE half-reset the sidecar"
    assert _wal_writer_threads() == []
    assert store._ckpt_thread is None

    reader.execute("COMMIT")
    reader.close()

    reopened = SQLiteStore(path)  # recovers the frames the close left behind
    loaded = reopened.load("s1")
    assert loaded is not None and len(loaded.messages) == written
    reopened.close()


# ------------------------------- 7. aclose: waits, off-loop, and leaves nothing
def test_aclose_joins_an_inflight_checkpoint_without_blocking_the_loop(tmp_path):
    """Shutdown while a checkpoint copies pages: wait for it, off-loop.

    There is no task to cancel any more. What must hold instead: the loop keeps
    ticking while `close()` joins the thread and truncates, the thread is gone
    (joined, not daemon-and-abandoned) when `aclose()` returns, and the data is
    intact.
    """
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path, checkpoint_bytes=64 * 1024, checkpoint_min_interval_s=0.0)
    store.create_session(Session(id="s1", created_at=1))
    for i in range(20):
        _append(store, "s1", i)

    entered = threading.Event()
    release = threading.Event()

    def slow_checkpoint() -> None:
        # Holds what the real one holds while it copies pages: this is the
        # in-flight checkpoint `close()` has to wait for.
        with store._ckpt_lock:
            entered.set()
            release.wait(30.0)

    store.checkpoint_restart = slow_checkpoint  # type: ignore[method-assign]

    async def scenario() -> tuple[float, int, threading.Thread]:
        loop = asyncio.get_running_loop()
        assert store.schedule_checkpoint() is True
        thread = store._ckpt_thread
        assert thread is not None and thread.is_alive()
        assert await asyncio.to_thread(entered.wait, 5.0), "checkpoint thread never started"
        threading.Timer(1.0, release.set).start()  # let the "copy" finish

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        tick_task = loop.create_task(ticker())
        started = time.perf_counter()
        try:
            await store.aclose()
        finally:
            elapsed = time.perf_counter() - started
            tick_task.cancel()
            release.set()  # never leave the test's own thread waiting
        return elapsed, ticks, thread

    elapsed, ticks, thread = asyncio.run(scenario())

    assert store._closed is True
    assert not thread.is_alive(), "aclose returned while the checkpoint thread still ran"
    assert store._ckpt_thread is None
    assert _wal_writer_threads() == []
    # It waited for the copy instead of walking away from it...
    assert elapsed >= 0.9, f"aclose returned in {elapsed:.2f}s, before the copy ended"
    # ...and the loop ran the whole time.
    assert ticks >= 20, f"the loop only ticked {ticks} times in {elapsed:.1f}s: it was blocked"

    reopened = SQLiteStore(path)
    loaded = reopened.load("s1")
    assert loaded is not None and len(loaded.messages) == 20  # waiting lost nothing
    reopened.close()


# ---------------------------- 8. a thread that cannot start is not fatal
def test_schedule_checkpoint_survives_a_thread_start_failure(tmp_path):
    """`Thread.start()` raising must not latch checkpointing off.

    The last append of a shutdown window is where this lands (out of threads,
    interpreter exiting): if `_ckpt_inflight` stayed True the store would never
    checkpoint again, and never log why.
    """
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path, checkpoint_bytes=64 * 1024, checkpoint_min_interval_s=0.0)
    store.create_session(Session(id="s1", created_at=1))
    for i in range(20):
        _append(store, "s1", i)
    assert _wal_bytes(path) > 64 * 1024

    real_thread = threading.Thread

    class NoStart(real_thread):  # type: ignore[misc, valid-type]
        def start(self) -> None:  # the failure under test
            raise RuntimeError("can't start new thread")

    async def scenario() -> tuple[bool, bool, object, bool]:
        threading.Thread = NoStart  # type: ignore[misc]
        try:
            first = store.schedule_checkpoint()
            latched = store._ckpt_inflight
            handle = store._ckpt_thread
        finally:
            threading.Thread = real_thread  # type: ignore[misc]
        second = store.schedule_checkpoint()
        await _settle_async(store)
        return first, latched, handle, second

    first, latched, handle, second = asyncio.run(scenario())
    assert first is False, "a failed start must report that nothing was scheduled"
    assert latched is False, "_ckpt_inflight stayed True: checkpointing is latched off"
    assert handle is None, "a thread that never started was left on the handle"
    assert second is True, "the store never recovered"
    assert store._ckpt_runs == 1
    store.close()


# ------------------------------------ 9. _ckpt_runs counts resets, not busses
def test_ckpt_runs_counts_only_a_checkpoint_that_reset_the_log(tmp_path):
    """`busy=1` means nothing was truncated; it must not count as a success.

    This is the "all green, WAL still growing" trap in counter form: a pinned
    reader makes RESTART return `(1, n, n)` without a reset.
    """
    path = tmp_path / "sessions.db"
    store = SQLiteStore(path, checkpoint_bytes=64 * 1024, checkpoint_min_interval_s=0.0)
    store.create_session(Session(id="s1", created_at=1))
    for i in range(20):
        _append(store, "s1", i)

    reader = sqlite3.connect(str(path), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM messages").fetchone()
    try:
        pinned = store.checkpoint_restart()
        assert pinned is not None and pinned[0] == 1, f"expected a busy run, got {pinned}"
        assert store._ckpt_runs == 0, "a busy checkpoint was counted as a success"
    finally:
        reader.execute("COMMIT")
        reader.close()

    free = store.checkpoint_restart()
    assert free is not None and free[0] == 0
    assert store._ckpt_runs == 1
    store.close()
