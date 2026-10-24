"""The hosted-service supervisor: restart policy, backoff, exhaustion, stop paths, readiness."""

from __future__ import annotations

import asyncio
import inspect
import random
import threading
import time
from typing import Any

import pytest

from lauren import DIContainer, OperationScope
from lauren._scheduling._supervisor import ServiceRunner, Supervisor
from lauren.exceptions import LifecycleError
from lauren.logging import InMemoryLogger
from lauren.scheduling import HostedServiceInfo, HostedServiceMeta, ManualClock, ServiceContext
from lauren.signals import (
    HostedServiceFailed,
    HostedServiceStarted,
    HostedServiceStopped,
    LifecycleEvent,
    SignalBus,
)

SEED = 11


class Harness:
    def __init__(self, *services: tuple[Any, HostedServiceMeta], enabled: bool = True) -> None:
        self.clock = ManualClock()
        self.bus = SignalBus()
        self.events: list[LifecycleEvent] = []
        self.bus.on(LifecycleEvent)(self.events.append)
        self.logger = InMemoryLogger()
        container = DIContainer()
        container.compile()

        async def resolve(provider: Any) -> Any:
            return provider  # the "provider" is the instance itself in these tests

        self.runners = [
            ServiceRunner(
                name=meta.name or type(instance).__name__,
                meta=meta,
                provider=instance,
                is_coroutine=inspect.iscoroutinefunction(instance.run),
            )
            for instance, meta in services
        ]
        self.supervisor = Supervisor(
            self.runners,
            clock=self.clock,
            logger=self.logger,
            signals=self.bus,
            scope_factory=lambda name, values=None: OperationScope(container, name, values=values),
            resolve_instance=resolve,
            enabled=enabled,
            rng=random.Random(SEED),
        )

    def of(self, kind: type[LifecycleEvent]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]

    def status(self, index: int = 0) -> HostedServiceInfo:
        return self.supervisor.statuses()[index]


class Cooperative:
    def __init__(self) -> None:
        self.ticks = 0

    async def run(self, ctx: ServiceContext) -> None:
        while not ctx.stop_requested:
            if await ctx.sleep(1.0):
                self.ticks += 1


class Crashing:
    def __init__(self) -> None:
        self.starts: list[float] = []
        self.clock: ManualClock | None = None

    async def run(self, ctx: ServiceContext) -> None:
        assert self.clock is not None
        self.starts.append(self.clock.monotonic())
        raise RuntimeError(f"crash {len(self.starts)}")


async def test_a_cooperative_service_runs_until_stopped() -> None:
    service = Cooperative()
    h = Harness((service, HostedServiceMeta()))

    await h.supervisor.start()
    await h.clock.advance(3)
    running = h.status()
    await h.supervisor.stop()

    assert service.ticks == 3
    assert running.state == "running" and running.healthy
    assert h.status().state == "stopped"
    assert [type(e).__name__ for e in h.events] == ["HostedServiceStarted", "HostedServiceStopped"]
    assert h.of(HostedServiceStopped)[0].duration_s == 3.0


async def test_restart_backoff_follows_full_jitter_exponential_delays() -> None:
    service = Crashing()
    h = Harness((service, HostedServiceMeta(max_restarts=3, backoff=(1.0, 30.0), restart_window=1000)))
    service.clock = h.clock
    rng = random.Random(SEED)
    delays = [rng.uniform(0.0, min(30.0, 1.0 * 2**n)) for n in range(3)]

    await h.supervisor.start()
    await h.clock.advance(100)

    expected = [0.0, delays[0], delays[0] + delays[1], delays[0] + delays[1] + delays[2]]
    assert service.starts == pytest.approx(expected)
    assert [f.will_restart for f in h.of(HostedServiceFailed)] == [True, True, True, False]
    assert [f.restart_count for f in h.of(HostedServiceFailed)] == [0, 1, 2, 3]
    status = h.status()
    assert status.state == "failed" and status.restart_count == 3
    assert status.healthy is False  # on_exhausted="fail_readiness"
    assert status.last_error == "RuntimeError: crash 4"


async def test_restarts_outside_the_window_are_forgotten() -> None:
    class FailsAfterWork:
        def __init__(self) -> None:
            self.clock: ManualClock | None = None
            self.starts = 0

        async def run(self, ctx: ServiceContext) -> None:
            assert self.clock is not None
            self.starts += 1
            await self.clock.sleep(20)
            raise RuntimeError("late failure")

    service = FailsAfterWork()
    h = Harness(
        (service, HostedServiceMeta(max_restarts=1, restart_window=10, backoff=(0.0, 0.0), stop_timeout=0.0))
    )
    service.clock = h.clock

    await h.supervisor.start()
    await h.clock.advance(65)

    assert service.starts == 4  # each failure comes 20 s after the last restart (> window)
    await h.supervisor.stop()


