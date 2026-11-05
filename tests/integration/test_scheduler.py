"""Scheduled jobs in a full app: discovery, DI per run, startup validation, lifecycle, isolation.

Most tests drive ``await app.startup()`` / ``await app.shutdown()`` in one event
loop with a ``ManualClock``; a few use today's ``TestClient`` for HTTP.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from lauren import (
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    post_construct,
    pre_destruct,
)
from lauren.exceptions import (
    DuplicateBindingError,
    LifecycleConfigError,
    UnresolvableParameterError,
)
from lauren.logging import InMemoryLogger
from lauren.scheduling import (
    JobContext,
    JobSkipped,
    JobStarted,
    ManualClock,
    ScheduleModule,
    SchedulerRegistry,
    ServiceContext,
    cron,
    hosted_service,
    interval,
    run_once,
)
from lauren.signals import LifecycleEvent, SignalBus, StartupComplete
from lauren.testing import TestClient

EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _recorder() -> tuple[SignalBus, list[Any]]:
    bus = SignalBus()
    events: list[Any] = []
    bus.on(LifecycleEvent)(events.append)
    return bus, events


def _offsets(events: list[Any], name: str) -> list[float]:
    return [
        (e.scheduled_at - EPOCH).total_seconds()
        for e in events
        if isinstance(e, JobStarted) and e.name == name
    ]


async def _registry(app: Any) -> SchedulerRegistry:
    return await app.container.resolve(SchedulerRegistry)


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


async def test_interval_cron_and_run_once_fire_at_the_expected_clock_times() -> None:
    clock = ManualClock()
    bus, events = _recorder()

    @injectable()
    class Jobs:
        @interval(seconds=60, initial_delay=10)
        async def every_minute(self) -> None: ...

        @cron("*/5 * * * *", name="five")
        async def every_five(self) -> None: ...

        @run_once(delay=30, name="warmup")
        async def warmup(self) -> None: ...

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=bus)
    await app.startup()
    await clock.advance(minutes=11)
    await app.shutdown()

    assert _offsets(events, "Jobs.every_minute") == [10.0 + 60 * n for n in range(11)]
    assert _offsets(events, "five") == [300.0, 600.0]
    assert _offsets(events, "warmup") == [30.0]


async def test_nothing_fires_before_startup_complete() -> None:
    clock = ManualClock()
    bus, events = _recorder()

    @injectable()
    class Jobs:
        @run_once()
        async def immediately(self) -> None: ...

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=bus)
    await app.startup()
    await clock.advance()
    await app.shutdown()

    names = [type(e).__name__ for e in events]
    assert names.index("StartupComplete") < names.index("JobStarted")


async def test_a_request_scoped_dependency_is_fresh_per_run_and_torn_down() -> None:
    clock = ManualClock()
    created: list[int] = []
    closed: list[int] = []
    destructed: list[int] = []
    contexts: list[bool] = []

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        def __init__(self, ctx: JobContext) -> None:
            self.ctx = ctx
            created.append(id(self))

        @pre_destruct
        def rollback(self) -> None:
            destructed.append(id(self))

        async def aclose(self) -> None:
            closed.append(id(self))

    @injectable()
    class Purge:
        @interval(seconds=60)
        async def purge(self, ctx: JobContext, db: DbSession) -> None:
            contexts.append(db.ctx is ctx and ctx.scope is not None and ctx.scope.active)

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Purge, DbSession])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance(60)
    await app.shutdown()

    assert len(created) == 2 and len(set(created)) == 2
    assert closed == created and destructed == created
    assert contexts == [True, True]


async def test_sync_jobs_run_on_worker_threads() -> None:
    import threading

    clock = ManualClock()
    threads: list[bool] = []

    @injectable()
    class Jobs:
        @interval(seconds=10)
        def blocking(self, ctx: JobContext) -> None:
            threads.append(threading.current_thread() is not threading.main_thread())

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance(20)
    await app.shutdown()

    assert threads == [True, True, True]


async def test_static_and_class_methods_can_be_scheduled() -> None:
    clock = ManualClock()
    calls: list[str] = []

    @injectable()
    class Jobs:
        @staticmethod
        @interval(seconds=100, name="static")
        async def static_job() -> None:
            calls.append("static")

        @classmethod
        @interval(seconds=100, name="class")
        async def class_job(cls) -> None:
            calls.append(cls.__name__)

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance()
    await app.shutdown()

    assert sorted(calls) == ["Jobs", "static"]


async def test_stacked_schedules_get_numbered_default_names() -> None:
    clock = ManualClock()

    @injectable()
    class Jobs:
        @cron("0 * * * *")
        @interval(seconds=600)
        async def refresh(self) -> None: ...

    @module(imports=[ScheduleModule.for_root(clock=clock, enabled=False)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    registry = await _registry(app)

    assert [(j.name, j.kind) for j in registry.jobs()] == [
        ("Jobs.refresh#1", "interval"),
        ("Jobs.refresh#2", "cron"),
    ]


async def test_inherited_scheduled_methods_are_discovered_on_the_subclass() -> None:
    clock = ManualClock()
    bus, events = _recorder()

    class Base:
        @interval(seconds=100, name="inherited")
        async def tick(self) -> None: ...

    @injectable()
    class Concrete(Base): ...

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Concrete])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=bus)
    await app.startup()
    await clock.advance()
    await app.shutdown()

    assert _offsets(events, "inherited") == [0.0]


async def test_cron_jobs_use_the_module_timezone_by_default() -> None:
    clock = ManualClock()
    plus2 = timezone(timedelta(hours=2))
    seen: list[datetime] = []

    @injectable()
    class Jobs:
        @cron("0 3 * * *")
        async def nightly(self, ctx: JobContext) -> None:
            seen.append(ctx.scheduled_at)

    @module(imports=[ScheduleModule.for_root(clock=clock, timezone=plus2)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance(hours=1)
    await app.shutdown()

    assert seen == [datetime(2000, 1, 1, 3, 0, tzinfo=plus2)]  # 01:00 UTC


async def test_exclusive_job_with_a_denying_lock_is_skipped() -> None:
    class DenyAll:
        async def acquire(self, key: str, *, ttl: float) -> bool:
            return False

        async def release(self, key: str) -> None:  # pragma: no cover
            pass

    clock = ManualClock()
    bus, events = _recorder()
    ran: list[bool] = []

    @injectable()
    class Jobs:
        @cron("* * * * *", exclusive=True, name="purge")
        async def purge(self) -> None:
            ran.append(True)  # pragma: no cover - the lock always denies

    @module(imports=[ScheduleModule.for_root(clock=clock, lock=DenyAll())], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=bus)
    await app.startup()
    await clock.advance(minutes=2)
    await app.shutdown()

    assert [e.reason for e in events if isinstance(e, JobSkipped)] == ["locked", "locked"]
    assert ran == []


async def test_exclusive_jobs_on_the_in_memory_lock_log_once_at_ready() -> None:
    logger = InMemoryLogger()

    @injectable()
    class Jobs:
        @interval(seconds=60, exclusive=True)
        async def purge(self) -> None: ...

    @module(imports=[ScheduleModule.for_root(clock=ManualClock())], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, logger=logger)
    await app.startup()
    await app.shutdown()

    notes = [m for m in logger.messages() if "exclusivity is per process" in m]
    assert len(notes) == 1


async def test_two_apps_run_independent_schedules() -> None:
    clocks = [ManualClock(), ManualClock()]
    counts: dict[str, int] = {}

    def build(label: str, clock: ManualClock) -> Any:
        @injectable()
        class Jobs:
            @interval(seconds=10, name="tick")
            async def tick(self) -> None:
                counts[label] = counts.get(label, 0) + 1

        @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Jobs])
        class AppModule: ...

        return LaurenFactory.create(AppModule)

    first, second = build("first", clocks[0]), build("second", clocks[1])
    await first.startup()
    await second.startup()

    await clocks[0].advance(30)
    await clocks[1].advance(10)

    assert counts == {"first": 4, "second": 2}
    assert (await _registry(first)) is not (await _registry(second))
    await first.shutdown()
    await second.shutdown()


# ---------------------------------------------------------------------------
# Disabled firing and trigger()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_enabled", "factory_scheduler"),
    [(False, True), (True, False)],
    ids=["ScheduleModule(enabled=False)", "create(scheduler=False)"],
)
async def test_disabled_scheduling_fires_nothing_but_trigger_runs_with_di(
    module_enabled: bool, factory_scheduler: bool
) -> None:
    clock = ManualClock()
    runs: list[str] = []

    @injectable(scope=Scope.REQUEST)
    class Repo:
        pass

    @injectable()
    class Jobs:
        @interval(seconds=10, name="sync")
        async def sync(self, repo: Repo) -> None:
            runs.append(type(repo).__name__)

    @module(imports=[ScheduleModule.for_root(clock=clock, enabled=module_enabled)], providers=[Jobs, Repo])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, scheduler=factory_scheduler)
    await app.startup()
    await clock.advance(60)
    registry = await _registry(app)
    run = await registry.trigger("sync")
    await app.shutdown()

    assert run.status == "completed"
    assert runs == ["Repo"]


async def test_trigger_on_a_statically_disabled_job_is_skipped() -> None:
    @injectable()
    class Jobs:
        @interval(seconds=10, name="off", enabled=False)
        async def off(self) -> None:  # pragma: no cover - disabled
            pass

    @module(imports=[ScheduleModule.for_root(clock=ManualClock())], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    run = await (await _registry(app)).trigger("off")
    await app.shutdown()

    assert (run.status, run.reason) == ("skipped", "disabled")


def test_registry_is_injectable_into_controllers_and_trigger_works_under_testclient() -> None:
    calls: list[str] = []

    @injectable()
    class Jobs:
        @cron("0 3 * * *", name="purge-sessions")
        async def purge(self, ctx: JobContext) -> None:
            calls.append(ctx.name)

    @controller("/admin/jobs")
    class JobsController:
        def __init__(self, registry: SchedulerRegistry) -> None:
            self._registry = registry

        @get("/")
        async def list_jobs(self) -> list:
            return [j.as_dict() for j in self._registry.jobs()]

        @post("/{name}/run")
        async def run_now(self, name: str) -> dict:
            run = await self._registry.trigger(name)
            return {"status": run.status}

    @module(
        imports=[ScheduleModule.for_root(enabled=False)],
        controllers=[JobsController],
        providers=[Jobs],
    )
    class AppModule: ...

    client = TestClient(LaurenFactory.create(AppModule))

    listing = client.get("/admin/jobs/").json()
    triggered = client.post("/admin/jobs/purge-sessions/run").json()

    assert [(j["name"], j["kind"], j["schedule"]) for j in listing] == [
        ("purge-sessions", "cron", "cron 0 3 * * * (UTC)")
    ]
    assert triggered == {"status": "completed"}
    assert calls == ["purge-sessions"]


# ---------------------------------------------------------------------------
# Shutdown ordering
# ---------------------------------------------------------------------------


async def test_shutdown_awaits_runs_then_stops_services_then_runs_pre_destruct() -> None:
    clock = ManualClock()
    log: list[str] = []
    gate = asyncio.Event()

    @injectable()
    class Database:
        @pre_destruct
        def close(self) -> None:
            log.append("db.close")

    @hosted_service()
    class Consumer:
        def __init__(self, db: Database) -> None:
            self.db = db

        async def run(self, ctx: ServiceContext) -> None:
            await ctx.wait_stopped()
            log.append("consumer.stopped")

    @injectable()
    class Jobs:
        def __init__(self, db: Database) -> None:
            self.db = db

        @interval(seconds=10, name="slow")
        async def slow(self) -> None:
            log.append("job.start")
            await gate.wait()
            log.append("job.end")

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Database, Consumer, Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance()

    shutdown = asyncio.create_task(app.shutdown())
    await asyncio.sleep(0)
    await clock.advance(30)  # the scheduler stopped firing at ShutdownBegin
    gate.set()
    await shutdown

    assert log == ["job.start", "job.end", "consumer.stopped", "db.close"]


async def test_job_drain_timeout_cancels_slow_runs() -> None:
    clock = ManualClock()
    cancelled: list[bool] = []

    @injectable()
    class Jobs:
        @run_once(name="forever")
        async def forever(self, ctx: JobContext) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(ctx.cancelled)

    @module(imports=[ScheduleModule.for_root(clock=clock, job_drain_timeout=0)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance()
    await app.shutdown()

    assert cancelled == [True]
    assert (await _registry(app)).job("forever").last_status == "cancelled"


async def test_scheduling_post_construct_can_add_dynamic_jobs() -> None:
    clock = ManualClock()
    calls: list[str] = []

    @injectable()
    class Registrar:
        def __init__(self, registry: SchedulerRegistry) -> None:
            self.registry = registry

        @post_construct
        def register(self) -> None:
            self.registry.add_interval("heartbeat", lambda ctx: calls.append(ctx.name), seconds=30)

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Registrar])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance(60)
    await app.shutdown()

    assert calls == ["heartbeat"] * 3


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_scheduled_methods_without_schedule_module_fail_startup() -> None:
    @injectable()
    class Maintenance:
        @interval(seconds=60)
        async def warm_cache(self) -> None: ...

    @module(providers=[Maintenance])
    class AppModule: ...

    with pytest.raises(LifecycleConfigError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail == {
        "target": "Maintenance.warm_cache",
        "decorator": "interval",
        "reason": "scheduling_not_enabled",
    }
    assert "import ScheduleModule.for_root()" in info.value.message


def test_a_scheduled_method_on_a_request_scoped_provider_fails_startup() -> None:
    @injectable(scope=Scope.REQUEST)
    class PerRequest:
        @run_once()
        async def job(self) -> None: ...

    @module(imports=[ScheduleModule.for_root()], providers=[PerRequest])
    class AppModule: ...

    with pytest.raises(LifecycleConfigError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["reason"] == "not_singleton"
    assert info.value.detail["target"] == "PerRequest.job"


def test_duplicate_job_names_fail_startup() -> None:
    @injectable()
    class A:
        @interval(seconds=1, name="sync")
        async def a(self) -> None: ...

    @injectable()
    class B:
        @cron("* * * * *", name="sync")
        async def b(self) -> None: ...

    @module(imports=[ScheduleModule.for_root()], providers=[A, B])
    class AppModule: ...

    with pytest.raises(DuplicateBindingError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["name"] == "sync"
    assert info.value.detail["reason"] == "duplicate_job_name"
    assert len(info.value.detail["targets"]) == 2


def test_an_unresolvable_job_parameter_fails_startup() -> None:
    class NotAProvider:
        pass

    @injectable()
    class Jobs:
        @interval(seconds=1)
        async def job(self, thing: NotAProvider) -> None: ...

    @module(imports=[ScheduleModule.for_root()], providers=[Jobs])
    class AppModule: ...

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["target"] == "Jobs.job"
    assert info.value.detail["param"] == "thing"


def test_a_dependency_not_visible_from_the_providers_module_fails_startup() -> None:
    @injectable(scope=Scope.REQUEST)
    class Hidden:
        pass

    @module(providers=[Hidden])  # not exported
    class HiddenModule: ...

    @injectable()
    class Jobs:
        @interval(seconds=1)
        async def job(self, hidden: Hidden) -> None: ...

    @module(imports=[ScheduleModule.for_root(), HiddenModule], providers=[Jobs])
    class AppModule: ...

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["param"] == "hidden"


def test_importing_schedule_module_twice_fails_startup() -> None:
    @module(imports=[ScheduleModule.for_root()])
    class Feature: ...

    @module(imports=[ScheduleModule.for_root(), Feature])
    class AppModule: ...

    with pytest.raises(LifecycleConfigError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail == {"reason": "duplicate_schedule_module", "count": 2}


async def test_apps_without_scheduling_carry_no_runtime() -> None:
    @module()
    class Plain: ...

    app = LaurenFactory.create(Plain)
    await app.startup()
    await app.shutdown()

    assert app._scheduling is None


async def test_startup_complete_fires_once_with_scheduling_enabled() -> None:
    bus = SignalBus()
    seen: list[StartupComplete] = []
    bus.on(StartupComplete)(seen.append)

    @module(imports=[ScheduleModule.for_root(clock=ManualClock())])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=bus)
    await app.startup()
    await app.shutdown()

    assert len(seen) == 1
    assert (await _registry(app)).jobs() == ()


async def test_no_exclusivity_note_when_firing_is_disabled() -> None:
    logger = InMemoryLogger()

    @injectable()
    class Jobs:
        @interval(seconds=60, exclusive=True)
        async def purge(self) -> None: ...

    @module(imports=[ScheduleModule.for_root(clock=ManualClock(), enabled=False)], providers=[Jobs])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, logger=logger)
    await app.startup()
    await app.shutdown()

    assert not [m for m in logger.messages() if "exclusivity is per process" in m]
