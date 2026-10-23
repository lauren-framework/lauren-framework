"""The per-app scheduler and :class:`SchedulerRegistry` (private module).

One task per app holds the job table. It sleeps on the app's
:class:`~lauren.scheduling.Clock` until the earliest fire (or until the
registry wakes it), dispatches each due run as a tracked task and enforces
the overlap, ``max_instances``, ``timeout``, lock, jitter and misfire
policies. One task rather than one per job keeps the overhead flat in the
number of jobs and makes a :class:`~lauren.scheduling.ManualClock` trivial.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Literal, cast

from ..exceptions import DecoratorUsageError, DuplicateBindingError, LifecycleViolationError
from ..logging import Logger
from ..signals import JobCompleted, JobFailed, JobSkipped, JobStarted, SignalBus
from ._clock import Clock, wait_event_or_sleep
from ._context import JobContext, bound_logger
from ._cron import CronExpression, resolve_timezone
from ._decorators import ScheduleMeta, _choice, _common, _number
from ._lock import SchedulerLock
from ._threads import run_sync_abandonable

if TYPE_CHECKING:
    from .._discovery import DiscoveredMethod
    from ..context import OperationScope
    from ._supervisor import HostedServiceInfo, Supervisor

#: Re-read the wall clock at least this often, so cron jobs follow clock jumps.
_MAX_SLEEP = 60.0
#: Real seconds a cancelled run (or the cancelled loop task) gets to unwind.
_CANCEL_GRACE = 1.0
_MAX_LOCK_TTL = 3600.0
_EPSILON = 1e-9
_CATCH_UP_LIMIT = 10_000
_UTC = timezone.utc

RunStatus = Literal["completed", "failed", "skipped"]


class _RunTimeout(TimeoutError):
    """A run exceeded its ``timeout`` (distinct from a ``TimeoutError`` the job raised)."""


@dataclass(frozen=True, slots=True)
class JobInfo:
    """A snapshot of one job, returned by :meth:`SchedulerRegistry.jobs`.

    ``kind`` is ``"interval"``, ``"cron"`` or ``"once"``; ``schedule`` is a
    readable description. ``next_run_at`` is ``None`` while the scheduler is
    not firing (disabled, before startup, during shutdown) or the job is
    paused, disabled or done. ``last_status`` is ``"completed"``,
    ``"failed"`` or ``"cancelled"`` (``None`` before the first run).
    Statistics are bounded: the last outcome plus counters, no history.
    """

    name: str
    kind: str
    schedule: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    last_duration_s: float | None
    running: int
    paused: bool
    enabled: bool
    dynamic: bool
    runs: int
    failures: int
    skips: int

    def as_dict(self) -> dict[str, Any]:
        """A JSON-friendly ``dict`` (datetimes as ISO 8601 strings)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "schedule": self.schedule,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "last_duration_s": self.last_duration_s,
            "running": self.running,
            "paused": self.paused,
            "enabled": self.enabled,
            "dynamic": self.dynamic,
            "runs": self.runs,
            "failures": self.failures,
            "skips": self.skips,
        }


@dataclass(frozen=True, slots=True)
class JobRun:
    """The outcome of :meth:`SchedulerRegistry.trigger`.

    ``status`` is ``"completed"``, ``"failed"`` (``error`` holds the
    exception; a timeout is a ``TimeoutError``) or ``"skipped"`` (``reason``
    is ``"overlap"``, ``"locked"`` or ``"disabled"``, or ``"stopping"`` when
    shutdown began right after an ``exclusive`` run won its lock).
    """

    name: str
    status: RunStatus
    scheduled_at: datetime
    duration_s: float = 0.0
    error: BaseException | None = None
    reason: str | None = None


class _Queued:
    """The single run queued behind a busy ``overlap="queue"`` job."""

    __slots__ = ("scheduled_at", "waiters")

    def __init__(self, scheduled_at: datetime) -> None:
        self.scheduled_at = scheduled_at
        self.waiters: list[asyncio.Future[JobRun]] = []

    def fail(self, error: BaseException) -> None:
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_exception(error)


