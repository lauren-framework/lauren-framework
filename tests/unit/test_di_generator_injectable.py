"""Unit tests for generator-based injectable lifecycle.

When an ``@injectable()``-decorated function is a generator (or async
generator) the container treats it as a FastAPI-style context manager:

* Code before ``yield`` is setup (like ``@post_construct``).
* The yielded value becomes the resolved dependency.
* Code after ``yield`` is teardown (like ``@pre_destruct``).

Teardown is invoked via ``aclose()`` when the scope ends:

* ``SINGLETON`` — ``LifecycleScheduler.run_pre_destruct()`` explicit pass.
* ``REQUEST``   — ASGI/WS cleanup already calls ``aclose()`` on every
  request-scoped instance; no special handling needed.
* ``TRANSIENT`` — disallowed; raises ``StartupError`` at registration.
"""

from __future__ import annotations

from typing import Any

import pytest

from lauren import Depends, Scope, injectable
from lauren._di import DIContainer, _GeneratorContextWrapper
from lauren._lifecycle import LifecycleScheduler
from lauren.exceptions import StartupError


# ---------------------------------------------------------------------------
# Sync generator provider
# ---------------------------------------------------------------------------


class TestSyncGeneratorProvider:
    @pytest.mark.asyncio
    async def test_yields_value_to_caller(self):
        setup_calls: list[str] = []
        teardown_calls: list[str] = []

        @injectable()
        def db_conn():
            setup_calls.append("setup")
            yield "conn-123"
            teardown_calls.append("teardown")

        c = DIContainer()
        c.register(db_conn)
        c.compile()

        value = await c.resolve(db_conn)
        assert value == "conn-123"
        assert setup_calls == ["setup"]
        assert teardown_calls == []  # not yet closed

    @pytest.mark.asyncio
    async def test_teardown_runs_on_aclose(self):
        events: list[str] = []

        @injectable()
        def resource():
            events.append("open")
            yield "res"
            events.append("close")

        c = DIContainer()
        c.register(resource)
        c.compile()

        await c.resolve(resource)

        # The wrapper is stored in the cache; retrieve it directly to call aclose.
        wrapper = c._singletons[resource]
        assert isinstance(wrapper, _GeneratorContextWrapper)
        await wrapper.aclose()
        assert events == ["open", "close"]

    @pytest.mark.asyncio
    async def test_aclose_idempotent(self):
        close_count = [0]

        @injectable()
        def res():
            yield "v"
            close_count[0] += 1

        c = DIContainer()
        c.register(res)
        c.compile()

        await c.resolve(res)
        wrapper = c._singletons[res]
        await wrapper.aclose()
        await wrapper.aclose()  # second call must be no-op
        assert close_count[0] == 1

    @pytest.mark.asyncio
    async def test_singleton_returns_same_value_on_repeat_resolve(self):
        call_count = [0]

        @injectable()
        def counter():
            call_count[0] += 1
            yield f"instance-{call_count[0]}"

        c = DIContainer()
        c.register(counter)
        c.compile()

        v1 = await c.resolve(counter)
        v2 = await c.resolve(counter)
        assert v1 == v2 == "instance-1"
        assert call_count[0] == 1

    @pytest.mark.asyncio
    async def test_request_scope_new_instance_per_cache(self):
        call_count = [0]

        @injectable(scope=Scope.REQUEST)
        def req_res():
            call_count[0] += 1
            yield f"req-{call_count[0]}"

        c = DIContainer()
        c.register(req_res)
        c.compile()

        cache1: dict[Any, Any] = {}
        cache2: dict[Any, Any] = {}
        v1 = await c.resolve(req_res, request_cache=cache1)
        v2 = await c.resolve(req_res, request_cache=cache1)  # same cache → same instance
        v3 = await c.resolve(req_res, request_cache=cache2)  # new cache → new instance
        assert v1 == v2 == "req-1"
        assert v3 == "req-2"

    @pytest.mark.asyncio
    async def test_generator_with_di_deps(self):
        @injectable()
        class Config:
            def __init__(self) -> None:
                self.dsn = "sqlite://test"

        @injectable()
        def conn(cfg: Config) -> Any:
            yield f"connected:{cfg.dsn}"

        c = DIContainer()
        c.register(Config)
        c.register(conn)
        c.compile()

        value = await c.resolve(conn)
        assert value == "connected:sqlite://test"

    @pytest.mark.asyncio
    async def test_exception_in_setup_propagates(self):
        @injectable()
        def bad_setup():
            raise RuntimeError("setup failed")
            yield "never"

        c = DIContainer()
        c.register(bad_setup)
        c.compile()

        with pytest.raises(RuntimeError, match="setup failed"):
            await c.resolve(bad_setup)

    @pytest.mark.asyncio
    async def test_transient_scope_raises_startup_error(self):
        with pytest.raises(StartupError, match="TRANSIENT"):

            @injectable(scope=Scope.TRANSIENT)
            def transient_gen():
                yield "v"

            c = DIContainer()
            c.register(transient_gen)


