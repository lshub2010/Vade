"""`flock data/core.lock` — one core process per `data/` directory.

design.md:899 and 31-C5a §T4 both make this a hard rule, not advice: two cores
on one data directory each rotate `setup.token` on start (D59) and each keep a
private `DeviceStore`/`SessionStore` copy, so they silently overwrite each
other's state. The lock is acquired **before any side effect** in `_serve` and
held for the life of the process (the open file description keeps it).

`flock` locks are advisory and per open-file-description, so the CLI can share
this helper to answer "is a core running against this data dir?" with the same
truth the core itself uses — rather than a PID file nobody updates.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

__all__ = ["acquire_instance_lock", "release_instance_lock", "lock_path"]


def lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / "core.lock"


def acquire_instance_lock(data_dir: Path) -> int:
    """Try to become the only core for `data_dir`; return the held fd.

    Raises `OSError` when another process holds the lock. The caller must keep
    the returned descriptor referenced until exit — closing it releases the
    lock.
    """
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(directory), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    # Diagnostics only: `cat data/core.lock` answers "who is holding this?".
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
    except OSError:
        pass
    return fd


def release_instance_lock(fd: int | None) -> None:
    """Release explicitly (tests, and the CLI's probe path)."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