class Job:
    """One schedule: static options, the call target and its running state."""

    __slots__ = (
        "name",
        "meta",
        "tz",
        "target",
        "entry",
        "instance",
        "fn",
        "fn_takes_ctx",
        "fn_is_coroutine",
        "dynamic",
        "cron",
        "period",
        "misfire_grace",
        "nominal",
        "grid_anchor",
        "jitter_offset",
        "running",
        "queued",
        "paused",
        "fired",
        "last_run_at",
        "last_status",
        "last_error",
        "last_duration_s",
        "runs",
        "failures",
        "skips",
    )

    def __init__(
        self,
        *,
        name: str,
        meta: ScheduleMeta,
        tz: tzinfo,
        target: str,
        entry: DiscoveredMethod | None = None,
        fn: Callable[..., Any] | None = None,
        fn_takes_ctx: bool = False,
        fn_is_coroutine: bool = False,
    ) -> None:
        self.name = name
        self.meta = meta
        self.tz = tz
        self.target = target
        self.entry = entry
        self.instance: Any = None
        self.fn = fn
        self.fn_takes_ctx = fn_takes_ctx
        self.fn_is_coroutine = fn_is_coroutine
        self.dynamic = entry is None
        self.cron: CronExpression | None = meta.cron.with_tz(tz) if meta.cron is not None else None
        self.period: float | None = meta.seconds if meta.kind == "interval" else None
        self.misfire_grace = (
            meta.misfire_grace if meta.misfire_grace is not None else min(60.0, self.period or 60.0)
        )
        self.nominal: float | datetime | None = None
        self.grid_anchor = 0.0
        self.jitter_offset = 0.0
        self.running: set[asyncio.Task[JobRun]] = set()
        self.queued: _Queued | None = None
        self.paused = False
        self.fired = False
        self.last_run_at: datetime | None = None
        self.last_status: str | None = None
        self.last_error: str | None = None
        self.last_duration_s: float | None = None
        self.runs = 0
        self.failures = 0
        self.skips = 0

    @property
    def lock_ttl(self) -> float:
        if self.meta.timeout is not None:
            return self.meta.timeout
        return min(self.period or _MAX_LOCK_TTL, _MAX_LOCK_TTL)

    def record(self, status: str, fired_at: datetime, duration: float, error: str | None) -> None:
        self.last_run_at = fired_at
        self.last_status = status
        self.last_error = error
        self.last_duration_s = duration
        self.runs += 1
        if status != "completed":
            self.failures += 1


def _is_async_callable(fn: Any) -> bool:
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(fn, "__call__", None)  # noqa: B004 - callable objects with an async __call__
    return inspect.iscoroutinefunction(call)


def _dynamic_signature(fn: Any, where: str) -> tuple[bool, bool]:
    """``(takes_ctx, is_coroutine)`` for a dynamically added job function."""
    if not callable(fn):
        raise DecoratorUsageError(
            f"{where}: fn must be callable, got {fn!r}", detail={"helper": where, "reason": "not_callable"}
        )
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError) as exc:
        raise DecoratorUsageError(
            f"{where}: cannot read the signature of {fn!r}: {exc}",
            detail={"helper": where, "reason": "no_signature"},
        ) from exc
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    required = [p for p in params if p.kind in positional and p.default is inspect.Parameter.empty]
    required_kw = [
        p for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    ]
    if len(required) > 1 or required_kw:
        raise DecoratorUsageError(
            f"{where}: fn must take no argument or one (the JobContext); dynamically added jobs cannot "
            "request dependencies",
            detail={"helper": where, "reason": "invalid_signature"},
        )
    takes_ctx = bool(required) or any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
    return takes_ctx, _is_async_callable(fn)


