"""Hosted services in a full app: lifecycle ordering, readiness, restarts, startup validation.

These drive ``await app.startup()`` / ``await app.shutdown()`` inside one event
loop (pytest-asyncio), the way the ASGI lifespan protocol does.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import (
    Lauren,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    post_construct,
    pre_destruct,
)
from lauren.exceptions import (
    DuplicateBindingError,
    LifecycleConfigError,
    LifecycleError,
    MetadataInheritanceError,
)
from lauren.logging import InMemoryLogger
from lauren.scheduling import (
    HostedServiceFailed,
    ManualClock,
    ScheduleModule,
    SchedulerRegistry,
    ServiceContext,
    hosted_service,
)
from lauren.signals import LifecycleEvent, ShutdownBegin, SignalBus, StartupComplete
from lauren.testing import TestClient


def _bus(log: list[str]) -> SignalBus:
    bus = SignalBus()

    @bus.on(LifecycleEvent)
    def record(event: LifecycleEvent) -> None:
        name = type(event).__name__
        if name.startswith(("HostedService", "Startup", "Shutdown")):
            log.append(name)

    return bus


async def test_a_service_starts_after_post_construct_and_stops_before_pre_destruct() -> None:
    log: list[str] = []

    @injectable()
    class Database:
        @post_construct
        def connect(self) -> None:
            log.append("db.connect")

        @pre_destruct
        def close(self) -> None:
            log.append("db.close")

    @hosted_service()
    class Relay:
        def __init__(self, db: Database) -> None:
            self.db = db

        async def run(self, ctx: ServiceContext) -> None:
            log.append("relay.run")
            await ctx.wait_stopped()
            log.append("relay.stopped")

    @module(providers=[Database, Relay])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=_bus(log))
    app.on_shutdown(lambda: log.append("on_shutdown"))

    await app.startup()
    await asyncio.sleep(0)
    await app.shutdown()

    assert log == [
        "StartupBegin",
        "db.connect",
        "HostedServiceStarted",
        "relay.run",
        "StartupComplete",
        "ShutdownBegin",
        "relay.stopped",
        "HostedServiceStopped",
        "on_shutdown",
        "db.close",
    ]


async def test_services_start_in_dependency_order_and_stop_in_reverse() -> None:
    log: list[str] = []

    @hosted_service()
    class Consumer:
        async def run(self, ctx: ServiceContext) -> None:
            log.append("consumer.start")
            await ctx.wait_stopped()
            log.append("consumer.stop")

    @hosted_service()
    class Dispatcher:
        def __init__(self, consumer: Consumer) -> None:  # depends on another hosted service
            self.consumer = consumer

        async def run(self, ctx: ServiceContext) -> None:
            log.append("dispatcher.start")
            await ctx.wait_stopped()
            log.append("dispatcher.stop")

    @module(providers=[Dispatcher, Consumer])  # registration order is the reverse of the graph
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await asyncio.sleep(0)
    await app.shutdown()

    assert log == ["consumer.start", "dispatcher.start", "dispatcher.stop", "consumer.stop"]


async def test_wait_ready_holds_startup_until_mark_ready() -> None:
    log: list[str] = []

    @hosted_service(wait_ready=True)
    class Broker:
        async def run(self, ctx: ServiceContext) -> None:
            await asyncio.sleep(0)
            log.append("broker.connected")
            ctx.mark_ready()
            await ctx.wait_stopped()

    @module(providers=[Broker])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=_bus(log))
    await app.startup()
    await app.shutdown()

    assert log.index("broker.connected") < log.index("StartupComplete")


async def test_wait_ready_timeout_fails_startup() -> None:
    @hosted_service(wait_ready=True, start_timeout=0.01)
    class Silent:
        async def run(self, ctx: ServiceContext) -> None:
            await ctx.wait_stopped()

    @module(providers=[Silent])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)

    with pytest.raises(LifecycleError) as info:
        await app.startup()

    assert info.value.detail["reason"] == "start_timeout"


async def test_a_crashing_service_restarts_then_reports_unhealthy() -> None:
    clock = ManualClock()
    failures: list[HostedServiceFailed] = []
    bus = SignalBus()
    bus.on(HostedServiceFailed)(failures.append)

    @hosted_service(max_restarts=2, backoff=(1.0, 4.0))
    class Flaky:
        async def run(self, ctx: ServiceContext) -> None:
            raise ConnectionError("broker unreachable")

    @injectable()
    class Health:
        def __init__(self, registry: SchedulerRegistry) -> None:
            self.registry = registry

    @module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Flaky, Health])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, signals=bus)
    await app.startup()
    await clock.advance(60)

    health = await app.container.resolve(Health)
    (status,) = health.registry.services()
    assert [f.will_restart for f in failures] == [True, True, False]
    assert status.state == "failed" and status.healthy is False and status.restart_count == 2
    await app.shutdown()


async def test_hosted_services_false_builds_and_validates_without_starting() -> None:
    started: list[bool] = []

    @hosted_service()
    class Worker:
        async def run(self, ctx: ServiceContext) -> None:  # pragma: no cover - never started
            started.append(True)

    @module(providers=[Worker])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, hosted_services=False)
    await app.startup()
    await asyncio.sleep(0)
    await app.shutdown()

    assert started == []


async def test_lauren_forwards_hosted_services() -> None:
    started: list[bool] = []

    @hosted_service()
    class Worker:
        async def run(self, ctx: ServiceContext) -> None:  # pragma: no cover - never started
            started.append(True)

    @module(providers=[Worker])
    class WorkerModule: ...

    app = Lauren(hosted_services=False, scheduler=False, openapi_url=None, docs_url=None, redoc_url=None)
    app.include_module(WorkerModule)
    compiled = await app.startup()
    await compiled.startup()
    await asyncio.sleep(0)
    await app.shutdown()

    assert started == []


async def test_service_scope_gives_each_iteration_fresh_request_scoped_dependencies() -> None:
    closed: list[int] = []

    @injectable(scope=Scope.REQUEST)
    class UnitOfWork:
        def __init__(self, ctx: ServiceContext) -> None:
            self.service = ctx.name

        async def aclose(self) -> None:
            closed.append(id(self))

    seen: list[Any] = []

    @hosted_service(name="relay")
    class Relay:
        async def run(self, ctx: ServiceContext) -> None:
            for _ in range(2):
                async with ctx.scope() as scope:
                    uow = await scope.resolve(UnitOfWork)
                    seen.append((uow.service, id(uow)))
            await ctx.wait_stopped()

    @module(providers=[Relay, UnitOfWork])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await asyncio.sleep(0)
    await app.shutdown()

    assert [service for service, _ in seen] == ["relay", "relay"]
    assert len(closed) == 2


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_a_request_scoped_hosted_service_fails_startup() -> None:
    @hosted_service()
    @injectable(scope=Scope.REQUEST)
    class PerRequest:
        async def run(self, ctx: ServiceContext) -> None: ...

    @module(providers=[PerRequest])
    class AppModule: ...

    with pytest.raises(LifecycleConfigError) as info:
        LaurenFactory.create(AppModule, hosted_services=False)

    assert info.value.detail["reason"] == "not_singleton"
    assert info.value.detail["scope"] == "request"


def test_duplicate_service_names_fail_startup() -> None:
    @hosted_service(name="worker")
    class A:
        async def run(self, ctx: ServiceContext) -> None: ...

    @hosted_service(name="worker")
    class B:
        async def run(self, ctx: ServiceContext) -> None: ...

    @module(providers=[A, B])
    class AppModule: ...

    with pytest.raises(DuplicateBindingError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["name"] == "worker"
    assert info.value.detail["reason"] == "duplicate_service_name"


def test_an_undecorated_subclass_of_a_hosted_service_fails_startup() -> None:
    @hosted_service()
    class Base:
        async def run(self, ctx: ServiceContext) -> None: ...

    @injectable()
    class Child(Base): ...

    @module(providers=[Child])
    class AppModule: ...

    with pytest.raises(MetadataInheritanceError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["marker"] == "__lauren_hosted_service__"


async def test_a_redecorated_subclass_is_its_own_service() -> None:
    names: list[str] = []

    @hosted_service()
    class Base:
        async def run(self, ctx: ServiceContext) -> None:
            names.append(ctx.name)
            await ctx.wait_stopped()

    @hosted_service(name="child")
    class Child(Base): ...

    @module(providers=[Child])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    await app.startup()
    await asyncio.sleep(0)
    await app.shutdown()

    assert names == ["child"]


# ---------------------------------------------------------------------------
# Today's TestClient (startup runs in its own short-lived event loop)
# ---------------------------------------------------------------------------


def test_testclient_keeps_hosted_services_running_between_requests() -> None:
    logger = InMemoryLogger()
    states: list[str] = []

    @hosted_service()
    class Ticker:
        async def run(self, ctx: ServiceContext) -> None:
            states.append("running")
            await ctx.wait_stopped()
            states.append("stopped")

    @controller("/ping")
    class Ping:
        @get("/")
        async def ping(self) -> dict:
            return {"ok": True, "states": list(states)}

    @module(controllers=[Ping], providers=[Ticker])
    class AppModule: ...

    app = LaurenFactory.create(AppModule, logger=logger)

    with TestClient(app) as client:
        assert client.get("/ping").json() == {"ok": True, "states": ["running"]}
        assert client.get("/ping").json() == {"ok": True, "states": ["running"]}

    assert states == ["running", "stopped"]
    assert not any("cancelled outside shutdown" in m for m in logger.messages())


async def test_startup_complete_listeners_see_the_service_running() -> None:
    states: list[str] = []

    @hosted_service()
    class Worker:
        async def run(self, ctx: ServiceContext) -> None:
            await ctx.wait_stopped()

    @module(imports=[ScheduleModule.for_root(enabled=False)], providers=[Worker])
    class AppModule: ...

    bus = SignalBus()
    app = LaurenFactory.create(AppModule, signals=bus)

    async def on_ready(event: StartupComplete) -> None:
        registry = await app.container.resolve(SchedulerRegistry)
        states.append(registry.services()[0].state)

    bus.on(StartupComplete)(on_ready)
    bus.on(ShutdownBegin)(lambda e: states.append("shutdown"))

    await app.startup()
    await app.shutdown()

    assert states == ["running", "shutdown"]
