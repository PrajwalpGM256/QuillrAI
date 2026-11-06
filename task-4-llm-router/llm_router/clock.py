"""Time source for the sliding window.

Wall-clock seconds (time.time), not a monotonic clock: two router processes share one SQLite file,
and rows must stay meaningful across processes and restarts. A monotonic clock is per-process. The
cost is that an NTP step backwards briefly lengthens the window, which over-counts (fails safe).

FakeClock lets tests cross the 60 s window boundary without sleeping.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    """A settable clock, safe to read from the SQLite worker thread."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds
