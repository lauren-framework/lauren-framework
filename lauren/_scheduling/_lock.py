"""The single-runner seam for ``exclusive=True`` jobs (private module).

``SchedulerLock`` is a two-method protocol so a shared backend (a Redis or
database ``Store`` lock) can plug in later; ``InMemorySchedulerLock`` is the
per-process default.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ._clock import Clock, SystemClock


@runtime_checkable
class SchedulerLock(Protocol):
    """Claims one fire of an ``exclusive=True`` job, cluster-wide when the backend is shared.

    The scheduler calls ``acquire(f"lauren:job:{name}:{fire_time_iso}", ttl=...)``
    before an exclusive run and skips the run (``JobSkipped(reason="locked")``)
    when it returns ``False``. The key names one fire time, so the claim is
    held until ``ttl`` runs out rather than released after the run: a worker
    that wakes late must not run the same fire again. ``ttl`` is the job's
    ``timeout``, else ``min(period, 3600)`` seconds, so a crashed holder blocks
    nothing for long. ``release`` is called only when a claimed run never
    started (the scheduler was stopping).
    """

    async def acquire(self, key: str, *, ttl: float) -> bool:
        """Claim ``key`` for ``ttl`` seconds; ``False`` when someone else holds it."""
        ...

    async def release(self, key: str) -> None:
        """Drop the claim on ``key`` (no-op when it is not held)."""
        ...


class InMemorySchedulerLock:
    """A :class:`SchedulerLock` held in this process only (the default).

    Exclusivity is per process: with several workers, each runs every
    exclusive job. Share a store-backed lock across workers, or run
    scheduled work in one dedicated process. ``clock`` measures the TTL
    (the app's scheduler clock when the lock is created by ``ScheduleModule``).
    """

    __slots__ = ("_clock", "_held")

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._held: dict[str, float] = {}

    async def acquire(self, key: str, *, ttl: float) -> bool:
        now = self._clock.monotonic()
        for stale in [k for k, expires in self._held.items() if expires <= now]:
            del self._held[stale]
        if key in self._held:
            return False
        self._held[key] = now + max(0.0, ttl)
        return True

    async def release(self, key: str) -> None:
        self._held.pop(key, None)

    def __repr__(self) -> str:
        return f"<InMemorySchedulerLock {len(self._held)} claim(s)>"


__all__ = ["InMemorySchedulerLock", "SchedulerLock"]
