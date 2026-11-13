"""Retention: drop day tables older than the window, once a day at 03:00 UTC."""

from __future__ import annotations

import threading
import time
from typing import Callable

from .writer import AuditWriter

__all__ = ["RetentionJob"]


class RetentionJob:
    def __init__(
        self,
        writer: AuditWriter,
        *,
        keep_days: int = 30,
        hour_utc: int = 3,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._writer = writer
        self.keep_days = keep_days
        self.hour_utc = hour_utc
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[str]:
        return self._writer.prune(self._clock(), self.keep_days)

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._loop, name="vade-audit-retention", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            day = time.strftime("%Y%m%d", time.gmtime(now))
            hour = int(time.strftime("%H", time.gmtime(now)))
            if hour >= self.hour_utc:
                marker = getattr(self, "_last_run", None)
                if marker != day:
                    self._last_run = day
                    self.run_once()
            self._stop.wait(300)