class Scheduler:
    """The scheduler of one app: job table, firing loop and run bookkeeping."""

    def __init__(
        self,
        *,
        clock: Clock,
        lock: SchedulerLock,
        timezone: tzinfo,
        enabled: bool,
        logger: Logger,
        signals: SignalBus,
        scope_factory: Callable[..., OperationScope],
        resolve_instance: Callable[[Any], Any],
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._lock = lock
        self._tz = timezone
        self._enabled = enabled
        self._logger = logger
        self._signals = signals
        self._scope_factory = scope_factory
        self._resolve_instance = resolve_instance
        self._rng = rng or random.Random()
        self._jobs: dict[str, Job] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._armed = False
        self._firing = False
        self._stopping = False
        self._runs: set[asyncio.Task[JobRun]] = set()
        self._contexts: dict[asyncio.Task[Any], JobContext] = {}

    # -- Properties -------------------------------------------------------

    @property
    def lock(self) -> SchedulerLock:
        return self._lock

    @property
    def firing(self) -> bool:
        """``True`` between ``begin()`` and ``stop_firing()`` when firing is enabled."""
        return self._firing

    def job_table(self) -> tuple[Job, ...]:
        return tuple(self._jobs.values())

    def _now(self) -> datetime:
        # Wall time is kept in UTC internally: aware datetimes sharing a
        # tzinfo compare by wall time, which breaks across DST changes.
        return self._clock.now().astimezone(_UTC)

    # -- Registration -----------------------------------------------------

    def add(self, job: Job) -> None:
        existing = self._jobs.get(job.name)
        if existing is not None:
            raise DuplicateBindingError(
                f"two scheduled jobs are named {job.name!r} ({existing.target} and {job.target}); "
                "pass name= to tell them apart",
                detail={
                    "name": job.name,
                    "targets": [existing.target, job.target],
                    "reason": "duplicate_job_name",
                },
            )
        self._jobs[job.name] = job
        if self._firing:
            self._schedule_first(job, self._clock.monotonic(), self._now(), anchor=self._clock.monotonic())
            self._notify()

    def get(self, name: str) -> Job:
        try:
            return self._jobs[name]
        except KeyError:
            known = ", ".join(sorted(self._jobs)) or "none"
            raise KeyError(f"no scheduled job named {name!r} (known: {known})") from None

    # -- Lifecycle --------------------------------------------------------

    async def arm(self) -> None:
        """Resolve each discovered job's singleton (startup, after ``@post_construct``)."""
        for job in self._jobs.values():
            if job.entry is not None and job.instance is None:
                job.instance = await self._resolve_instance(job.entry.provider)
        self._armed = True

    def begin(self) -> None:
        """Start firing: compute first fire times from now (``READY``) and start the loop task."""
        if self._stopping or not self._enabled:
            return
        now_mono, now_wall = self._clock.monotonic(), self._now()
        for job in self._jobs.values():
            self._schedule_first(job, now_mono, now_wall, anchor=now_mono)
        self._firing = True
        self._task = asyncio.get_running_loop().create_task(self._main(), name="lauren-scheduler")
        for job in self._jobs.values():
            next_run = self._info(job).next_run_at
            self._logger.verbose(
                f"job {job.name!r} scheduled: {job.meta.describe(self._tz)}"
                + (f"; next run {next_run.isoformat()}" if next_run else ""),
                context="Scheduler",
                job=job.name,
            )

    def stop_firing(self) -> None:
        """Stop starting runs (shutdown step 1); in-flight runs continue."""
        self._stopping = True
        self._firing = False
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    async def drain(self, timeout: float) -> None:
        """Wait up to ``timeout`` real seconds for in-flight runs, then cancel the rest."""
        self.stop_firing()
        task = self._task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=_CANCEL_GRACE)
        for job in self._jobs.values():
            if job.queued is not None:
                job.queued.fail(
                    LifecycleViolationError(f"the app shut down before job {job.name!r}'s queued run")
                )
                job.queued = None
        pending = {run for run in self._runs if not run.done()}
        if not pending:
            return
        self._logger.log(
            f"Waiting up to {timeout:g}s for {len(pending)} scheduled run(s)",
            context="Shutdown",
            runs=len(pending),
        )
        if timeout > 0:
            _, pending = await asyncio.wait(pending, timeout=timeout)
        if pending:
            self._logger.warn(
                f"Cancelling {len(pending)} scheduled run(s) still going after {timeout:g}s",
                context="Shutdown",
                runs=len(pending),
            )
            for run in pending:
                ctx = self._contexts.get(run)
                if ctx is not None:
                    ctx._cancelled = True  # visible to the job's own cleanup
                run.cancel()
            await asyncio.wait(pending, timeout=_CANCEL_GRACE)

    # -- The loop ---------------------------------------------------------

    async def _main(self) -> None:
        clock = self._clock
        while not self._stopping:
            now_mono, now_wall = clock.monotonic(), self._now()
            due: list[tuple[float, Job]] = []
            next_delay: float | None = None
            for job in self._jobs.values():
                if job.nominal is None or job.paused or not job.meta.enabled:
                    continue
                deadline = self._deadline(job, now_mono, now_wall)
                if deadline <= now_mono + _EPSILON:
                    due.append((deadline, job))
                else:
                    remaining = deadline - now_mono
                    next_delay = remaining if next_delay is None else min(next_delay, remaining)
            if due:
                due.sort(key=lambda pair: pair[0])
                for _, job in due:
                    if self._stopping:
                        return
                    if self._jobs.get(job.name) is job and job.nominal is not None and not job.paused:
                        await self._fire(job, now_mono, now_wall)
                await asyncio.sleep(0)
                continue
            delay = None if next_delay is None else min(next_delay, _MAX_SLEEP)
            await wait_event_or_sleep(clock, self._wake, delay)
            self._wake.clear()

    def _notify(self) -> None:
        """Wake the loop task so it re-reads the job table."""
        task = self._task
        if task is None or task.done():
            return
        loop = task.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._wake.set()
        else:
            try:
                loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:  # pragma: no cover - the loop closed
                pass

    # -- Time arithmetic --------------------------------------------------

    def _jitter(self, job: Job) -> float:
        return self._rng.uniform(0.0, job.meta.jitter) if job.meta.jitter > 0 else 0.0

    @staticmethod
    def _deadline(job: Job, now_mono: float, now_wall: datetime) -> float:
        nominal = job.nominal
        if isinstance(nominal, datetime):
            return now_mono + (nominal - now_wall).total_seconds() + job.jitter_offset
        assert nominal is not None
        return nominal + job.jitter_offset

    @staticmethod
    def _wall_time(job: Job, nominal: float | datetime, now_mono: float, now_wall: datetime) -> datetime:
        if isinstance(nominal, datetime):
            return nominal.astimezone(job.tz)
        return (now_wall + timedelta(seconds=nominal - now_mono)).astimezone(job.tz)

    def _schedule_first(self, job: Job, now_mono: float, now_wall: datetime, *, anchor: float) -> None:
        kind = job.meta.kind
        if kind == "interval":
            job.grid_anchor = anchor + job.meta.initial_delay
            job.nominal = job.grid_anchor
        elif kind == "once":
            job.nominal = None if job.fired else anchor + job.meta.delay
        else:
            assert job.cron is not None
            first = job.cron.next_after(now_wall).astimezone(_UTC)
            gap = (job.cron.next_after(first).astimezone(_UTC) - first).total_seconds()
            job.period = gap
            if job.meta.misfire_grace is None:
                job.misfire_grace = min(60.0, gap)
            job.nominal = first
        job.jitter_offset = self._jitter(job)

    def _advance(self, job: Job) -> None:
        """Move to the fire after the current nominal one."""
        kind = job.meta.kind
        if kind == "interval":
            if job.meta.mode == "fixed_rate":
                assert isinstance(job.nominal, float) and job.period is not None
                job.nominal = job.nominal + job.period
            else:
                job.nominal = None  # rescheduled when the run completes
        elif kind == "cron":
            assert job.cron is not None and isinstance(job.nominal, datetime)
            job.nominal = job.cron.next_after(job.nominal).astimezone(_UTC)
        else:
            job.nominal = None
            job.fired = True
        job.jitter_offset = self._jitter(job)

    def _advance_past(self, job: Job, now_mono: float, now_wall: datetime) -> None:
        """Move to the first fire strictly after now (after a misfire)."""
        kind = job.meta.kind
        if kind == "interval":
            assert job.period is not None
            if job.meta.mode == "fixed_rate":
                steps = math.floor((now_mono - job.grid_anchor) / job.period) + 1
                job.nominal = job.grid_anchor + max(steps, 0) * job.period
            else:
                job.nominal = None
        elif kind == "cron":
            assert job.cron is not None
            job.nominal = job.cron.next_after(now_wall).astimezone(_UTC)
        else:
            job.nominal = None
            job.fired = True
        job.jitter_offset = self._jitter(job)

    def _latest_missed(self, job: Job, now_mono: float, now_wall: datetime) -> datetime:
        """The most recent nominal fire time at or before now (what a coalesced run stands for)."""
        nominal = job.nominal
        assert nominal is not None
        if job.meta.kind == "interval" and job.meta.mode == "fixed_rate":
            assert job.period is not None
            steps = max(math.floor((now_mono - job.grid_anchor) / job.period), 0)
            return self._wall_time(job, job.grid_anchor + steps * job.period, now_mono, now_wall)
        if job.cron is not None and isinstance(nominal, datetime):
            latest = nominal
            for _ in range(_CATCH_UP_LIMIT):
                following = job.cron.next_after(latest).astimezone(_UTC)
                if following > now_wall:
                    break
                latest = following
            return latest.astimezone(job.tz)
        return self._wall_time(job, nominal, now_mono, now_wall)

    # -- Firing -----------------------------------------------------------

    async def _fire(self, job: Job, now_mono: float, now_wall: datetime) -> None:
        assert job.nominal is not None
        lateness = now_mono - self._deadline(job, now_mono, now_wall)
        scheduled_at = self._wall_time(job, job.nominal, now_mono, now_wall)
        fixed_delay = job.meta.kind == "interval" and job.meta.mode == "fixed_delay"
        if lateness > job.misfire_grace + _EPSILON:
            if job.meta.misfire == "skip":
                self._advance_past(job, now_mono, now_wall)
                if fixed_delay:
                    job.nominal = now_mono + (job.period or 0.0)
                await self._skip(job, "misfire", scheduled_at)
                return
            scheduled_at = self._latest_missed(job, now_mono, now_wall)
            self._advance_past(job, now_mono, now_wall)
        else:
            self._advance(job)
        started = await self._dispatch(job, scheduled_at)
        if fixed_delay and not started:
            job.nominal = now_mono + (job.period or 0.0)

    async def _dispatch(self, job: Job, scheduled_at: datetime) -> bool:
        """Start, queue or skip one fire; ``True`` when a run started or was queued."""
        if len(job.running) < job.meta.max_instances:
            self._start(job, scheduled_at)
            return True
        if job.meta.overlap == "queue" and job.queued is None:
            job.queued = _Queued(scheduled_at)
            return True
        await self._skip(job, "overlap", scheduled_at)
        return False

    async def _skip(self, job: Job, reason: str, scheduled_at: datetime) -> None:
        job.skips += 1
        self._logger.verbose(
            f"job {job.name!r} skipped ({reason})", context="Scheduler", job=job.name, reason=reason
        )
        await self._signals.emit(JobSkipped(name=job.name, reason=reason, scheduled_at=scheduled_at))

    def _start(self, job: Job, scheduled_at: datetime) -> asyncio.Task[JobRun]:
        task = asyncio.get_running_loop().create_task(
            self._run(job, scheduled_at), name=f"lauren-job:{job.name}"
        )
        job.running.add(task)
        self._runs.add(task)
        task.add_done_callback(functools.partial(self._run_done, job))
        return task

    def _run_done(self, job: Job, task: asyncio.Task[JobRun]) -> None:
        job.running.discard(task)
        self._runs.discard(task)
        self._contexts.pop(task, None)
        registered = self._jobs.get(job.name) is job
        queued, job.queued = job.queued, None
        if queued is not None:
            if task.cancelled() or self._stopping or not registered:
                queued.fail(LifecycleViolationError(f"job {job.name!r}'s queued run was dropped"))
            else:
                follow_up = self._start(job, queued.scheduled_at)
                follow_up.add_done_callback(functools.partial(_settle_waiters, queued.waiters))
                return
        if (
            job.meta.kind == "interval"
            and job.meta.mode == "fixed_delay"
            and self._firing
            and registered
            and not job.running
            and not job.paused
        ):
            job.nominal = self._clock.monotonic() + (job.period or 0.0)
            job.jitter_offset = self._jitter(job)
            self._notify()

    async def _run(self, job: Job, scheduled_at: datetime) -> JobRun:
        clock = self._clock
        fired_at = self._now().astimezone(job.tz)
        ctx = JobContext(
            name=job.name,
            scheduled_at=scheduled_at,
            fired_at=fired_at,
            log=bound_logger(self._logger, f"Job:{job.name}", job=job.name),
        )
        self._contexts[cast("asyncio.Task[Any]", asyncio.current_task())] = ctx
        if job.meta.exclusive:
            key = f"lauren:job:{job.name}:{scheduled_at.isoformat(timespec='seconds')}"
            try:
                won = await self._lock.acquire(key, ttl=job.lock_ttl)
            except Exception as exc:
                self._logger.error(
                    f"scheduler lock failed for job {job.name!r}: {exc}; skipping this fire",
                    context="Scheduler",
                    job=job.name,
                    error=type(exc).__name__,
                )
                won = False
            if not won:
                await self._skip(job, "locked", scheduled_at)
                return JobRun(job.name, "skipped", scheduled_at, reason="locked")
            if self._stopping:  # claimed, but shutdown began: hand the fire back
                await self._lock.release(key)
                return JobRun(job.name, "skipped", scheduled_at, reason="stopping")
        started = clock.monotonic()
        await self._signals.emit(JobStarted(name=job.name, scheduled_at=scheduled_at))
        error: BaseException | None = None
        try:
            await self._invoke_with_timeout(job, ctx)
        except asyncio.CancelledError:
            ctx._cancelled = True
            duration = clock.monotonic() - started
            job.record("cancelled", fired_at, duration, "CancelledError: the run was cancelled")
            self._logger.warn(f"job {job.name!r} was cancelled", context="Scheduler", job=job.name)
            cancelled = asyncio.CancelledError(f"job {job.name!r} was cancelled")
            await self._signals.emit(
                JobFailed(name=job.name, error=cancelled, duration_s=duration, scheduled_at=scheduled_at)
            )
            raise
        except Exception as exc:
            error = exc
        duration = clock.monotonic() - started
        if error is None:
            job.record("completed", fired_at, duration, None)
            self._logger.verbose(
                f"job {job.name!r} completed in {duration:.3f}s", context="Scheduler", job=job.name
            )
            await self._signals.emit(
                JobCompleted(name=job.name, duration_s=duration, scheduled_at=scheduled_at)
            )
            return JobRun(job.name, "completed", scheduled_at, duration)
        job.record("failed", fired_at, duration, f"{type(error).__name__}: {error}")
        self._logger.error(
            f"job {job.name!r} failed: {type(error).__name__}: {error}",
            context="Scheduler",
            job=job.name,
            error=type(error).__name__,
        )
        await self._signals.emit(
            JobFailed(name=job.name, error=error, duration_s=duration, scheduled_at=scheduled_at)
        )
        return JobRun(job.name, "failed", scheduled_at, duration, error)

    async def _invoke_with_timeout(self, job: Job, ctx: JobContext) -> None:
        timeout = job.meta.timeout
        if timeout is None:
            await self._invoke(job, ctx)
            return
        inner = asyncio.ensure_future(self._invoke(job, ctx))
        timer = asyncio.ensure_future(self._clock.sleep(timeout))
        try:
            await asyncio.wait({inner, timer}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            ctx._cancelled = True
            timer.cancel()
            inner.cancel()
            await asyncio.wait({inner}, timeout=_CANCEL_GRACE)
            raise
        if inner.done():
            timer.cancel()
            inner.result()
            return
        ctx._cancelled = True
        inner.cancel()
        await asyncio.wait({inner}, timeout=_CANCEL_GRACE)
        raise _RunTimeout(f"job {job.name!r} exceeded its timeout of {timeout:g}s")

    async def _invoke(self, job: Job, ctx: JobContext) -> None:
        entry = job.entry
        track = getattr(self._clock, "_track_offloaded", None)
        if entry is None:
            assert job.fn is not None
            args = (ctx,) if job.fn_takes_ctx else ()
            await _call(job.fn_is_coroutine, functools.partial(job.fn, *args), {}, track)
            return
        values: Mapping[Any, Any] = {JobContext: ctx}
        bound = entry.bind(job.instance)
        if entry.plan.needs_scope:
            async with self._scope_factory(f"job:{job.name}", values=values) as scope:
                ctx._scope = scope
                kwargs = await entry.plan.build_kwargs(scope, values=values)
                await _call(entry.plan.is_coroutine, bound, kwargs, track)
            return
        kwargs = await entry.plan.build_kwargs(None, values=values)
        await _call(entry.plan.is_coroutine, bound, kwargs, track)

    # -- Registry operations ----------------------------------------------

    def _info(self, job: Job) -> JobInfo:
        next_run_at = None
        if self._firing and job.nominal is not None and not job.paused and job.meta.enabled:
            next_run_at = self._wall_time(job, job.nominal, self._clock.monotonic(), self._now())
        return JobInfo(
            name=job.name,
            kind=job.meta.kind,
            schedule=job.meta.describe(self._tz),
            next_run_at=next_run_at,
            last_run_at=job.last_run_at,
            last_status=job.last_status,
            last_error=job.last_error,
            last_duration_s=job.last_duration_s,
            running=len(job.running),
            paused=job.paused,
            enabled=job.meta.enabled,
            dynamic=job.dynamic,
            runs=job.runs,
            failures=job.failures,
            skips=job.skips,
        )

    def jobs(self) -> tuple[JobInfo, ...]:
        return tuple(self._info(job) for job in self._jobs.values())

    def info(self, name: str) -> JobInfo:
        return self._info(self.get(name))

    async def trigger(self, name: str) -> JobRun:
        job = self.get(name)
        if not self._armed or self._stopping:
            state = "shutting down" if self._stopping else "not started"
            raise LifecycleViolationError(
                f"cannot trigger job {name!r}: the app is {state}; call it between app.startup() and "
                "app.shutdown()",
                detail={"job": name, "reason": "not_running"},
            )
        scheduled_at = self._now().astimezone(job.tz)
        if not job.meta.enabled:
            await self._skip(job, "disabled", scheduled_at)
            return JobRun(job.name, "skipped", scheduled_at, reason="disabled")
        if len(job.running) >= job.meta.max_instances:
            if job.meta.overlap == "queue":
                waiter: asyncio.Future[JobRun] = asyncio.get_running_loop().create_future()
                if job.queued is None:
                    job.queued = _Queued(scheduled_at)
                job.queued.waiters.append(waiter)
                return await asyncio.shield(waiter)
            await self._skip(job, "overlap", scheduled_at)
            return JobRun(job.name, "skipped", scheduled_at, reason="overlap")
        return await asyncio.shield(self._start(job, scheduled_at))

    def pause(self, name: str) -> None:
        self.get(name).paused = True

    def resume(self, name: str) -> None:
        job = self.get(name)
        if not job.paused:
            return
        job.paused = False
        if self._firing:
            now_mono, now_wall = self._clock.monotonic(), self._now()
            if job.meta.kind == "interval" and job.meta.mode == "fixed_delay":
                if not job.running:
                    job.nominal = now_mono + (job.period or 0.0)
            elif job.meta.kind != "once":
                self._advance_past(job, now_mono, now_wall)
            self._notify()

    def remove(self, name: str) -> None:
        job = self.get(name)
        del self._jobs[name]
        if job.queued is not None:
            job.queued.fail(LifecycleViolationError(f"job {name!r} was removed before its queued run"))
            job.queued = None
        self._notify()

    def _check_can_add(self, where: str) -> None:
        if self._stopping:
            raise LifecycleViolationError(
                f"{where}: the app is shutting down; no new jobs can be added",
                detail={"helper": where, "reason": "shutting_down"},
            )

    def add_dynamic(
        self,
        where: str,
        name: Any,
        fn: Any,
        meta: ScheduleMeta,
    ) -> JobInfo:
        self._check_can_add(where)
        if not isinstance(name, str) or not name.strip():
            raise DecoratorUsageError(
                f"{where}: name must be a non-empty string, got {name!r}",
                detail={"helper": where, "reason": "invalid_name"},
            )
        takes_ctx, is_coroutine = _dynamic_signature(fn, where)
        job = Job(
            name=name,
            meta=meta,
            tz=meta.tz or self._tz,
            target=f"{getattr(fn, '__qualname__', repr(fn))} (dynamic)",
            fn=fn,
            fn_takes_ctx=takes_ctx,
            fn_is_coroutine=is_coroutine,
        )
        self.add(job)
        return self._info(job)


async def _call(
    is_coroutine: bool, fn: Callable[..., Any], kwargs: Mapping[str, Any], track: Callable[..., Any] | None
) -> None:
    if is_coroutine:
        result = fn(**kwargs)
    else:
        offloaded = asyncio.ensure_future(run_sync_abandonable(functools.partial(fn, **kwargs)))
        if track is not None:
            track(offloaded)  # ManualClock.advance waits for thread-offloaded runs
        try:
            result = await offloaded
        except asyncio.CancelledError:
            offloaded.cancel()  # abandons the thread
            raise
    if inspect.isawaitable(result):
        await result


def _settle_waiters(waiters: list[asyncio.Future[JobRun]], task: asyncio.Task[JobRun]) -> None:
    for waiter in waiters:
        if waiter.done():
            continue
        if task.cancelled():
            waiter.cancel()
            continue
        error = task.exception()
        if error is not None:  # pragma: no cover - _run reports failures as a JobRun
            waiter.set_exception(error)
        else:
            waiter.set_result(task.result())


class SchedulerRegistry:
    """The app's scheduler, for status and control; inject it like any provider.

    Available when ``ScheduleModule.for_root()`` is imported (one registry per
    app). :meth:`jobs` lists every job (discovered and dynamic) as
    :class:`JobInfo`; :meth:`services` lists hosted services as
    :class:`~lauren.scheduling.HostedServiceInfo`. :meth:`trigger` runs a job
    now and returns its :class:`JobRun`, respecting ``overlap`` and
    ``exclusive``. Dynamically added jobs (:meth:`add_interval`,
    :meth:`add_cron`) receive a ``JobContext`` only — no dependency
    injection — and their names must not collide with existing jobs
    (:class:`~lauren.exceptions.DuplicateBindingError`). Unknown names raise
    ``KeyError``.
    """

    __slots__ = ("_scheduler", "_supervisor")

    def __init__(self) -> None:
        self._scheduler: Scheduler | None = None
        self._supervisor: Supervisor | None = None

    def _bind(self, scheduler: Scheduler, supervisor: Supervisor | None) -> None:
        self._scheduler = scheduler
        self._supervisor = supervisor

    @property
    def _sched(self) -> Scheduler:
        if self._scheduler is None:  # pragma: no cover - bound inside LaurenFactory.create
            raise LifecycleViolationError("SchedulerRegistry is not bound to an app")
        return self._scheduler

    def jobs(self) -> tuple[JobInfo, ...]:
        """Every job, in registration order."""
        return self._sched.jobs()

    def job(self, name: str) -> JobInfo:
        """One job by name."""
        return self._sched.info(name)

    def services(self) -> tuple[HostedServiceInfo, ...]:
        """Every hosted service of the app, in start order."""
        return self._supervisor.statuses() if self._supervisor is not None else ()

    async def trigger(self, name: str) -> JobRun:
        """Run ``name`` now and wait for it (overlap and ``exclusive`` still apply).

        Works while the app is running, also when firing is disabled
        (``ScheduleModule.for_root(enabled=False)``), so tests can run a job
        with full dependency injection. Raises
        :class:`~lauren.exceptions.LifecycleViolationError` before
        ``startup()`` or during shutdown.
        """
        return await self._sched.trigger(name)

    def pause(self, name: str) -> None:
        """Stop firing ``name`` (a run in progress continues)."""
        self._sched.pause(name)

    def resume(self, name: str) -> None:
        """Fire ``name`` again from the next fire time after now."""
        self._sched.resume(name)

    def remove(self, name: str) -> None:
        """Remove ``name`` from the schedule (a run in progress continues)."""
        self._sched.remove(name)

    def add_interval(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        seconds: float | timedelta,
        initial_delay: float | timedelta = 0.0,
        mode: str = "fixed_rate",
        overlap: str = "skip",
        max_instances: int | None = None,
        timeout: float | timedelta | None = None,
        jitter: float | timedelta = 0.0,
        misfire: str = "coalesce",
        misfire_grace: float | timedelta | None = None,
        exclusive: bool = False,
    ) -> JobInfo:
        """Add a job running ``fn()`` / ``fn(ctx)`` every ``seconds`` (options as ``@interval``).

        Added while the app runs, the first fire is ``initial_delay`` seconds
        from now; added before startup, from ``READY``.
        """
        where = "SchedulerRegistry.add_interval"
        meta = ScheduleMeta(
            kind="interval",
            seconds=_number(where, "seconds", seconds, positive=True, example="seconds=60"),
            initial_delay=_number(
                where, "initial_delay", initial_delay, positive=False, example="initial_delay=5"
            ),
            mode=_choice(where, "mode", mode, ("fixed_rate", "fixed_delay")),
            **_common(
                where,
                name=name,
                overlap=overlap,
                max_instances=max_instances,
                timeout=timeout,
                jitter=jitter,
                misfire=misfire,
                misfire_grace=misfire_grace,
                exclusive=exclusive,
                enabled=True,
            ),
        )
        return self._sched.add_dynamic(where, name, fn, meta)

    def add_cron(
        self,
        name: str,
        fn: Callable[..., Any],
        expression: str | CronExpression,
        *,
        tz: str | tzinfo | None = None,
        overlap: str = "skip",
        max_instances: int | None = None,
        timeout: float | timedelta | None = None,
        jitter: float | timedelta = 0.0,
        misfire: str = "coalesce",
        misfire_grace: float | timedelta | None = None,
        exclusive: bool = False,
    ) -> JobInfo:
        """Add a job running ``fn()`` / ``fn(ctx)`` on a cron schedule (options as ``@cron``)."""
        where = "SchedulerRegistry.add_cron"
        parsed = expression if isinstance(expression, CronExpression) else CronExpression(expression)
        zone = resolve_timezone(tz, where=where) if tz is not None else parsed.tz
        meta = ScheduleMeta(
            kind="cron",
            cron=parsed.with_tz(None),
            tz=zone,
            **_common(
                where,
                name=name,
                overlap=overlap,
                max_instances=max_instances,
                timeout=timeout,
                jitter=jitter,
                misfire=misfire,
                misfire_grace=misfire_grace,
                exclusive=exclusive,
                enabled=True,
            ),
        )
        return self._sched.add_dynamic(where, name, fn, meta)

    def __repr__(self) -> str:
        count = len(self._scheduler.job_table()) if self._scheduler is not None else 0
        return f"<SchedulerRegistry {count} job(s)>"


__all__ = ["Job", "JobInfo", "JobRun", "Scheduler", "SchedulerRegistry"]
