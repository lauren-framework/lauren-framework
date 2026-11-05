"""Integration tests for ``OperationScope`` / ``app.scope()``.

Each test builds a real app with ``LaurenFactory.create``, starts it, and
drives scopes inside one event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from lauren import (
    Lauren,
    LaurenApp,
    LaurenFactory,
    OperationScope,
    Scope,
    injectable,
    module,
    post_construct,
    pre_destruct,
)
from lauren._di import _scope
from lauren.exceptions import DestructError, LifecycleViolationError, MissingProviderError


class JobContext:
    """A framework value a consumer seeds into the scope (stand-in for F14's)."""

    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture(autouse=True)
def _declare_job_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare ``JobContext`` the way a feature module would, for this test only."""
    monkeypatch.setattr(_scope, "_FRAMEWORK_VALUES", {**_scope._FRAMEWORK_VALUES, JobContext: None})


def _build(events: list[str]) -> tuple[LaurenApp, dict[str, type]]:
    @injectable()
    class Config:
        @post_construct
        def ready(self) -> None:
            events.append("Config.post_construct")

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        def __init__(self, config: Config) -> None:
            self.config = config
            events.append("DbSession.open")

        @pre_destruct
        async def flush(self) -> None:
            events.append("DbSession.pre_destruct")

        async def aclose(self) -> None:
            events.append("DbSession.aclose")

    @injectable(scope=Scope.REQUEST)
    class Repo:
        def __init__(self, db: DbSession, ctx: JobContext) -> None:
            self.db = db
            self.ctx = ctx

        @pre_destruct
        def close(self) -> None:
            events.append("Repo.pre_destruct")

    @injectable(scope=Scope.REQUEST)
    def audit_log() -> Any:
        events.append("audit.setup")
        yield ["audit"]
        events.append("audit.teardown")

    @injectable(scope=Scope.TRANSIENT)
    class Scratch:
        async def aclose(self) -> None:
            events.append("Scratch.aclose")

    @module(providers=[Config, DbSession, Repo, audit_log, Scratch])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)
    return app, {
        "Config": Config,
        "DbSession": DbSession,
        "Repo": Repo,
        "audit_log": audit_log,
        "Scratch": Scratch,
        "AppModule": AppModule,
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_request_scoped_instances_are_cached_per_scope() -> None:
    app, t = _build([])

    async def main() -> tuple[Any, Any, Any]:
        await app.startup()
        async with app.scope("a") as a:
            first = await a.resolve(t["DbSession"])
            again = await a.resolve(t["DbSession"])
        async with app.scope("b") as b:
            other = await b.resolve(t["DbSession"])
        await app.shutdown()
        return first, again, other

    first, again, other = _run(main())

    assert first is again
    assert first is not other


def test_singletons_are_shared_across_scopes_and_with_the_container() -> None:
    app, t = _build([])

    async def main() -> tuple[Any, Any, Any]:
        await app.startup()
        async with app.scope() as a:
            in_a = await a.resolve(t["Config"])
        async with app.scope() as b:
            in_b = await b.resolve(t["Config"])
        direct = await app.container.resolve(t["Config"])
        await app.shutdown()
        return in_a, in_b, direct

    in_a, in_b, direct = _run(main())

    assert in_a is in_b is direct


def test_transients_are_new_each_time_and_not_torn_down() -> None:
    events: list[str] = []
    app, t = _build(events)

    async def main() -> tuple[Any, Any]:
        await app.startup()
        async with app.scope() as scope:
            pair = (await scope.resolve(t["Scratch"]), await scope.resolve(t["Scratch"]))
        return pair

    first, second = _run(main())

    assert first is not second
    assert "Scratch.aclose" not in events


def test_teardown_runs_in_reverse_creation_order() -> None:
    events: list[str] = []
    app, t = _build(events)

    async def main() -> None:
        await app.startup()
        async with app.scope("job", values={JobContext: JobContext("nightly")}) as scope:
            await scope.resolve(t["Repo"])  # creates DbSession first, then Repo
            await scope.resolve(t["audit_log"])
            events.append("body done")

    _run(main())

    assert events == [
        "Config.post_construct",
        "DbSession.open",
        "audit.setup",
        "body done",
        "audit.teardown",
        "Repo.pre_destruct",
        "DbSession.pre_destruct",
        "DbSession.aclose",
    ]


def test_values_seed_framework_values() -> None:
    app, t = _build([])
    ctx = JobContext("purge")

    async def main() -> tuple[Any, Any]:
        await app.startup()
        async with app.scope("purge", values={JobContext: ctx}) as scope:
            return await scope.resolve(JobContext), await scope.resolve(t["Repo"])

    seeded, repo = _run(main())

    assert seeded is ctx
    assert repo.ctx is ctx


def test_undeclared_framework_value_still_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_scope, "_FRAMEWORK_VALUES", {})

    with pytest.raises(MissingProviderError) as info:
        _build([])

    assert info.value.detail == {"token": "JobContext"}


def test_declared_framework_value_missing_at_run_time_raises() -> None:
    app, t = _build([])

    async def main() -> None:
        await app.startup()
        async with app.scope() as scope:  # no values= seeded
            await scope.resolve(t["Repo"])

    with pytest.raises(MissingProviderError):
        _run(main())


def test_register_framework_value_returns_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_scope, "_FRAMEWORK_VALUES", {})

    class CommandContext:
        pass

    assert _scope.register_framework_value(CommandContext) is CommandContext
    assert _scope.framework_value_types() == frozenset({CommandContext})


def test_scope_exposes_its_name_and_values() -> None:
    app, _ = _build([])
    ctx = JobContext("x")

    scope = app.scope("purge", values={JobContext: ctx})

    assert isinstance(scope, OperationScope)
    assert scope.name == "purge"
    assert dict(scope.values) == {JobContext: ctx}
    assert not scope.active
    assert "purge" in repr(scope)


def test_default_scope_name() -> None:
    app, _ = _build([])

    assert app.scope().name == "operation"


def test_teardown_errors_are_collected_and_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    boom = RuntimeError("disk full")

    @injectable(scope=Scope.REQUEST)
    class Flaky:
        @pre_destruct
        def flush(self) -> None:
            raise boom

    @injectable(scope=Scope.REQUEST)
    class Healthy:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    @module(providers=[Flaky, Healthy])
    class M:
        pass

    app = LaurenFactory.create(M)

    async def main() -> tuple[OperationScope, Any]:
        await app.startup()
        async with app.scope("flaky") as scope:
            healthy = await scope.resolve(Healthy)
            await scope.resolve(Flaky)
        return scope, healthy

    with caplog.at_level(logging.ERROR, logger="lauren"):
        scope, healthy = _run(main())

    (error,) = scope.teardown_errors
    assert isinstance(error, DestructError)
    assert error.__cause__ is boom
    assert error.detail == {
        "class": "Flaky",
        "stage": "pre_destruct",
        "origin": "scope 'flaky'",
        "cause": "disk full",
    }
    assert healthy.closed  # teardown continued past the failure
    assert "Flaky" in caplog.text


def test_body_exception_propagates_after_teardown() -> None:
    events: list[str] = []
    app, t = _build(events)

    async def main() -> None:
        await app.startup()
        async with app.scope() as scope:
            await scope.resolve(t["DbSession"])
            raise ValueError("job failed")

    with pytest.raises(ValueError, match="job failed"):
        _run(main())

    assert events[-2:] == ["DbSession.pre_destruct", "DbSession.aclose"]


def test_owning_module_restricts_visibility() -> None:
    @injectable()
    class Secret:
        pass

    @module(providers=[Secret])
    class Private:
        pass

    @module(imports=[Private])
    class Consumer:
        pass

    app = LaurenFactory.create(Consumer)

    async def main() -> Any:
        await app.startup()
        async with app.scope() as scope:
            assert isinstance(await scope.resolve(Secret), Secret)  # global by default
            return await scope.resolve(Secret, owning_module=Consumer)

    with pytest.raises(MissingProviderError):
        _run(main())


@pytest.mark.parametrize("when", ["before enter", "after exit"])
def test_resolve_outside_the_block_raises(when: str) -> None:
    app, t = _build([])

    async def main() -> None:
        await app.startup()
        scope = app.scope("late")
        if when == "after exit":
            async with scope:
                pass
        await scope.resolve(t["Config"])

    with pytest.raises(LifecycleViolationError) as info:
        _run(main())

    assert info.value.detail["scope"] == "late"


def test_scopes_are_single_use() -> None:
    app, _ = _build([])

    async def main() -> None:
        await app.startup()
        scope = app.scope("once")
        async with scope:
            pass
        async with scope:
            pass

    with pytest.raises(LifecycleViolationError, match="single-use"):
        _run(main())


def test_concurrent_sibling_scopes_are_independent() -> None:
    events: list[str] = []
    app, t = _build(events)

    async def one(name: str) -> Any:
        async with app.scope(name) as scope:
            db = await scope.resolve(t["DbSession"])
            await asyncio.sleep(0)
            assert db is await scope.resolve(t["DbSession"])
            return db

    async def main() -> list[Any]:
        await app.startup()
        return list(await asyncio.gather(one("a"), one("b")))

    a, b = _run(main())

    assert a is not b
    assert events.count("DbSession.aclose") == 2


def test_two_apps_in_one_process_are_isolated() -> None:
    app_a, types_a = _build([])
    app_b, types_b = _build([])

    async def main() -> tuple[Any, Any]:
        await app_a.startup()
        await app_b.startup()
        async with app_a.scope() as a, app_b.scope() as b:
            return await a.resolve(types_a["Config"]), await b.resolve(types_b["Config"])

    config_a, config_b = _run(main())

    assert config_a is not config_b
    assert type(config_a) is not type(config_b)


def test_same_provider_class_in_two_apps_gets_separate_instances() -> None:
    @injectable()
    class Shared:
        pass

    @module(providers=[Shared])
    class M:
        pass

    app_a, app_b = LaurenFactory.create(M), LaurenFactory.create(M)

    async def main() -> tuple[Any, Any]:
        async with app_a.scope() as a, app_b.scope() as b:
            return await a.resolve(Shared), await b.resolve(Shared)

    in_a, in_b = _run(main())

    assert in_a is not in_b


def test_request_scoped_provider_outside_any_scope_falls_back_to_transient() -> None:
    # Documented behaviour: outside a request or app.scope(), a Scope.REQUEST
    # provider is built fresh on every resolution and never torn down.
    events: list[str] = []
    app, t = _build(events)

    async def main() -> tuple[Any, Any]:
        await app.startup()
        first = await app.container.resolve(t["DbSession"])
        second = await app.container.resolve(t["DbSession"])
        await app.shutdown()
        return first, second

    first, second = _run(main())

    assert first is not second
    assert "DbSession.aclose" not in events
    assert "DbSession.pre_destruct" not in events


def test_lauren_scope_requires_a_compiled_app() -> None:
    app = Lauren()

    with pytest.raises(LifecycleViolationError, match="scope"):
        app.scope()


def test_lauren_scope_forwards_to_the_compiled_app() -> None:
    @injectable(scope=Scope.REQUEST)
    class Unit:
        pass

    app = Lauren()
    app.add_provider(Unit)

    async def main() -> tuple[str, Any, Any]:
        await app.startup()
        async with app.scope("imperative") as scope:
            return scope.name, await scope.resolve(Unit), await scope.resolve(Unit)

    name, first, again = _run(main())

    assert name == "imperative"
    assert first is again
