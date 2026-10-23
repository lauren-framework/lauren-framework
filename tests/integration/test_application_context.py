"""Integration tests for ``ApplicationContext`` / ``LaurenFactory.create_context`` (PRD F19 §5.1-5.2)."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

import pytest

from lauren import (
    ApplicationContext,
    Lauren,
    LaurenApp,
    LaurenFactory,
    OperationScope,
    Scope,
    injectable,
    module,
    post_construct,
    pre_destruct,
    use_value,
)
from lauren.exceptions import (
    DIScopeViolationError,
    LifecycleViolationError,
    MissingProviderError,
)
from lauren.scheduling import ManualClock, ScheduleModule, ServiceContext, hosted_service, interval
from lauren.testing import Test


EVENTS: list[str] = []


@pytest.fixture(autouse=True)
def _reset_events() -> None:
    EVENTS.clear()


@injectable()
class Database:
    def __init__(self) -> None:
        self.open = False

    @post_construct
    def connect(self) -> None:
        self.open = True
        EVENTS.append("Database.post_construct")

    @pre_destruct
    def disconnect(self) -> None:
        EVENTS.append("Database.pre_destruct")


@injectable(scope=Scope.REQUEST)
class DbSession:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def aclose(self) -> None:
        EVENTS.append("DbSession.aclose")


@injectable(scope=Scope.TRANSIENT)
class Scratch:
    pass


@injectable()
def settings() -> Any:
    EVENTS.append("settings.setup")
    yield {"debug": False}
    EVENTS.append("settings.teardown")


@module(providers=[Database, DbSession, Scratch, settings])
class AppModule:
    pass


def _build(events: list[str]) -> dict[str, Any]:
    """The shared test graph; ``events`` is the module-level ``EVENTS`` list."""
    assert events is EVENTS
    return {
        "AppModule": AppModule,
        "Database": Database,
        "DbSession": DbSession,
        "Scratch": Scratch,
        "settings": settings,
    }


async def test_enter_runs_post_construct_and_exit_runs_pre_destruct() -> None:
    events = EVENTS
    t = _build(events)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        events.append("body")
        assert ctx.active

    assert (
        events.index("Database.post_construct") < events.index("body") < events.index("Database.pre_destruct")
    )
    assert not ctx.active


async def test_get_returns_the_constructed_singleton() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        db = ctx.get(t["Database"])
        assert db.open is True
        assert db is await ctx.resolve(t["Database"])


async def test_get_unwraps_generator_singletons() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        assert ctx.get(t["settings"]) == {"debug": False}


@pytest.mark.parametrize("token", ["DbSession", "Scratch"])
async def test_get_rejects_non_singletons(token: str) -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        with pytest.raises(DIScopeViolationError) as info:
            ctx.get(t[token])

    assert info.value.detail["reason"] == "not_singleton"


async def test_get_rejects_unknown_tokens() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        with pytest.raises(MissingProviderError):
            ctx.get(OperationScope)


async def test_resolve_returns_new_transients() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        assert await ctx.resolve(t["Scratch"]) is not await ctx.resolve(t["Scratch"])


async def test_resolve_refuses_request_scoped_tokens() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        with pytest.raises(DIScopeViolationError) as info:
            await ctx.resolve(t["DbSession"])

    assert info.value.detail == {"token": "DbSession", "reason": "no_active_scope"}


async def test_resolve_reports_missing_providers() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        with pytest.raises(MissingProviderError):
            await ctx.resolve(OperationScope)


async def test_scope_resolves_request_scoped_providers_and_tears_them_down() -> None:
    events = EVENTS
    t = _build(events)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        async with ctx.scope("backfill") as scope:
            session = await scope.resolve(t["DbSession"])
            assert session.db is ctx.get(t["Database"])
            assert scope.name == "backfill"
        assert "DbSession.aclose" in events


async def test_default_scope_name_is_context() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        assert ctx.scope().name == "context"


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("get", (Database,)),
        ("resolve", (Database,)),
        ("scope", ()),
        ("run_command", ("x",)),
        ("wait_for_shutdown_signal", ()),
    ],
)
async def test_members_require_an_entered_context(call: str, args: tuple[Any, ...]) -> None:
    ctx = LaurenFactory.create_context(AppModule)

    with pytest.raises(LifecycleViolationError) as info:
        result = getattr(ctx, call)(*args)
        if asyncio.iscoroutine(result):
            await result

    assert info.value.detail["reason"] == "context_not_active"


async def test_escape_hatches_expose_app_and_container() -> None:
    t = _build(EVENTS)
    ctx = LaurenFactory.create_context(t["AppModule"])

    assert isinstance(ctx.app, LaurenApp)
    assert ctx.container is ctx.app.container
    assert "pending" in repr(ctx)


async def test_body_exception_propagates_after_shutdown() -> None:
    events = EVENTS
    t = _build(events)

    with pytest.raises(RuntimeError, match="boom"):
        async with LaurenFactory.create_context(t["AppModule"]):
            raise RuntimeError("boom")

    assert "Database.pre_destruct" in events


async def test_leaving_twice_shuts_down_once() -> None:
    ctx = LaurenFactory.create_context(AppModule)
    await ctx.__aenter__()

    await ctx.__aexit__(None, None, None)
    await ctx.__aexit__(None, None, None)

    assert EVENTS.count("Database.pre_destruct") == 1


async def test_contexts_are_single_use() -> None:
    t = _build(EVENTS)
    ctx = LaurenFactory.create_context(t["AppModule"])
    async with ctx:
        pass

    with pytest.raises(LifecycleViolationError) as info:
        await ctx.__aenter__()

    assert info.value.detail["reason"] == "context_reused"


async def test_wrapping_a_started_app_is_rejected() -> None:
    t = _build(EVENTS)
    app = LaurenFactory.create(t["AppModule"])
    await app.startup()
    try:
        with pytest.raises(LifecycleViolationError) as info:
            app.context()
    finally:
        await app.shutdown()

    assert info.value.detail["reason"] == "app_already_started"


async def test_wrapping_a_shut_down_app_is_rejected() -> None:
    t = _build(EVENTS)
    app = LaurenFactory.create(t["AppModule"])
    await app.shutdown()

    with pytest.raises(LifecycleViolationError) as info:
        app.context()

    assert info.value.detail["reason"] == "app_shut_down"


async def test_app_started_after_wrapping_is_rejected_on_enter() -> None:
    t = _build(EVENTS)
    app = LaurenFactory.create(t["AppModule"])
    ctx = app.context()
    await app.startup()
    try:
        with pytest.raises(LifecycleViolationError):
            await ctx.__aenter__()
    finally:
        await app.shutdown()


async def test_app_context_wraps_a_built_app() -> None:
    events = EVENTS
    t = _build(events)
    app = LaurenFactory.create(t["AppModule"])

    async with app.context(drain_timeout=1.0) as ctx:
        assert ctx.app is app
        assert app._started

    assert "Database.pre_destruct" in events


@injectable()
class First:
    @post_construct
    def up(self) -> None:
        EVENTS.append("First.up")

    @pre_destruct
    def down(self) -> None:
        EVENTS.append("First.down")


@injectable()
class Second:
    def __init__(self, first: First) -> None:
        self.first = first

    @post_construct
    def up(self) -> None:
        raise RuntimeError("cannot connect")


@module(providers=[First, Second])
class FailingModule:
    pass


async def test_failed_startup_shuts_down_what_started_then_propagates() -> None:
    ctx = LaurenFactory.create_context(FailingModule)
    with pytest.raises(RuntimeError, match="cannot connect"):
        async with ctx:
            pass  # pragma: no cover - never entered

    assert EVENTS == ["First.up", "First.down"]
    assert not ctx.active


async def test_failed_startup_logs_a_failing_cleanup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    t = _build(EVENTS)
    app = LaurenFactory.create(t["AppModule"])

    async def broken_startup() -> None:
        raise RuntimeError("startup")

    async def broken_shutdown(**_: Any) -> None:
        raise ValueError("shutdown")

    monkeypatch.setattr(app, "startup", broken_startup)
    monkeypatch.setattr(app, "shutdown", broken_shutdown)

    with caplog.at_level(logging.ERROR, logger="lauren"), pytest.raises(RuntimeError, match="startup"):
        async with app.context():
            pass  # pragma: no cover - never entered

    assert "shutdown after a failed startup raised" in caplog.text


async def test_from_lauren_compiles_without_starting() -> None:
    events = EVENTS
    t = _build(events)
    lauren_app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)
    lauren_app.include_module(t["AppModule"])

    ctx = ApplicationContext.from_lauren(lauren_app)
    assert events == []
    async with ctx:
        assert ctx.get(t["Database"]).open

    assert ctx.app is lauren_app._compiled
    assert "Database.pre_destruct" in events


async def test_two_contexts_from_two_apps_coexist() -> None:
    first = second = _build(EVENTS)

    async with LaurenFactory.create_context(first["AppModule"]) as a:
        async with LaurenFactory.create_context(second["AppModule"]) as b:
            assert a.get(first["Database"]) is not b.get(second["Database"])
            assert a.container is not b.container


async def test_create_context_forwards_factory_options() -> None:
    t = _build(EVENTS)

    async with LaurenFactory.create_context(t["AppModule"], strict_request_scope=True) as ctx:
        with pytest.raises(DIScopeViolationError):
            await ctx.container.resolve(t["DbSession"])


def test_create_context_rejects_unknown_options() -> None:
    t = _build(EVENTS)

    with pytest.raises(TypeError):
        LaurenFactory.create_context(t["AppModule"], not_an_option=True)


# ---------------------------------------------------------------------------
# Multi-bound and aliased singletons
# ---------------------------------------------------------------------------


class Plugin:
    def __init__(self, name: str) -> None:
        self.name = name


async def test_get_returns_every_multi_bound_singleton() -> None:
    @module(
        providers=[
            use_value(provide=Plugin, value=Plugin("a"), multi=True),
            use_value(provide=Plugin, value=Plugin("b"), multi=True),
        ]
    )
    class PluginModule:
        pass

    async with LaurenFactory.create_context(PluginModule) as ctx:
        assert [p.name for p in ctx.get(Plugin)] == ["a", "b"]
        assert [p.name for p in ctx.get(list[Plugin])] == ["a", "b"]


async def test_get_rejects_an_unknown_list_token() -> None:
    @module(providers=[])
    class EmptyModule:
        pass

    async with LaurenFactory.create_context(EmptyModule) as ctx:
        with pytest.raises(MissingProviderError):
            ctx.get(list[Plugin])


async def test_get_follows_aliases() -> None:
    from lauren import use_existing

    @injectable()
    class Real:
        pass

    @module(providers=[Real, use_existing(provide="alias", existing=Real)])
    class AliasModule:
        pass

    async with LaurenFactory.create_context(AliasModule) as ctx:
        assert ctx.get("alias") is ctx.get(Real)
        assert await ctx.resolve("alias") is ctx.get(Real)


# ---------------------------------------------------------------------------
# Hosted services and the scheduler (F14)
# ---------------------------------------------------------------------------


def _scheduling_app(events: list[str]) -> type:
    @hosted_service()
    class Relay:
        async def run(self, ctx: ServiceContext) -> None:
            events.append("relay.run")
            await ctx.wait_stopped()

    @injectable()
    class Jobs:
        @interval(seconds=60)
        async def tick(self) -> None:  # pragma: no cover - never fires in these tests
            events.append("tick")

    @module(imports=[ScheduleModule.for_root(clock=ManualClock())], providers=[Relay, Jobs])
    class WorkerModule:
        pass

    return WorkerModule


async def test_hosted_services_and_scheduler_are_off_by_default() -> None:
    events: list[str] = []

    async with LaurenFactory.create_context(_scheduling_app(events)) as ctx:
        await asyncio.sleep(0)
        scheduling = ctx.app._scheduling
        assert scheduling is not None and scheduling.scheduler is not None
        assert scheduling.scheduler.firing is False

    assert events == []


async def test_hosted_services_start_when_enabled() -> None:
    events: list[str] = []

    async with LaurenFactory.create_context(
        _scheduling_app(events), hosted_services=True, scheduler=True
    ) as ctx:
        for _ in range(50):
            if events:
                break
            await asyncio.sleep(0.01)
        scheduling = ctx.app._scheduling
        assert scheduling is not None and scheduling.scheduler is not None
        assert scheduling.scheduler.firing is True

    assert events == ["relay.run"]


async def test_app_context_narrows_an_app_built_with_hosted_services() -> None:
    events: list[str] = []
    app = LaurenFactory.create(_scheduling_app(events))  # hosted_services=True by default

    async with app.context():
        await asyncio.sleep(0.05)

    assert events == []


async def test_app_context_cannot_enable_what_the_app_disabled() -> None:
    events: list[str] = []
    app = LaurenFactory.create(_scheduling_app(events), hosted_services=False)

    async with app.context(hosted_services=True):
        await asyncio.sleep(0.05)

    assert events == []


# ---------------------------------------------------------------------------
# Testing story (F21 overrides apply to contexts)
# ---------------------------------------------------------------------------


async def test_testing_module_overrides_apply_to_contexts() -> None:
    t = _build(EVENTS)

    class FakeDatabase:
        open = "fake"

    app = (
        Test.create_testing_module(t["AppModule"])
        .override_provider(t["Database"])
        .use_value(FakeDatabase())
        .compile()
    )

    async with app.context() as ctx:
        assert ctx.get(t["Database"]).open == "fake"


# ---------------------------------------------------------------------------
# Workers: wait_for_shutdown_signal
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="delivers real signals to the test process")


@posix_only
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_wait_for_shutdown_signal_returns_the_signal(signum: int) -> None:
    t = _build(EVENTS)
    previous = signal.getsignal(signum)

    async with LaurenFactory.create_context(t["AppModule"]) as ctx:
        asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signum)
        received = await asyncio.wait_for(ctx.wait_for_shutdown_signal(), timeout=10)

    assert received == signum
    assert signal.getsignal(signum) == previous  # handlers are removed again


async def test_wait_for_shutdown_signal_outside_the_main_thread_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    t = _build(EVENTS)

    async def run() -> None:
        async with LaurenFactory.create_context(t["AppModule"]) as ctx:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ctx.wait_for_shutdown_signal(), timeout=0.05)

    with caplog.at_level(logging.WARNING, logger="lauren"):
        await asyncio.to_thread(asyncio.run, run())

    assert "no signal handler could be installed" in caplog.text
