"""`flock data/core.lock` — one core process per `data/` directory.

design.md:899 and 31-C5a §T4 both make this a hard rule, not advice: two cores
on one data directory each rotate `setup.token` on start (D59) and each keep a
private `DeviceStore`/`SessionStore` copy, so they silently overwrite each
other's state. The lock is acquired **before any side effect** in `_serve` and
held for the life of the process (the open file description keeps it).

`flock` locks are advisory and per open-file-description, so the CLI can share
this helper to answer "is a core running against this data dir?" with the same
truth the core itself uses — rather than a PID file nobody updates.

Windows has no `fcntl`; `msvcrt.locking` provides the same exclusion (non
blocking, held by the OS for the life of the handle, visible across processes).
Same rule, same failure mode (`OSError` when busy), platform-appropriate
mechanism.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["acquire_instance_lock", "release_instance_lock", "lock_path"]

_IS_WINDOWS = os.name == "nt"  # matches the convention in the test suite


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
    fd = os.open(lock_path(directory), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if _IS_WINDOWS:
            import msvcrt

            # msvcrt.locking needs at least one byte in the file to lock.
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

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
        if _IS_WINDOWS:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
