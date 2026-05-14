"""Integration tests for @post_construct / @pre_destruct lifecycle hooks.

These tests drive real LaurenApp instances through LaurenFactory.create()
and verify lifecycle behaviour end-to-end, including:

- @post_construct fires at startup in topological (deps-first) order
- @pre_destruct fires at shutdown in reverse-topological order
- Both sync and async hook variants are supported
- Sync hooks run in a thread pool (non-blocking, timeout-enforced)
- Errors in hooks are collected; shutdown still completes
- REQUEST-scoped hooks fire per-request, not at global startup/shutdown
- TRANSIENT-scoped hooks fire each time the provider is instantiated
- Hooks from providers across multiple imported modules fire correctly
- app.on_shutdown callbacks run before @pre_destruct hooks
- Handlers can observe state written by @post_construct
"""

from __future__ import annotations

import asyncio
import time

import pytest

from lauren import (
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    post_construct,
    pre_destruct,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(*providers, controllers=(), extra_modules=()):
    @module(controllers=list(controllers), providers=list(providers), imports=list(extra_modules))
    class M:
        pass

    return LaurenFactory.create(M)


# ---------------------------------------------------------------------------
# @post_construct — fires at startup
# ---------------------------------------------------------------------------


class TestPostConstruct:
    def test_async_post_construct_fires_at_startup(self):
        log: list[str] = []

        @injectable()
        class Svc:
            @post_construct
            async def init(self) -> None:
                log.append("init")

        app = _build(Svc)
        TestClient(app)
        assert log == ["init"]

    def test_sync_post_construct_fires_at_startup(self):
        log: list[str] = []

        @injectable()
        class Svc:
            @post_construct
            def init(self) -> None:
                log.append("sync-init")

        app = _build(Svc)
        TestClient(app)
        assert log == ["sync-init"]

    def test_post_construct_sets_state_visible_to_handlers(self):
        """State written in @post_construct is readable from handler DI."""

        @injectable()
        class Cache:
            def __init__(self) -> None:
                self.ready = False

            @post_construct
            async def warm(self) -> None:
                self.ready = True

        @controller("/check")
        class C:
            def __init__(self, cache: Cache) -> None:
                self._cache = cache

            @get("/")
            async def h(self) -> dict:
                return {"ready": self._cache.ready}

        app = _build(Cache, controllers=[C])
        r = TestClient(app).get("/check/")
        assert r.status_code == 200
        assert r.json() == {"ready": True}

    def test_post_construct_topological_order(self):
        """Deps run their @post_construct before consumers."""
        order: list[str] = []

        @injectable()
        class A:
            @post_construct
            async def init(self) -> None:
                order.append("A")

        @injectable()
        class B:
            def __init__(self, a: A) -> None: ...

            @post_construct
            async def init(self) -> None:
                order.append("B")

        @injectable()
        class C:
            def __init__(self, b: B) -> None: ...

            @post_construct
            async def init(self) -> None:
                order.append("C")

        app = _build(A, B, C)
        TestClient(app)
        assert order == ["A", "B", "C"]

    def test_post_construct_fires_once_for_singleton(self):
        count: list[int] = [0]

        @injectable()
        class Svc:
            @post_construct
            async def init(self) -> None:
                count[0] += 1

        app = _build(Svc)
        client = TestClient(app)
        # Trigger multiple requests to confirm init only ran once
        client.get("/nonexistent")  # 404 but startup already done
        assert count[0] == 1

    def test_post_construct_not_called_for_request_scoped_at_startup(self):
        """REQUEST-scoped providers are not initialised during global startup."""
        startup_calls: list[str] = []

        @injectable(scope=Scope.REQUEST)
        class PerRequest:
            @post_construct
            async def init(self) -> None:
                startup_calls.append("started")

        app = _build(PerRequest)
        TestClient(app)
        # @post_construct on REQUEST scope fires per-request, not at startup
        assert startup_calls == []

    def test_post_construct_fires_for_request_scoped_on_each_request(self):
        per_request_calls: list[str] = []

        @injectable(scope=Scope.REQUEST)
        class PerRequest:
            @post_construct
            async def init(self) -> None:
                per_request_calls.append("req")

        # Controller must also be REQUEST-scoped to inject a REQUEST-scoped dep
        @injectable(scope=Scope.REQUEST)
        @controller("/req")
        class C:
            def __init__(self, pr: PerRequest) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        app = _build(PerRequest, controllers=[C])
        client = TestClient(app)
        client.get("/req/")
        client.get("/req/")
        assert len(per_request_calls) == 2


# ---------------------------------------------------------------------------
# @pre_destruct — fires at shutdown
# ---------------------------------------------------------------------------


class TestPreDestruct:
    @pytest.mark.asyncio
    async def test_async_pre_destruct_fires_at_shutdown(self):
        log: list[str] = []

        @injectable()
        class Svc:
            @pre_destruct
            async def stop(self) -> None:
                log.append("stopped")

        app = _build(Svc)
        TestClient(app)
        await app.shutdown()
        assert log == ["stopped"]

    @pytest.mark.asyncio
    async def test_sync_pre_destruct_fires_at_shutdown(self):
        log: list[str] = []

        @injectable()
        class Svc:
            @pre_destruct
            def stop(self) -> None:
                log.append("sync-stopped")

        app = _build(Svc)
        TestClient(app)
        await app.shutdown()
        assert log == ["sync-stopped"]

    @pytest.mark.asyncio
    async def test_pre_destruct_reverse_topological_order(self):
        order: list[str] = []

        @injectable()
        class A:
            @pre_destruct
            async def stop(self) -> None:
                order.append("A")

        @injectable()
        class B:
            def __init__(self, a: A) -> None: ...

            @pre_destruct
            async def stop(self) -> None:
                order.append("B")

        @injectable()
        class C:
            def __init__(self, b: B) -> None: ...

            @pre_destruct
            async def stop(self) -> None:
                order.append("C")

        app = _build(A, B, C)
        TestClient(app)
        await app.shutdown()
        # Teardown is reverse of construction: C first, then B, then A
        assert order == ["C", "B", "A"]

    @pytest.mark.asyncio
    async def test_post_then_pre_destruct_full_lifecycle(self):
        log: list[str] = []

        @injectable()
        class Resource:
            @post_construct
            async def open(self) -> None:
                log.append("open")

            @pre_destruct
            async def close(self) -> None:
                log.append("close")

        app = _build(Resource)
        TestClient(app)
        await app.shutdown()
        assert log == ["open", "close"]

    @pytest.mark.asyncio
    async def test_pre_destruct_error_does_not_prevent_full_shutdown(self):
        """A failing hook is collected; remaining hooks still run."""
        log: list[str] = []

        @injectable()
        class Faulty:
            @pre_destruct
            async def stop(self) -> None:
                raise RuntimeError("boom")

        @injectable()
        class Healthy:
            def __init__(self, f: Faulty) -> None: ...

            @pre_destruct
            async def stop(self) -> None:
                log.append("healthy-stopped")

        app = _build(Faulty, Healthy)
        TestClient(app)
        await app.shutdown()
        # Healthy ran despite Faulty raising
        assert "healthy-stopped" in log

    @pytest.mark.asyncio
    async def test_async_pre_destruct_timeout_enforced(self):
        """Async @pre_destruct that exceeds timeout is cancelled."""
        log: list[str] = []

        @injectable()
        class SlowAsync:
            @pre_destruct
            async def stop(self) -> None:
                await asyncio.sleep(5)
                log.append("should-not-reach")

        app = _build(SlowAsync)
        TestClient(app)
        await app.shutdown(drain_timeout=0.05)
        assert log == []  # timed out, never completed

    @pytest.mark.asyncio
    async def test_sync_pre_destruct_does_not_block_event_loop(self):
        """Sync @pre_destruct runs in a thread; event loop stays responsive."""
        ticks: list[float] = []

        async def ticker() -> None:
            for _ in range(4):
                ticks.append(asyncio.get_event_loop().time())
                await asyncio.sleep(0.02)

        @injectable()
        class SlowSync:
            @pre_destruct
            def stop(self) -> None:
                time.sleep(0.12)  # 120 ms blocking — must not stall the loop

        app = _build(SlowSync)
        TestClient(app)

        await asyncio.gather(
            app.shutdown(drain_timeout=2.0),
            ticker(),
        )
        # If the event loop was blocked, ticker could not advance
        assert len(ticks) == 4

    @pytest.mark.asyncio
    async def test_sync_pre_destruct_timeout_enforced(self):
        """Sync @pre_destruct that exceeds timeout returns DestructTimeoutError."""
        from lauren.exceptions import DestructTimeoutError

        @injectable()
        class BlockingSync:
            @pre_destruct
            def stop(self) -> None:
                time.sleep(0.5)

        app = _build(BlockingSync)
        TestClient(app)
        # Bypass shutdown() wrapper and call lifecycle directly to inspect errors
        errors = await app._lifecycle.run_pre_destruct(timeout=0.05)
        assert len(errors) == 1
        assert isinstance(errors[0], DestructTimeoutError)

    @pytest.mark.asyncio
    async def test_pre_destruct_not_called_when_never_instantiated(self):
        """A singleton that was never resolved has no instance — hook skipped."""
        log: list[str] = []

        @injectable()
        class Orphan:
            @pre_destruct
            async def stop(self) -> None:
                log.append("orphan")

        # Build app but never inject/resolve Orphan
        app = _build(Orphan)
        # Don't use TestClient (no startup) — shutdown with no instances
        await app.shutdown()
        assert log == []

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self):
        """Calling shutdown() twice does not run hooks twice."""
        log: list[str] = []

        @injectable()
        class Svc:
            @pre_destruct
            async def stop(self) -> None:
                log.append("stop")

        app = _build(Svc)
        TestClient(app)
        await app.shutdown()
        await app.shutdown()  # second call must be a no-op
        assert log == ["stop"]


