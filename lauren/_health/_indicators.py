"""Built-in health indicators (stdlib only): event-loop lag, disk space, process memory."""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import sys
from collections import deque
from collections.abc import Callable

from ._types import HealthResult

#: Seconds between two event-loop lag samples, and the window the p99 covers.
LAG_INTERVAL = 0.5
LAG_WINDOW = 10.0


class LoopLagMonitor:
    """Measure event-loop scheduling lag with a sampler task; report its p99.

    The sampler sleeps :data:`LAG_INTERVAL` seconds and records how much
    later than requested it woke up. The status is ``DOWN`` when the p99 over
    the last :data:`LAG_WINDOW` seconds exceeds ``threshold_ms`` and
    ``DEGRADED`` above half of it. The sampler belongs to one event loop; it
    is (re)started on the running loop with :meth:`ensure_running` and
    stopped — samples cleared — with :meth:`stop`.
    """

    __slots__ = ("threshold_ms", "_interval", "_samples", "_task")

    def __init__(
        self, threshold_ms: float, *, interval: float = LAG_INTERVAL, window: float = LAG_WINDOW
    ) -> None:
        self.threshold_ms = float(threshold_ms)
        self._interval = interval
        self._samples: deque[float] = deque(maxlen=max(1, round(window / interval)))
        self._task: asyncio.Task[None] | None = None

    def add_sample(self, lag_ms: float) -> None:
        """Record one lag measurement, in milliseconds."""
        self._samples.append(lag_ms)

    def p99(self) -> float | None:
        """The 99th-percentile lag of the window, or ``None`` without samples."""
        if not self._samples:
            return None
        ordered = sorted(self._samples)
        return ordered[max(0, math.ceil(0.99 * len(ordered)) - 1)]

    def result(self) -> HealthResult:
        """The indicator's status for the current window."""
        lag = self.p99()
        details = {"lag_ms": None if lag is None else round(lag, 3), "threshold_ms": self.threshold_ms}
        if lag is not None and lag > self.threshold_ms:
            return HealthResult.down(**details)
        if lag is not None and lag > self.threshold_ms / 2:
            return HealthResult.degraded(**details)
        return HealthResult.up(**details)

    @property
    def running(self) -> bool:
        """``True`` while a sampler task is alive."""
        return self._task is not None and not self._task.done()

    def ensure_running(self) -> None:
        """Start the sampler on the running loop unless it already runs there."""
        loop = asyncio.get_running_loop()
        task = self._task
        if task is not None and not task.done() and task.get_loop() is loop:
            return
        self._samples.clear()  # samples taken on another loop say nothing about this one
        self._task = loop.create_task(self._sample(), name="lauren-health:event-loop")

    def stop(self) -> None:
        """Cancel the sampler (safe from any thread) and forget the samples."""
        task, self._task = self._task, None
        self._samples.clear()
        if task is None or task.done():
            return
        loop = task.get_loop()
        if loop.is_closed():
            return
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            task.cancel()
        else:
            loop.call_soon_threadsafe(task.cancel)

    async def _sample(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await asyncio.sleep(self._interval)
            self.add_sample(max(0.0, (loop.time() - started - self._interval) * 1000.0))


def disk_probe(path: str, min_free: int) -> Callable[[], HealthResult]:
    """A check reading ``shutil.disk_usage(path)``: down when fewer than ``min_free`` bytes are free."""

    def probe() -> HealthResult:
        usage = shutil.disk_usage(path)
        details = {"path": path, "free_bytes": usage.free, "min_free_bytes": min_free}
        return HealthResult.down(**details) if usage.free < min_free else HealthResult.up(**details)

    return probe


def memory_supported() -> bool:
    """Whether :func:`read_rss` works on this platform (Linux and other POSIX systems)."""
    if sys.platform.startswith("linux"):
        return True
    try:
        import resource  # noqa: F401
    except ImportError:  # Windows
        return False
    return True


def read_rss() -> tuple[int, str]:
    """``(bytes, kind)``: the current RSS on Linux (``"current"``), else the peak RSS (``"peak"``).

    Linux reads ``/proc/self/statm``; other POSIX systems fall back to
    ``resource.getrusage(RUSAGE_SELF).ru_maxrss``, which is the *peak*
    resident size (bytes on macOS, kilobytes elsewhere).
    """
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", encoding="utf-8") as fh:
                pages = int(fh.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE"), "current"
        except (OSError, ValueError, IndexError):  # pragma: no cover - /proc unavailable (sandboxes)
            pass
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (peak if sys.platform == "darwin" else peak * 1024), "peak"


def memory_probe(
    max_rss_bytes: int, reader: Callable[[], tuple[int, str]] = read_rss
) -> Callable[[], HealthResult]:
    """A check comparing the process RSS (see :func:`read_rss`) with ``max_rss_bytes``."""

    def probe() -> HealthResult:
        rss, kind = reader()
        details = {"rss_bytes": rss, "max_rss_bytes": max_rss_bytes, "measure": kind}
        return HealthResult.down(**details) if rss > max_rss_bytes else HealthResult.up(**details)

    return probe


__all__ = [
    "LAG_INTERVAL",
    "LAG_WINDOW",
    "LoopLagMonitor",
    "disk_probe",
    "memory_probe",
    "memory_supported",
    "read_rss",
]