async def test_on_exhausted_log_keeps_the_service_healthy() -> None:
    service = Crashing()
    h = Harness((service, HostedServiceMeta(restart="never", on_exhausted="log")))
    service.clock = h.clock

    await h.supervisor.start()
    await h.clock.advance()

    assert h.status().state == "failed"
    assert h.status().healthy is True
    assert any("will not restart" in m for m in h.logger.messages())


async def test_restart_never_reports_a_final_failure() -> None:
    service = Crashing()
    h = Harness((service, HostedServiceMeta(restart="never")))
    service.clock = h.clock

    await h.supervisor.start()
    await h.clock.advance(60)

    assert len(service.starts) == 1
    (failed,) = h.of(HostedServiceFailed)
    assert failed.will_restart is False and isinstance(failed.error, RuntimeError)


async def test_restart_always_restarts_after_a_normal_return() -> None:
    class Returns:
        def __init__(self) -> None:
            self.starts = 0

        async def run(self, ctx: ServiceContext) -> None:
            self.starts += 1

    service = Returns()
    h = Harness((service, HostedServiceMeta(restart="always", max_restarts=2, backoff=(0.0, 0.0))))

    await h.supervisor.start()
    await h.clock.advance(5)

    assert service.starts == 3
    assert h.status().state == "failed"  # out of restarts
    assert not h.of(HostedServiceFailed)


async def test_on_failure_does_not_restart_a_normal_return() -> None:
    class Returns:
        async def run(self, ctx: ServiceContext) -> None:
            return None

    h = Harness((Returns(), HostedServiceMeta()))

    await h.supervisor.start()
    await h.clock.advance()

    assert h.status().state == "stopped" and h.status().healthy
    assert len(h.of(HostedServiceStarted)) == 1


async def test_stop_requested_during_backoff_ends_the_service() -> None:
    service = Crashing()
    h = Harness((service, HostedServiceMeta(backoff=(100.0, 100.0))))
    service.clock = h.clock
    await h.supervisor.start()
    await h.clock.advance()

    await h.supervisor.stop()

    assert len(service.starts) == 1
    assert h.status().state == "stopped"


async def test_a_failure_while_stopping_is_reported_without_restart() -> None:
    class FailsOnStop:
        async def run(self, ctx: ServiceContext) -> None:
            await ctx.wait_stopped()
            raise RuntimeError("flush failed")

    h = Harness((FailsOnStop(), HostedServiceMeta()))
    await h.supervisor.start()

    await h.supervisor.stop()

    (failed,) = h.of(HostedServiceFailed)
    assert failed.will_restart is False
    assert h.status().state == "stopped"


async def test_a_service_ignoring_stop_is_cancelled_after_stop_timeout() -> None:
    cancelled: list[bool] = []

    class Stubborn:
        async def run(self, ctx: ServiceContext) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    h = Harness((Stubborn(), HostedServiceMeta(stop_timeout=0.01)))
    await h.supervisor.start()

    await h.supervisor.stop()

    assert cancelled == [True]
    assert h.status().state == "stopped"
    assert any("did not stop within" in m for m in h.logger.messages())
    assert len(h.of(HostedServiceStopped)) == 1


async def test_sync_service_runs_on_its_own_thread_and_polls_stop() -> None:
    seen: list[str] = []

    class Polling:
        def run(self, ctx: ServiceContext) -> None:
            seen.append(threading.current_thread().name)
            ctx.mark_ready()
            while not ctx.stop_requested:
                time.sleep(0.001)

    h = Harness((Polling(), HostedServiceMeta(name="poller", wait_ready=True)))

    await h.supervisor.start()
    ready = h.status().ready
    await h.supervisor.stop()

    assert seen == ["lauren-hosted:poller"]
    assert ready is True
    assert h.status().state == "stopped"


async def test_a_blocked_sync_service_is_abandoned_after_stop_timeout() -> None:
    release = threading.Event()

    class Blocked:
        def run(self, ctx: ServiceContext) -> None:
            release.wait(5)

    h = Harness((Blocked(), HostedServiceMeta(stop_timeout=0.01)))
    await h.supervisor.start()

    await h.supervisor.stop()
    release.set()

    assert any("abandoned" in m for m in h.logger.messages())


async def test_wait_ready_returns_once_the_service_marks_ready() -> None:
    class Ready:
        async def run(self, ctx: ServiceContext) -> None:
            await asyncio.sleep(0)
            ctx.mark_ready()
            await ctx.wait_stopped()

    h = Harness((Ready(), HostedServiceMeta(wait_ready=True)))

    await h.supervisor.start()

    assert h.status().ready is True
    await h.supervisor.stop()
    assert h.status().ready is False