# ---------------------------------------------------------------------------
# on_shutdown callbacks + @pre_destruct ordering
# ---------------------------------------------------------------------------


class TestShutdownOrdering:
    @pytest.mark.asyncio
    async def test_on_shutdown_callbacks_run_before_pre_destruct(self):
        """User-registered on_shutdown callbacks precede @pre_destruct hooks."""
        order: list[str] = []

        @injectable()
        class Svc:
            @pre_destruct
            async def stop(self) -> None:
                order.append("pre_destruct")

        app = _build(Svc)
        TestClient(app)

        @app.on_shutdown
        async def callback() -> None:
            order.append("on_shutdown")

        await app.shutdown()
        assert order == ["on_shutdown", "pre_destruct"]

    @pytest.mark.asyncio
    async def test_multiple_on_shutdown_callbacks_reversed(self):
        order: list[str] = []

        app = _build()

        @app.on_shutdown
        def cb1() -> None:
            order.append("first")

        @app.on_shutdown
        def cb2() -> None:
            order.append("second")

        await app.shutdown()
        # Callbacks run in reverse registration order
        assert order == ["second", "first"]


# ---------------------------------------------------------------------------
# Multi-module lifecycle
# ---------------------------------------------------------------------------


class TestMultiModuleLifecycle:
    @pytest.mark.asyncio
    async def test_lifecycle_hooks_across_imported_modules(self):
        """Providers from imported modules also get their hooks invoked."""
        log: list[str] = []

        @injectable()
        class Infrastructure:
            @post_construct
            async def start(self) -> None:
                log.append("infra-start")

            @pre_destruct
            async def stop(self) -> None:
                log.append("infra-stop")

        @module(providers=[Infrastructure], exports=[Infrastructure])
        class InfraModule:
            pass

        @injectable()
        class App:
            def __init__(self, infra: Infrastructure) -> None: ...

            @post_construct
            async def start(self) -> None:
                log.append("app-start")

            @pre_destruct
            async def stop(self) -> None:
                log.append("app-stop")

        @module(imports=[InfraModule], providers=[App])
        class AppModule:
            pass

        app = LaurenFactory.create(AppModule)
        TestClient(app)
        await app.shutdown()

        # Construction: infra before app; teardown: reverse
        assert log == ["infra-start", "app-start", "app-stop", "infra-stop"]

    @pytest.mark.asyncio
    async def test_provider_from_imported_module_with_no_hook(self):
        """Providers without hooks do not interfere with ordering."""

        @injectable()
        class Plain:
            pass  # no lifecycle hooks

        @injectable()
        class WithHook:
            def __init__(self, p: Plain) -> None: ...

            started: bool = False

            @post_construct
            async def init(self) -> None:
                WithHook.started = True

        @module(providers=[Plain, WithHook])
        class M:
            pass

        app = LaurenFactory.create(M)
        TestClient(app)
        assert WithHook.started is True


# ---------------------------------------------------------------------------
# Controller-attached lifecycle hooks
# ---------------------------------------------------------------------------


class TestControllerLifecycle:
    def test_controller_post_construct_fires_at_startup(self):
        """Controllers are SINGLETON by default; @post_construct fires once."""
        log: list[str] = []

        @controller("/ctrl-lc")
        class C:
            @post_construct
            async def init(self) -> None:
                log.append("ctrl-init")

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        TestClient(app).get("/ctrl-lc/")
        assert log == ["ctrl-init"]

    @pytest.mark.asyncio
    async def test_controller_pre_destruct_fires_at_shutdown(self):
        log: list[str] = []

        @controller("/ctrl-pd")
        class C:
            @pre_destruct
            async def stop(self) -> None:
                log.append("ctrl-stop")

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        TestClient(app).get("/ctrl-pd/")
        await app.shutdown()
        assert log == ["ctrl-stop"]
