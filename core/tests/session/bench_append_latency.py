"""Append-latency probe for the SQLite session store (WAL checkpoint work).

Not a pytest test (`bench_*` is not collected): this is the reproducible probe
behind the numbers in the report. It drives the *real* write path — real
SQLite, real WAL, real `SessionLog` — and prints one JSON line per run.

    python core/tests/session/bench_append_latency.py --mode store   --events 1000
    python core/tests/session/bench_append_latency.py --mode manager --events 1000
    python core/tests/session/bench_append_latency.py --mode store --checkpoint-bytes 1048576

`--mode store` isolates the store (append + fetch, no asyncio), so the before/
after delta is the checkpoint policy alone. `--mode manager` goes through
`SessionLog.emit`, which is where the background checkpoint is scheduled; its
numbers therefore include the trigger's own cost.

Latency is measured with `perf_counter` around one append only — the WAL
sampling, the settle wait and the bookkeeping stay outside the timed region.
Needless to say the host matters: `--settle` exists because a checkpoint of a
slow disk can still be running when the last append lands, and the "final WAL"
number is only meaningful once it finished.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from vade.session.models import Event, Message, Session
from vade.session.sqlite_store import SQLiteStore


def _make_store(path: Path, checkpoint_bytes: int | None) -> SQLiteStore:
    """Pass `checkpoint_bytes` only when the store accepts it (before/after)."""
    params = inspect.signature(SQLiteStore.__init__).parameters
    if checkpoint_bytes is not None and "checkpoint_bytes" in params:
        return SQLiteStore(path, checkpoint_bytes=checkpoint_bytes)
    return SQLiteStore(path)


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    # nearest-rank: p95 of 1000 samples = the 950th, no interpolation
    idx = min(len(ordered) - 1, max(0, int(round(q * len(ordered))) - 1))
    return ordered[idx]


def _wal_bytes(path: Path) -> int:
    try:
        return os.path.getsize(str(path) + "-wal")
    except OSError:
        return 0


def _payload(size: int) -> str:
    return ("x" * size) if size else ""


def _runs(store: SQLiteStore) -> int:
    return int(getattr(store, "_ckpt_runs", 0))


def _result(latencies: list[float], peak_wal: int, final_wal: int, path: Path,
            checkpoints: int) -> dict:
    return {
        "n": len(latencies),
        "p50_ms": round(_pct(latencies, 0.50), 3),
        "p95_ms": round(_pct(latencies, 0.95), 3),
        "p99_ms": round(_pct(latencies, 0.99), 3),
        "max_ms": round(max(latencies), 3),
        "mean_ms": round(sum(latencies) / len(latencies), 3),
        "wal_peak_kib": round(peak_wal / 1024, 1),
        "wal_final_kib": round(final_wal / 1024, 1),
        "db_kib": round(os.path.getsize(path) / 1024, 1),
        "checkpoints": checkpoints,
    }


def run_store(path: Path, events: int, body: str, checkpoint_bytes: int | None,
              settle: float) -> dict:
    store = _make_store(path, checkpoint_bytes)
    store.create_session(Session(id="bench", created_at=1))
    latencies: list[float] = []
    peak = 0
    for i in range(events):
        seq = store.next_seq("bench")
        message = Message(id=f"m{i}", session_id="bench", role="assistant",
                          content=body, seq=seq)
        event = Event(session_id="bench", seq=seq, type="turn.delta",
                      payload={"turn_id": "t1", "text": body})
        start = time.perf_counter()
        store.append("bench", [message], [event])
        latencies.append((time.perf_counter() - start) * 1000.0)
        if i % 10 == 0:
            peak = max(peak, _wal_bytes(path))
    peak = max(peak, _wal_bytes(path))
    _settle(store, settle)
    # A completed checkpoint only gives the space back when the *writer*
    # restarts the log (`journal_size_limit=0`), so one untimed append after
    # the settle is what makes "final WAL" mean steady state, not high-water.
    _append_tail(store)
    _settle(store, settle)
    final = _wal_bytes(path)
    runs = _runs(store)
    store.close()
    return _result(latencies, peak, final, path, runs)


def _append_tail(store: SQLiteStore) -> None:
    """One untimed append, outside the latency sample (see `run_*`)."""
    try:
        store.create_session(Session(id="bench-tail", created_at=1))
    except Exception:  # noqa: BLE001 - already there on a second call
        pass
    seq = store.next_seq("bench-tail")
    store.append(
        "bench-tail",
        [Message(id=f"tail{seq}", session_id="bench-tail", role="assistant",
                 content="tail", seq=seq)],
        [Event(session_id="bench-tail", seq=seq, type="turn.delta",
               payload={"turn_id": "t1", "text": "tail"})],
    )


async def run_manager(path: Path, events: int, body: str, checkpoint_bytes: int | None,
                      settle: float) -> dict:
    from vade.session.manager import SessionLog
    from vade.session.replay import ReplayBuffer

    store = _make_store(path, checkpoint_bytes)
    log = SessionLog(store, replay=ReplayBuffer(maxlen=events + 100))
    session = log.create_session()
    latencies: list[float] = []
    peak = 0
    for i in range(events):
        message = Message(id=f"m{i}", session_id=session.id, role="assistant",
                          content=body, seq=0)
        start = time.perf_counter()
        await log.emit(session.id, "turn.delta",
                       {"turn_id": "t1", "text": body}, messages=[message])
        latencies.append((time.perf_counter() - start) * 1000.0)
        if i % 10 == 0:
            peak = max(peak, _wal_bytes(path))
        if i % 100 == 99:
            await asyncio.sleep(0)  # run scheduled checkpoint callbacks
    peak = max(peak, _wal_bytes(path))
    await _settle_async(store, settle)
    _append_tail(store)  # untimed; lets the writer restart the log
    await _settle_async(store, settle)
    final = _wal_bytes(path)
    runs = _runs(store)
    await log.close()
    return _result(latencies, peak, final, path, runs)


def _settle(store: SQLiteStore, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and getattr(store, "_ckpt_inflight", False):
        time.sleep(0.02)


async def _settle_async(store: SQLiteStore, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and getattr(store, "_ckpt_inflight", False):
        await asyncio.sleep(0.02)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("store", "manager"), default="store")
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--body", type=int, default=256, help="message text length")
    parser.add_argument("--checkpoint-bytes", type=int, default=None)
    parser.add_argument("--settle", type=float, default=60.0,
                        help="seconds to wait for an in-flight checkpoint")
    args = parser.parse_args()

    body = _payload(args.body)
    runs: list[dict] = []
    for _ in range(args.repeat):
        root = Path(tempfile.mkdtemp(prefix="vade-bench-"))
        path = root / "sessions.db"
        try:
            if args.mode == "store":
                runs.append(run_store(path, args.events, body, args.checkpoint_bytes, args.settle))
            else:
                runs.append(asyncio.run(
                    run_manager(path, args.events, body, args.checkpoint_bytes, args.settle)))
        finally:
            shutil.rmtree(root, ignore_errors=True)
    print(json.dumps({"mode": args.mode, "events": args.events, "runs": runs}, ensure_ascii=False))


if __name__ == "__main__":
    main()