async def test_wait_ready_timeout_stops_started_services_and_raises() -> None:
    first, never = Cooperative(), Cooperative()
    h = Harness(
        (first, HostedServiceMeta(name="first")),
        (never, HostedServiceMeta(name="never", wait_ready=True, start_timeout=0.01)),
    )

    with pytest.raises(LifecycleError) as info:
        await h.supervisor.start()

    assert info.value.detail == {"service": "never", "reason": "start_timeout", "start_timeout": 0.01}
    assert [s.state for s in h.supervisor.statuses()] == ["stopped", "stopped"]


async def test_wait_ready_fails_when_the_service_exits_first() -> None:
    class Quits:
        async def run(self, ctx: ServiceContext) -> None:
            return None

    h = Harness((Quits(), HostedServiceMeta(wait_ready=True)))

    with pytest.raises(LifecycleError) as info:
        await h.supervisor.start()

    assert info.value.detail["reason"] == "exited_before_ready"


async def test_hosted_services_disabled_starts_nothing() -> None:
    h = Harness((Cooperative(), HostedServiceMeta()), enabled=False)

    await h.supervisor.start()
    await h.supervisor.stop()

    assert h.status().state == "pending"
    assert not h.events
    assert any("not started" in m for m in h.logger.messages())


async def test_a_disabled_service_is_not_started() -> None:
    h = Harness((Cooperative(), HostedServiceMeta(enabled=False)))

    await h.supervisor.start()

    assert h.status().state == "disabled"
    assert not h.events


async def test_cancellation_outside_shutdown_is_logged_and_propagates() -> None:
    h = Harness((Cooperative(), HostedServiceMeta()))
    await h.supervisor.start()
    await h.clock.advance()  # the service is inside run()
    task = h.runners[0].task
    assert task is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert h.status().state == "stopped"
    assert any("cancelled outside shutdown" in m for m in h.logger.messages())
    await h.supervisor.stop()  # already finished: a no-op


async def test_services_stop_in_reverse_start_order() -> None:
    order: list[str] = []

    class Named:
        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self, ctx: ServiceContext) -> None:
            await ctx.wait_stopped()
            order.append(self.name)

    h = Harness((Named("a"), HostedServiceMeta(name="a")), (Named("b"), HostedServiceMeta(name="b")))
    await h.supervisor.start()

    await h.supervisor.stop()

    assert order == ["b", "a"]


async def test_service_context_scope_seeds_the_context() -> None:
    seen: list[Any] = []

    class Scoped:
        async def run(self, ctx: ServiceContext) -> None:
            async with ctx.scope() as scope:
                seen.append((scope.name, scope.values[ServiceContext] is ctx))
            async with ctx.scope("custom") as scope:
                seen.append(scope.name)

    h = Harness((Scoped(), HostedServiceMeta(name="svc")))

    await h.supervisor.start()
    await h.clock.advance()

    assert seen == [("service:svc", True), "custom"]


async def test_wait_stopped_times_out_on_the_clock() -> None:
    results: list[bool] = []

    class Waiter:
        async def run(self, ctx: ServiceContext) -> None:
            results.append(await ctx.wait_stopped(timeout=30))
            results.append(await ctx.wait_stopped())

    h = Harness((Waiter(), HostedServiceMeta()))
    await h.supervisor.start()

    await h.clock.advance(30)
    await h.supervisor.stop()

    assert results == [False, True]


async def test_service_context_exposes_name_log_and_restarts() -> None:
    seen: list[Any] = []

    class Introspect:
        async def run(self, ctx: ServiceContext) -> None:
            seen.append((ctx.name, ctx.restart_count, ctx.ready))
            ctx.log.info("hello", answer=42)
            ctx.mark_ready()
            seen.append(repr(ctx))
            await ctx.wait_stopped()

    h = Harness((Introspect(), HostedServiceMeta(name="intro")))
    await h.supervisor.start()
    await h.clock.advance()
    await h.supervisor.stop()

    assert seen[0] == ("intro", 0, False)
    assert "ready" in seen[1]
    record = next(r for r in h.logger.records if r.message == "hello")
    assert record.context == "Service:intro"
    assert record.extra == {"service": "intro", "answer": 42}


def test_hosted_service_info_as_dict() -> None:
    info = HostedServiceInfo(
        name="n", state="running", ready=True, restart_count=1, last_error=None, healthy=True
    )

    assert info.as_dict() == {
        "name": "n",
        "state": "running",
        "ready": True,
        "restart_count": 1,
        "last_error": None,
        "healthy": True,
    }
