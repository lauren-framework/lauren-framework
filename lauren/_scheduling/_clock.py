"""Clocks for the scheduler and hosted services (private).

Everything the scheduler and the hosted-service supervisor *schedule* reads
time through a :class:`Clock`: fire times, jitter, misfire detection, per-run
timeouts, restart backoff and :meth:`ServiceContext.sleep
<lauren.scheduling.ServiceContext.sleep>`. :class:`SystemClock` is the real
one; :class:`ManualClock` only moves when a test calls :meth:`ManualClock.advance`,
so scheduling tests never wait on wall-clock time.

Lifecycle bounds (``start_timeout``, ``stop_timeout``, the job drain at
shutdown) are deliberately measured in real time: a manual clock can then
never make ``startup()`` or ``shutdown()`` hang.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

#: Default start instant of a :class:`ManualClock` (a fixed, documented value
#: keeps tests deterministic).
MANUAL_CLOCK_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# How long ManualClock.advance lets the event loop run after each wake-up.
_SETTLE_MAX_ROUNDS = 10_000
_SETTLE_FALLBACK_ROUNDS = 64
#: Real seconds ManualClock.advance waits for sync jobs running on threads.
_THREAD_WAIT = 5.0


@runtime_checkable
class Clock(Protocol):
    """The time source of a scheduler: a monotonic reading, wall time and a sleep."""

    def monotonic(self) -> float:
        """Seconds on a monotonic scale (intervals, timeouts, backoff)."""
        ...

    def now(self) -> datetime:
        """The current wall-clock time as an aware UTC datetime (cron, ``scheduled_at``)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the caller for ``seconds`` on this clock's scale."""
        ...


class SystemClock:
    """The real clock: :func:`time.monotonic`, the system wall clock and :func:`asyncio.sleep`."""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    def __repr__(self) -> str:
        return "SystemClock()"


class ManualClock:
    """A clock that only moves when :meth:`advance` is awaited (for tests).

    ``start`` is the initial wall time (an aware datetime; a naive one is read
    as UTC). It defaults to ``2000-01-01T00:00:00Z``; the monotonic reading
    starts at ``0.0``. Wall time and monotonic time move together.

    :meth:`advance` wakes every sleeper whose deadline falls inside the window,
    in deadline order, and lets the event loop run after each wake-up, so
    every job due in the window fires (and a job that does not block
    finishes) before it returns. Sync jobs run on worker threads; ``advance``
    waits for those it started (up to five seconds of real time each round).
    :meth:`jump` moves time without waking anything on the way (a suspended
    process), to test misfire handling. Use one clock per app and drive it
    from the loop the app runs on.
    """

    __slots__ = ("_start", "_elapsed", "_sleepers", "_sequence", "_offloaded")

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = MANUAL_CLOCK_EPOCH
        elif start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._start = start.astimezone(timezone.utc)
        self._elapsed = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._sequence = itertools.count()
        self._offloaded: set[asyncio.Future[object]] = set()

    def monotonic(self) -> float:
        return self._elapsed

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    @property
    def sleepers(self) -> int:
        """How many coroutines are currently sleeping on this clock."""
        return sum(1 for _, _, future in self._sleepers if not future.done())

    async def sleep(self, seconds: float) -> None:
        deadline = self._elapsed + seconds
        if seconds <= 0 or deadline <= self._elapsed:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (deadline, next(self._sequence), future))
        await future

    def _track_offloaded(self, future: asyncio.Future[object]) -> None:
        """Remember a thread-offloaded job call so :meth:`advance` waits for it."""
        self._offloaded.add(future)
        future.add_done_callback(self._offloaded.discard)

    async def _settle(self) -> None:
        """Let the loop run until idle, including sync jobs on worker threads."""
        await _settle()
        pending = {f for f in self._offloaded if not f.done()}
        while pending:
            _, still = await asyncio.wait(pending, timeout=_THREAD_WAIT)
            await _settle()
            if still:
                return  # a sync job that blocks on something the test controls
            pending = {f for f in self._offloaded if not f.done()}

    async def advance(
        self,
        seconds: float = 0.0,
        *,
        minutes: float = 0.0,
        hours: float = 0.0,
        days: float = 0.0,
    ) -> None:
        """Move time forward, waking (in order) every sleeper due in the window.

        ``advance()`` with no argument just lets pending work run. Raises
        ``ValueError`` for a negative step: the clock never goes backwards.
        """
        step = seconds + minutes * 60.0 + hours * 3600.0 + days * 86400.0
        if step < 0:
            raise ValueError(f"ManualClock.advance() cannot go backwards (got {step} s)")
        target = self._elapsed + step
        await self._settle()
        while True:
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)  # cancelled sleepers
            if not self._sleepers or self._sleepers[0][0] > target:
                break
            self._elapsed = max(self._elapsed, self._sleepers[0][0])
            while self._sleepers and self._sleepers[0][0] <= self._elapsed:
                _, _, future = heapq.heappop(self._sleepers)
                if not future.done():
                    future.set_result(None)
            await self._settle()
        self._elapsed = max(self._elapsed, target)
        await self._settle()

    async def jump(
        self,
        seconds: float = 0.0,
        *,
        minutes: float = 0.0,
        hours: float = 0.0,
        days: float = 0.0,
    ) -> None:
        """Move time forward in one step, as if the process had been suspended.

        Unlike :meth:`advance`, nothing wakes on the way: every sleeper due in
        the window wakes together, *after* the jump — the way to test misfire
        handling. Raises ``ValueError`` for a negative step.
        """
        step = seconds + minutes * 60.0 + hours * 3600.0 + days * 86400.0
        if step < 0:
            raise ValueError(f"ManualClock.jump() cannot go backwards (got {step} s)")
        await self._settle()
        self._elapsed += step
        await self.advance()

    def __repr__(self) -> str:
        return f"ManualClock(now={self.now().isoformat()}, monotonic={self._elapsed})"


async def _settle() -> None:
    """Let the event loop run until nothing else is ready (bounded).

    On the default asyncio loop the ready queue is inspected, so this returns
    as soon as every other task is blocked; other loops get a fixed number of
    rounds.
    """
    loop = asyncio.get_running_loop()
    ready = getattr(loop, "_ready", None)
    for rounds in range(_SETTLE_MAX_ROUNDS):
        await asyncio.sleep(0)
        if ready is None:
            if rounds >= _SETTLE_FALLBACK_ROUNDS:
                return
        elif not ready:
            return


async def wait_event_or_sleep(clock: Clock, event: asyncio.Event, seconds: float | None) -> bool:
    """Wait until ``event`` is set or ``seconds`` pass on ``clock``.

    Returns ``True`` when the event was set (or already was), ``False`` when
    the sleep ran out. ``seconds=None`` waits for the event only.
    """
    if event.is_set():
        return True
    waiter = asyncio.ensure_future(event.wait())
    if seconds is None:
        try:
            await waiter
        finally:
            if not waiter.done():
                waiter.cancel()
        return True
    sleeper = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pending in (waiter, sleeper):
            if not pending.done():
                pending.cancel()
    return event.is_set()


__all__ = ["MANUAL_CLOCK_EPOCH", "Clock", "ManualClock", "SystemClock", "wait_event_or_sleep"]
