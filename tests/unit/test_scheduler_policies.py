"""Scheduler policies driven by a ``ManualClock``: grids, overlap, timeout, jitter, misfire, locks."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from lauren import DIContainer, OperationScope
from lauren._scheduling._scheduler import Scheduler
from lauren.exceptions import DecoratorUsageError, DuplicateBindingError, LifecycleViolationError
from lauren.logging import InMemoryLogger
from lauren.scheduling import (
    InMemorySchedulerLock,
    JobContext,
    JobRun,
    ManualClock,
    SchedulerRegistry,
)
from lauren.signals import JobCompleted, JobFailed, JobSkipped, JobStarted, LifecycleEvent, SignalBus

UTC = timezone.utc
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


class Harness:
    """A bare scheduler on a manual clock, recording every lifecycle event."""

    def __init__(self, *, enabled: bool = True, lock: Any = None, clock: ManualClock | None = None) -> None:
        self.clock = clock or ManualClock()
        self.bus = SignalBus()
        self.events: list[LifecycleEvent] = []
        self.bus.on(LifecycleEvent)(self.events.append)
        self.logger = InMemoryLogger()
        container = DIContainer()
        container.compile()

        async def resolve(provider: Any) -> Any:  # pragma: no cover - no discovered jobs here
            raise AssertionError("no discovered jobs")

        self.scheduler = Scheduler(
            clock=self.clock,
            lock=lock if lock is not None else InMemorySchedulerLock(clock=self.clock),
            timezone=UTC,
            enabled=enabled,
            logger=self.logger,
            signals=self.bus,
            scope_factory=lambda name, values=None: OperationScope(container, name, values=values),
            resolve_instance=resolve,
            rng=random.Random(7),
        )
        self.registry = SchedulerRegistry()
        self.registry._bind(self.scheduler, None)

    async def start(self) -> None:
        await self.scheduler.arm()
        self.scheduler.begin()
        await self.clock.advance()

    def of(self, kind: type[LifecycleEvent]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]

    def offsets(self, kind: type[LifecycleEvent]) -> list[float]:
        return [(e.scheduled_at - EPOCH).total_seconds() for e in self.of(kind)]


@pytest.fixture
def h() -> Harness:
    return Harness()


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------


async def test_fixed_rate_fires_on_a_grid_anchored_at_ready_plus_initial_delay(h: Harness) -> None:
    h.registry.add_interval("tick", lambda: None, seconds=10, initial_delay=5)
    await h.start()

    await h.clock.advance(30)

    assert h.offsets(JobStarted) == [5.0, 15.0, 25.0]
    assert len(h.of(JobCompleted)) == 3


async def test_fixed_delay_waits_after_each_run_completes(h: Harness) -> None:
    async def work() -> None:
        await h.clock.sleep(3)

    h.registry.add_interval("work", work, seconds=10, mode="fixed_delay")
    await h.start()

    await h.clock.advance(30)

    assert h.offsets(JobStarted) == [0.0, 13.0, 26.0]


async def test_run_once_style_cron_and_interval_coexist(h: Harness) -> None:
    h.registry.add_cron("every-5-min", lambda: None, "*/5 * * * *")
    await h.start()

    await h.clock.advance(minutes=12)

    assert h.offsets(JobStarted) == [300.0, 600.0]


async def test_cron_jobs_report_scheduled_at_in_their_zone(h: Harness) -> None:
    h.registry.add_cron("hourly", lambda: None, "0 * * * *", tz=timezone(timedelta(hours=2)))
    await h.start()

    await h.clock.advance(hours=1)

    (started,) = h.of(JobStarted)
    assert started.scheduled_at.utcoffset() == timedelta(hours=2)
    assert started.scheduled_at == EPOCH + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------


async def test_overlap_skip_drops_fires_while_the_previous_run_is_going(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("slow", release.wait, seconds=10)
    await h.start()

    await h.clock.advance(25)

    assert [s.reason for s in h.of(JobSkipped)] == ["overlap", "overlap"]
    assert h.registry.job("slow").running == 1
    release.set()
    await h.clock.advance()
    assert h.registry.job("slow").skips == 2


async def test_overlap_queue_runs_once_after_the_current_run(h: Harness) -> None:
    release = asyncio.Event()

    async def slow() -> None:
        await release.wait()

    h.registry.add_interval("slow", slow, seconds=10, overlap="queue")
    await h.start()
    await h.clock.advance(25)  # fires at 10 (queued) and 20 (dropped: one queued at most)

    release.set()
    await h.clock.advance()

    assert h.offsets(JobStarted) == [0.0, 10.0]
    assert [s.reason for s in h.of(JobSkipped)] == ["overlap"]


async def test_overlap_allow_runs_concurrently_up_to_max_instances(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("par", release.wait, seconds=10, overlap="allow", max_instances=2)
    await h.start()

    await h.clock.advance(25)

    assert h.registry.job("par").running == 2
    assert [s.reason for s in h.of(JobSkipped)] == ["overlap"]
    release.set()
    await h.clock.advance()


# ---------------------------------------------------------------------------
# Timeout, failure, jitter
# ---------------------------------------------------------------------------


async def test_timeout_cancels_the_run_and_reports_a_timeout_error(h: Harness) -> None:
    seen: list[JobContext] = []

    async def stuck(ctx: JobContext) -> None:
        seen.append(ctx)
        await h.clock.sleep(1000)

    h.registry.add_interval("stuck", stuck, seconds=100, timeout=5)
    await h.start()

    await h.clock.advance(5)

    (failed,) = h.of(JobFailed)
    assert isinstance(failed.error, TimeoutError)
    assert "timeout of 5s" in str(failed.error)
    assert failed.duration_s == 5.0
    assert seen[0].cancelled is True
    assert h.registry.job("stuck").last_status == "failed"


async def test_a_timeout_error_raised_by_the_job_is_an_ordinary_failure(h: Harness) -> None:
    async def raises() -> None:
        raise TimeoutError("upstream")

    h.registry.add_interval("raises", raises, seconds=100, timeout=50)
    await h.start()

    (failed,) = h.of(JobFailed)
    assert str(failed.error) == "upstream"


async def test_a_failing_job_is_logged_counted_and_keeps_its_schedule(h: Harness) -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    h.registry.add_interval("boom", boom, seconds=10)
    await h.start()
    # Sync functions run on a worker thread; trigger() awaits one to completion.
    run = await h.registry.trigger("boom")

    info = h.registry.job("boom")
    assert run.status == "failed" and isinstance(run.error, RuntimeError)
    assert info.last_error == "RuntimeError: boom"
    assert info.failures >= 1
    assert any("boom" in m for m in h.logger.messages())


async def test_jitter_delays_each_fire_within_bounds_without_drifting_the_grid(h: Harness) -> None:
    fired: list[tuple[float, float]] = []

    def record(ctx: JobContext) -> None:
        fired.append(
            ((ctx.scheduled_at - EPOCH).total_seconds(), (ctx.fired_at - ctx.scheduled_at).total_seconds())
        )

    async def arecord(ctx: JobContext) -> None:
        record(ctx)

    h.registry.add_interval("jittery", arecord, seconds=10, jitter=4)
    await h.start()

    await h.clock.advance(60)

    assert [nominal for nominal, _ in fired][:5] == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert all(0.0 <= delay <= 4.0 for _, delay in fired)
    assert len({delay for _, delay in fired}) > 1


# ---------------------------------------------------------------------------
# Misfire
# ---------------------------------------------------------------------------


async def test_misfire_coalesce_runs_once_for_the_latest_missed_fire(h: Harness) -> None:
    h.registry.add_interval("tick", lambda: None, seconds=10)
    await h.start()  # fires at 0

    await h.clock.jump(35)  # wakes 25 s past the fire at 10 (grace 10 s)

    assert h.offsets(JobStarted) == [0.0, 30.0]
    assert h.registry.job("tick").next_run_at == EPOCH + timedelta(seconds=40)


async def test_misfire_skip_waits_for_the_next_fire(h: Harness) -> None:
    h.registry.add_interval("tick", lambda: None, seconds=10, misfire="skip")
    await h.start()

    await h.clock.jump(35)

    assert h.offsets(JobStarted) == [0.0]
    assert [s.reason for s in h.of(JobSkipped)] == ["misfire"]
    await h.clock.advance(5)
    assert h.offsets(JobStarted) == [0.0, 40.0]


async def test_lateness_within_the_grace_window_still_runs_the_fire(h: Harness) -> None:
    h.registry.add_interval("tick", lambda: None, seconds=10, misfire_grace=30)
    await h.start()

    await h.clock.jump(15)

    assert h.offsets(JobStarted) == [0.0, 10.0]


async def test_cron_misfire_coalesces_to_the_latest_missed_minute(h: Harness) -> None:
    h.registry.add_cron("minutely", lambda: None, "* * * * *")
    await h.start()

    await h.clock.jump(hours=1, seconds=30)

    assert h.offsets(JobStarted) == [3600.0]


async def test_fixed_delay_misfire_skip_reschedules_from_now(h: Harness) -> None:
    h.registry.add_interval(
        "fd", lambda: None, seconds=10, mode="fixed_delay", initial_delay=10, misfire="skip"
    )
    await h.start()

    await h.clock.jump(100)
    await h.clock.advance(10)

    assert [s.reason for s in h.of(JobSkipped)] == ["misfire"]
    assert h.offsets(JobStarted) == [110.0]


# ---------------------------------------------------------------------------
# Exclusive jobs and locks
# ---------------------------------------------------------------------------


class DenyingLock:
    def __init__(self) -> None:
        self.keys: list[tuple[str, float]] = []

    async def acquire(self, key: str, *, ttl: float) -> bool:
        self.keys.append((key, ttl))
        return False

    async def release(self, key: str) -> None:  # pragma: no cover - never acquired
        pass


async def test_exclusive_job_losing_the_lock_is_skipped_as_locked() -> None:
    lock = DenyingLock()
    h = Harness(lock=lock)
    h.registry.add_interval("ex", lambda: None, seconds=60, exclusive=True)

    await h.start()

    assert [s.reason for s in h.of(JobSkipped)] == ["locked"]
    assert lock.keys == [("lauren:job:ex:2000-01-01T00:00:00+00:00", 60.0)]
    assert not h.of(JobStarted)


async def test_lock_ttl_prefers_the_timeout() -> None:
    lock = DenyingLock()
    h = Harness(lock=lock)
    h.registry.add_interval("ex", lambda: None, seconds=7200, exclusive=True, timeout=30)
    h.registry.add_interval("ex2", lambda: None, seconds=7200, exclusive=True)

    await h.start()

    assert [ttl for _, ttl in lock.keys] == [30.0, 3600.0]


async def test_a_shared_lock_lets_one_of_two_workers_run_each_fire() -> None:
    clock = ManualClock()
    shared = InMemorySchedulerLock(clock=clock)
    workers = [Harness(lock=shared, clock=clock), Harness(lock=shared, clock=clock)]
    for worker in workers:
        worker.registry.add_cron("purge", lambda: None, "*/10 * * * *", exclusive=True)
        await worker.start()

    await clock.advance(minutes=30)

    started = [len(w.of(JobStarted)) for w in workers]
    locked = [len(w.of(JobSkipped)) for w in workers]
    assert sum(started) == 3 and sum(locked) == 3


async def test_a_failing_lock_skips_the_fire_and_logs() -> None:
    class BrokenLock:
        async def acquire(self, key: str, *, ttl: float) -> bool:
            raise ConnectionError("redis down")

        async def release(self, key: str) -> None:  # pragma: no cover
            pass

    h = Harness(lock=BrokenLock())
    h.registry.add_interval("ex", lambda: None, seconds=60, exclusive=True)

    await h.start()

    assert [s.reason for s in h.of(JobSkipped)] == ["locked"]
    assert any("redis down" in m for m in h.logger.messages())


async def test_in_memory_lock_expires_claims_after_their_ttl() -> None:
    clock = ManualClock()
    lock = InMemorySchedulerLock(clock=clock)

    assert await lock.acquire("k", ttl=10)
    assert not await lock.acquire("k", ttl=10)
    await clock.advance(10)
    assert await lock.acquire("k", ttl=10)
    await lock.release("k")
    assert await lock.acquire("k", ttl=10)
    assert "1 claim" in repr(lock)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


async def test_trigger_runs_now_and_returns_the_outcome(h: Harness) -> None:
    calls: list[str] = []
    h.registry.add_interval("t", lambda ctx: calls.append(ctx.name), seconds=3600, initial_delay=3600)
    await h.start()

    run = await h.registry.trigger("t")

    assert isinstance(run, JobRun)
    assert run.status == "completed" and run.reason is None
    assert run.scheduled_at == EPOCH
    assert calls == ["t"]


async def test_trigger_respects_overlap_skip(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("slow", release.wait, seconds=100)
    await h.start()

    run = await h.registry.trigger("slow")

    assert run.status == "skipped" and run.reason == "overlap"
    release.set()


async def test_trigger_on_a_busy_queue_job_waits_for_the_queued_run(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("q", release.wait, seconds=100, overlap="queue")
    await h.start()
    first = asyncio.create_task(h.registry.trigger("q"))
    second = asyncio.create_task(h.registry.trigger("q"))
    await h.clock.advance()

    release.set()
    runs = await asyncio.gather(first, second)

    assert [r.status for r in runs] == ["completed", "completed"]
    assert len(h.of(JobStarted)) == 2  # the scheduled run, then the single queued run


async def test_trigger_before_startup_is_a_lifecycle_violation(h: Harness) -> None:
    h.registry.add_interval("t", lambda: None, seconds=10)

    with pytest.raises(LifecycleViolationError) as info:
        await h.registry.trigger("t")

    assert info.value.detail == {"job": "t", "reason": "not_running"}


async def test_trigger_unknown_name_raises_key_error(h: Harness) -> None:
    await h.start()

    with pytest.raises(KeyError, match="no scheduled job named 'nope'"):
        await h.registry.trigger("nope")


async def test_disabled_scheduler_fires_nothing_but_trigger_works() -> None:
    h = Harness(enabled=False)
    h.registry.add_interval("t", lambda: None, seconds=10)
    await h.start()

    await h.clock.advance(100)
    run = await h.registry.trigger("t")

    assert run.status == "completed"
    assert len(h.of(JobStarted)) == 1
    assert h.registry.job("t").next_run_at is None


async def test_pause_stops_firing_and_resume_continues_on_the_grid(h: Harness) -> None:
    h.registry.add_interval("t", lambda: None, seconds=10)
    await h.start()

    h.registry.pause("t")
    await h.clock.advance(25)
    paused = h.registry.job("t")
    h.registry.resume("t")
    h.registry.resume("t")  # idempotent
    await h.clock.advance(10)

    assert paused.paused and paused.next_run_at is None
    assert h.offsets(JobStarted) == [0.0, 30.0]


async def test_resume_of_a_fixed_delay_job_waits_a_full_period(h: Harness) -> None:
    h.registry.add_interval("fd", lambda: None, seconds=10, mode="fixed_delay", initial_delay=100)
    await h.start()

    h.registry.pause("fd")
    await h.clock.advance(5)
    h.registry.resume("fd")
    await h.clock.advance(10)

    assert h.offsets(JobStarted) == [15.0]


async def test_remove_stops_a_job_and_unknown_names_raise(h: Harness) -> None:
    h.registry.add_interval("t", lambda: None, seconds=10, initial_delay=5)
    await h.start()

    h.registry.remove("t")
    await h.clock.advance(20)

    assert not h.of(JobStarted)
    with pytest.raises(KeyError):
        h.registry.remove("t")
    with pytest.raises(KeyError):
        h.registry.pause("t")


async def test_jobs_added_while_running_start_from_now(h: Harness) -> None:
    await h.start()
    await h.clock.advance(100)

    h.registry.add_interval("late", lambda: None, seconds=10, initial_delay=5)
    await h.clock.advance(20)

    assert h.offsets(JobStarted) == [105.0, 115.0]


async def test_duplicate_dynamic_name_is_a_duplicate_binding(h: Harness) -> None:
    h.registry.add_interval("t", lambda: None, seconds=10)

    with pytest.raises(DuplicateBindingError) as info:
        h.registry.add_cron("t", lambda: None, "* * * * *")

    assert info.value.detail["reason"] == "duplicate_job_name"


@pytest.mark.parametrize(
    "fn",
    [42, lambda a, b: None, lambda *, flag: None],
)
async def test_dynamic_job_function_shape_is_validated(h: Harness, fn: Any) -> None:
    with pytest.raises(DecoratorUsageError):
        h.registry.add_interval("t", fn, seconds=10)


async def test_dynamic_job_name_must_be_a_string(h: Harness) -> None:
    with pytest.raises(DecoratorUsageError):
        h.registry.add_interval("", lambda: None, seconds=10)


async def test_dynamic_job_with_an_unreadable_signature_is_rejected(h: Harness) -> None:
    class Opaque:
        __signature__ = "not a signature"

        def __call__(self) -> None: ...

    with pytest.raises(DecoratorUsageError) as info:
        h.registry.add_interval("t", Opaque(), seconds=10)

    assert info.value.detail["reason"] == "no_signature"


async def test_async_callable_objects_are_awaited(h: Harness) -> None:
    calls: list[str] = []

    class Job:
        async def __call__(self, ctx: JobContext) -> None:
            calls.append(ctx.name)

    h.registry.add_interval("obj", Job(), seconds=100)
    await h.start()

    assert calls == ["obj"]


async def test_add_cron_validates_its_zone(h: Harness) -> None:
    with pytest.raises(DecoratorUsageError):
        h.registry.add_cron("c", lambda: None, "* * * * *", tz="Nowhere/Land")


async def test_job_info_as_dict_is_json_friendly(h: Harness) -> None:
    h.registry.add_interval("t", lambda: None, seconds=10)
    await h.start()

    data = h.registry.job("t").as_dict()

    assert data["last_run_at"] == "2000-01-01T00:00:00+00:00"
    assert data["next_run_at"] == "2000-01-01T00:00:10+00:00"
    assert data["dynamic"] is True and data["runs"] == 1
    assert "1 job" in repr(h.registry)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_drain_awaits_in_flight_runs(h: Harness) -> None:
    gate = asyncio.Event()
    done: list[bool] = []

    async def finishing() -> None:
        await gate.wait()
        done.append(True)

    h.registry.add_interval("t", finishing, seconds=100)
    await h.start()
    drain = asyncio.create_task(h.scheduler.drain(5.0))
    await asyncio.sleep(0)

    gate.set()
    await drain

    assert done == [True]
    assert len(h.of(JobCompleted)) == 1


async def test_drain_cancels_runs_that_outlive_the_timeout(h: Harness) -> None:
    contexts: list[JobContext] = []

    async def forever(ctx: JobContext) -> None:
        contexts.append(ctx)
        await asyncio.Event().wait()

    h.registry.add_interval("t", forever, seconds=100)
    await h.start()

    await h.scheduler.drain(0)

    (failed,) = h.of(JobFailed)
    assert isinstance(failed.error, asyncio.CancelledError)
    assert contexts[0].cancelled
    assert h.registry.job("t").last_status == "cancelled"


async def test_drain_cancels_a_timed_run_cleanly(h: Harness) -> None:
    async def forever() -> None:
        await asyncio.Event().wait()

    h.registry.add_interval("t", forever, seconds=100, timeout=50)
    await h.start()

    await h.scheduler.drain(0)

    assert h.registry.job("t").last_status == "cancelled"


async def test_drain_fails_queued_waiters(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("q", release.wait, seconds=100, overlap="queue")
    await h.start()
    waiter = asyncio.create_task(h.registry.trigger("q"))
    await h.clock.advance()

    await h.scheduler.drain(0)

    with pytest.raises(LifecycleViolationError):
        await waiter


async def test_nothing_starts_after_stop_firing(h: Harness) -> None:
    h.registry.add_interval("t", lambda: None, seconds=10, initial_delay=10)
    await h.start()

    h.scheduler.stop_firing()
    await h.clock.advance(30)

    assert not h.of(JobStarted)
    with pytest.raises(LifecycleViolationError):
        await h.registry.trigger("t")
    with pytest.raises(LifecycleViolationError):
        h.registry.add_interval("u", lambda: None, seconds=1)


async def test_an_exclusive_run_claimed_during_shutdown_hands_the_fire_back() -> None:
    released: list[str] = []

    class SlowLock:
        async def acquire(self, key: str, *, ttl: float) -> bool:
            await asyncio.sleep(0)
            h.scheduler.stop_firing()  # shutdown begins while the claim is in flight
            return True

        async def release(self, key: str) -> None:
            released.append(key)

    h = Harness(lock=SlowLock())
    h.registry.add_interval("ex", lambda: None, seconds=60, exclusive=True)
    await h.scheduler.arm()
    h.scheduler.begin()
    await h.clock.advance()

    assert released == ["lauren:job:ex:2000-01-01T00:00:00+00:00"]
    assert not h.of(JobStarted)


# ---------------------------------------------------------------------------
# Edge paths
# ---------------------------------------------------------------------------


async def test_jobs_added_from_another_thread_wake_the_loop(h: Harness) -> None:
    await h.start()

    await asyncio.to_thread(h.registry.add_interval, "remote", _noop, seconds=10, initial_delay=5)
    await h.clock.advance(5)

    assert h.offsets(JobStarted) == [5.0]


async def _noop() -> None:
    return None


async def test_cron_with_an_explicit_grace_keeps_it(h: Harness) -> None:
    h.registry.add_cron("minutely", _noop, "* * * * *", misfire_grace=600)
    await h.start()

    await h.clock.jump(minutes=5, seconds=30)  # up to 4.5 minutes late: inside the grace window

    # Every missed minute is caught up (none is a misfire); fires that land while
    # the previous catch-up run is still going are overlap skips.
    fires = h.offsets(JobStarted) + h.offsets(JobSkipped)
    assert sorted(fires) == [60.0, 120.0, 180.0, 240.0, 300.0]
    assert {s.reason for s in h.of(JobSkipped)} <= {"overlap"}


@pytest.mark.parametrize(("misfire", "expected"), [("coalesce", [10.0]), ("skip", [])])
async def test_a_late_run_once_coalesces_or_skips(h: Harness, misfire: str, expected: list[float]) -> None:
    from lauren._scheduling._decorators import ScheduleMeta

    meta = ScheduleMeta(kind="once", delay=10, misfire=misfire, name="once")  # type: ignore[arg-type]
    h.scheduler.add_dynamic("test", "once", _noop, meta)
    await h.start()

    await h.clock.jump(200)
    await h.clock.advance(100)

    assert h.offsets(JobStarted) == expected
    assert h.registry.job("once").next_run_at is None


async def test_a_fixed_delay_fire_skipped_by_overlap_reschedules(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("fd", release.wait, seconds=10, mode="fixed_delay", initial_delay=10)
    await h.start()
    manual = asyncio.create_task(h.registry.trigger("fd"))  # holds the only slot
    await h.clock.advance(10)  # the scheduled fire is skipped (overlap)

    await h.clock.advance(10)

    assert [s.reason for s in h.of(JobSkipped)] == ["overlap", "overlap"]
    release.set()
    await manual


async def test_a_cancelled_run_drops_its_queued_follow_up(h: Harness) -> None:
    h.registry.add_interval("q", asyncio.Event().wait, seconds=100, overlap="queue")
    await h.start()
    waiter = asyncio.create_task(h.registry.trigger("q"))
    await h.clock.advance()

    (running,) = h.scheduler.get("q").running
    running.cancel()
    await h.clock.advance()

    with pytest.raises(LifecycleViolationError, match="dropped"):
        await waiter


async def test_drain_cancels_a_queued_follow_up_run(h: Harness) -> None:
    gates = [asyncio.Event(), asyncio.Event()]
    runs = iter(gates)

    async def job() -> None:
        await next(runs).wait()

    h.registry.add_interval("q", job, seconds=100, overlap="queue")
    await h.start()
    waiter = asyncio.create_task(h.registry.trigger("q"))
    await h.clock.advance()
    gates[0].set()  # the first run ends; the queued run starts and blocks
    await h.clock.advance()

    await h.scheduler.drain(0)

    with pytest.raises(asyncio.CancelledError):
        await waiter


async def test_remove_fails_queued_waiters(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("q", release.wait, seconds=100, overlap="queue")
    await h.start()
    waiter = asyncio.create_task(h.registry.trigger("q"))
    await h.clock.advance()

    h.registry.remove("q")
    release.set()

    with pytest.raises(LifecycleViolationError, match="removed"):
        await waiter


async def test_resume_before_startup_and_of_a_one_shot_job() -> None:
    from lauren._scheduling._decorators import ScheduleMeta

    h = Harness()
    h.scheduler.add_dynamic("test", "once", _noop, ScheduleMeta(kind="once", delay=20, name="once"))
    h.registry.pause("once")
    h.registry.resume("once")  # not firing yet: only the flag changes
    await h.start()
    h.registry.pause("once")
    h.registry.resume("once")  # a one-shot keeps its original fire time

    await h.clock.advance(20)

    assert h.offsets(JobStarted) == [20.0]


async def test_resume_of_a_running_fixed_delay_job_waits_for_completion(h: Harness) -> None:
    release = asyncio.Event()
    h.registry.add_interval("fd", release.wait, seconds=10, mode="fixed_delay")
    await h.start()
    h.registry.pause("fd")

    h.registry.resume("fd")
    release.set()
    await h.clock.advance()
    await h.clock.advance(10)

    assert h.offsets(JobStarted) == [0.0, 10.0]


async def test_dynamic_job_name_is_required(h: Harness) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        h.registry.add_interval(None, _noop, seconds=1)  # type: ignore[arg-type]

    assert info.value.detail["reason"] == "invalid_name"


class UntrackedClock(ManualClock):
    """A manual clock without the thread-tracking hook (like a custom Clock)."""

    _track_offloaded = None  # type: ignore[assignment]


async def test_a_sync_job_past_its_timeout_is_abandoned() -> None:
    import threading

    release = threading.Event()
    h = Harness(clock=UntrackedClock())
    h.registry.add_interval("blocking", lambda: release.wait(5), seconds=100, timeout=5)
    await h.start()

    await h.clock.advance(5)
    release.set()

    (failed,) = h.of(JobFailed)
    assert isinstance(failed.error, TimeoutError)


async def test_sync_jobs_run_without_a_tracking_clock() -> None:
    h = Harness(clock=UntrackedClock())
    h.registry.add_interval("sync", lambda: None, seconds=100, initial_delay=100)
    await h.start()

    run = await h.registry.trigger("sync")

    assert run.status == "completed"