# ---------------------------------------------------------------------------
# Async generator provider
# ---------------------------------------------------------------------------


class TestAsyncGeneratorProvider:
    @pytest.mark.asyncio
    async def test_yields_value_to_caller(self):
        @injectable()
        async def async_db():
            yield "async-conn"

        c = DIContainer()
        c.register(async_db)
        c.compile()

        value = await c.resolve(async_db)
        assert value == "async-conn"

    @pytest.mark.asyncio
    async def test_teardown_runs_on_aclose(self):
        events: list[str] = []

        @injectable()
        async def async_res():
            events.append("open")
            yield "r"
            events.append("close")

        c = DIContainer()
        c.register(async_res)
        c.compile()

        await c.resolve(async_res)
        wrapper = c._singletons[async_res]
        assert isinstance(wrapper, _GeneratorContextWrapper)
        assert wrapper._is_async is True
        await wrapper.aclose()
        assert events == ["open", "close"]

    @pytest.mark.asyncio
    async def test_async_generator_with_finally(self):
        events: list[str] = []

        @injectable()
        async def with_finally():
            events.append("setup")
            try:
                yield "value"
            finally:
                events.append("teardown")

        c = DIContainer()
        c.register(with_finally)
        c.compile()

        await c.resolve(with_finally)
        wrapper = c._singletons[with_finally]
        await wrapper.aclose()
        assert events == ["setup", "teardown"]


# ---------------------------------------------------------------------------
# LifecycleScheduler integration — SINGLETON generator cleanup
# ---------------------------------------------------------------------------


class TestLifecycleSchedulerGeneratorCleanup:
    @pytest.mark.asyncio
    async def test_singleton_teardown_via_run_pre_destruct(self):
        events: list[str] = []

        @injectable()
        def singleton_res():
            events.append("up")
            yield "singleton-val"
            events.append("down")

        c = DIContainer()
        c.register(singleton_res)
        c.compile()

        # Resolve to instantiate and cache
        val = await c.resolve(singleton_res)
        assert val == "singleton-val"
        assert events == ["up"]

        # Scheduler teardown
        scheduler = LifecycleScheduler(c)
        scheduler.compute_order()
        errors = await scheduler.run_pre_destruct(timeout=5.0)

        assert errors == []
        assert events == ["up", "down"]

    @pytest.mark.asyncio
    async def test_singleton_async_teardown_via_run_pre_destruct(self):
        events: list[str] = []

        @injectable()
        async def async_singleton():
            events.append("up")
            yield "aval"
            events.append("down")

        c = DIContainer()
        c.register(async_singleton)
        c.compile()

        await c.resolve(async_singleton)
        assert events == ["up"]

        scheduler = LifecycleScheduler(c)
        scheduler.compute_order()
        errors = await scheduler.run_pre_destruct(timeout=5.0)

        assert errors == []
        assert events == ["up", "down"]

    @pytest.mark.asyncio
    async def test_unresolved_singleton_generator_skipped(self):
        """Generator never resolved → no wrapper in cache → cleanup skips silently."""

        @injectable()
        def unresolved():
            yield "never"

        c = DIContainer()
        c.register(unresolved)
        c.compile()

        # do NOT resolve — wrapper never created
        scheduler = LifecycleScheduler(c)
        scheduler.compute_order()
        errors = await scheduler.run_pre_destruct(timeout=5.0)
        assert errors == []

    @pytest.mark.asyncio
    async def test_teardown_exception_collected_not_raised(self):
        @injectable()
        def bad_teardown():
            yield "ok"
            raise RuntimeError("teardown boom")

        c = DIContainer()
        c.register(bad_teardown)
        c.compile()

        await c.resolve(bad_teardown)

        scheduler = LifecycleScheduler(c)
        scheduler.compute_order()
        errors = await scheduler.run_pre_destruct(timeout=5.0)

        assert len(errors) == 1
        assert "teardown boom" in str(errors[0])


# ---------------------------------------------------------------------------
# Callers always receive the plain yielded value, never the wrapper
# ---------------------------------------------------------------------------


class TestCallerReceivesValue:
    @pytest.mark.asyncio
    async def test_class_dep_on_generator_gets_value(self):
        @injectable()
        def token_factory():
            yield "tok-abc"

        @injectable()
        class Service:
            def __init__(self, tok: Depends[token_factory]) -> None:
                self.tok = tok

        c = DIContainer()
        c.register(token_factory)
        c.register(Service)
        c.compile()

        svc = await c.resolve(Service)
        assert svc.tok == "tok-abc"
        assert not isinstance(svc.tok, _GeneratorContextWrapper)

    @pytest.mark.asyncio
    async def test_wrapper_stays_in_singletons_for_cleanup(self):
        @injectable()
        def my_res():
            yield "v"

        c = DIContainer()
        c.register(my_res)
        c.compile()

        await c.resolve(my_res)
        raw = c._singletons[my_res]
        assert isinstance(raw, _GeneratorContextWrapper)
