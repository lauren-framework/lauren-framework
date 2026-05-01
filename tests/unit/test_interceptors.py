"""Unit tests for the interceptor mechanism.

Covers:

* ``@interceptor()`` decorator — marks a class, validates ``intercept`` presence,
  auto-marks as injectable.
* ``@use_interceptors()`` — attaches to controllers and handler methods.
* Execution order: global → controller → method.
* Interceptors can transform the handler result.
* Interceptors can short-circuit (return early without calling ``handle()``).
* Interceptors can catch and transform exceptions from the handler.
* Multiple interceptors form a correct onion (pre-handler left-to-right,
  post-handler right-to-left).
* ``ExecutionContext`` carries correct route/handler/metadata.
* DI-injected interceptors receive their dependencies.
* ``None`` entries in ``@use_interceptors`` are silently dropped.
* Class-level vs method-level scoping (subclass does not inherit).
* Config-error paths: missing ``intercept`` method, passing non-class.
* Global interceptors declared in ``LaurenFactory.create``.
* Interceptors coexist with guards and middleware.
* Sync handlers work with interceptors.
"""

# NOTE: intentionally NOT using ``from __future__ import annotations`` so
# class references in type hints inside test methods are evaluated immediately.

from typing import Any

import pytest

from lauren import (
    LaurenFactory,
    Path,
    controller,
    get,
    injectable,
    interceptor,
    module,
    set_metadata,
    use_guards,
    use_interceptors,
    use_middlewares,
)
from lauren.exceptions import InterceptorConfigError
from lauren.testing import TestClient
from lauren.types import CallHandler, ExecutionContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app(ctrl_cls: type, providers: list | None = None) -> "TestClient":
    @module(controllers=[ctrl_cls], providers=providers or [])
    class M:
        pass

    return TestClient(LaurenFactory.create(M))


# ---------------------------------------------------------------------------
# @interceptor decorator
# ---------------------------------------------------------------------------


class TestInterceptorDecorator:
    def test_marks_class_with_metadata(self):
        @interceptor()
        class I:
            async def intercept(self, ctx, call_handler):
                return await call_handler.handle()

        assert hasattr(I, "__lauren_interceptor__")

    def test_auto_marks_as_singleton_injectable(self):
        from lauren._di import INJECTABLE_META, InjectableMeta
        from lauren.types import Scope

        @interceptor()
        class I:
            async def intercept(self, ctx, call_handler):
                return await call_handler.handle()

        meta = getattr(I, INJECTABLE_META)
        assert isinstance(meta, InjectableMeta)
        assert meta.scope == Scope.SINGLETON

    def test_raises_without_intercept_method(self):
        with pytest.raises(InterceptorConfigError, match="must define 'intercept'"):

            @interceptor()
            class Bad:
                pass

    def test_existing_injectable_scope_preserved(self):
        from lauren._di import INJECTABLE_META
        from lauren.types import Scope

        @interceptor()
        @injectable(scope=Scope.REQUEST)
        class I:
            async def intercept(self, ctx, call_handler):
                return await call_handler.handle()

        meta = getattr(I, INJECTABLE_META)
        assert meta.scope == Scope.REQUEST


# ---------------------------------------------------------------------------
# @use_interceptors decorator
# ---------------------------------------------------------------------------


class TestUseInterceptorsDecorator:
    def test_attaches_to_controller(self):
        @interceptor()
        class I:
            async def intercept(self, ctx, call_handler):
                return await call_handler.handle()

        @use_interceptors(I)
        @controller("/x")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        assert I in C.__dict__.get("__lauren_use_interceptors__", [])

    def test_attaches_to_method(self):
        @interceptor()
        class I:
            async def intercept(self, ctx, call_handler):
                return await call_handler.handle()

        @controller("/x")
        class C:
            @use_interceptors(I)
            @get("/")
            async def h(self) -> dict:
                return {}

        assert I in C.h.__lauren_use_interceptors__

    def test_none_entries_dropped(self):
        @interceptor()
        class I:
            async def intercept(self, ctx, call_handler):
                return await call_handler.handle()

        @use_interceptors(I, None)
        @controller("/x")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        assert list(C.__dict__.get("__lauren_use_interceptors__", [])) == [I]

    def test_raises_without_intercept_method(self):
        class NotAnInterceptor:
            pass

        with pytest.raises(InterceptorConfigError, match="must define 'intercept'"):
            use_interceptors(NotAnInterceptor)

    def test_multiple_applications_append(self):
        @interceptor()
        class A:
            async def intercept(self, ctx, ch):
                return await ch.handle()

        @interceptor()
        class B:
            async def intercept(self, ctx, ch):
                return await ch.handle()

        @use_interceptors(A)
        @use_interceptors(B)
        @controller("/x")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        chain = list(C.__dict__.get("__lauren_use_interceptors__", []))
        assert A in chain and B in chain

    def test_subclass_does_not_inherit(self):
        @interceptor()
        class I:
            async def intercept(self, ctx, ch):
                return await ch.handle()

        @use_interceptors(I)
        @controller("/x")
        class Parent:
            @get("/")
            async def h(self) -> dict:
                return {}

        @controller("/y")
        class Child(Parent):
            pass

        assert "__lauren_use_interceptors__" not in Child.__dict__


# ---------------------------------------------------------------------------
# Execution — basic pass-through
# ---------------------------------------------------------------------------


class TestInterceptorExecution:
    @pytest.mark.asyncio
    async def test_passthrough_interceptor(self):
        @interceptor()
        class Noop:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                return await call_handler.handle()

        @use_interceptors(Noop)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"ok": True}

        r = _app(C).get("/c/")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    @pytest.mark.asyncio
    async def test_interceptor_transforms_result(self):
        @interceptor()
        class Wrapper:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                result = await call_handler.handle()
                return {"wrapped": True, "data": result}

        @use_interceptors(Wrapper)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"original": True}

        r = _app(C).get("/c/")
        assert r.json() == {"wrapped": True, "data": {"original": True}}

    @pytest.mark.asyncio
    async def test_interceptor_short_circuits(self):
        @interceptor()
        class ShortCircuit:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                # Never calls handle() — handler never runs.
                return {"short": "circuited"}

        @use_interceptors(ShortCircuit)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                raise RuntimeError("should never be called")

        r = _app(C).get("/c/")
        assert r.status_code == 200
        assert r.json() == {"short": "circuited"}

    @pytest.mark.asyncio
    async def test_interceptor_catches_handler_exception(self):
        from lauren.exceptions import HTTPError

        class NotFoundError(HTTPError):
            status_code = 404

        @interceptor()
        class ErrorCatcher:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                try:
                    return await call_handler.handle()
                except HTTPError:
                    return {"caught": True}

        @use_interceptors(ErrorCatcher)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                raise NotFoundError("gone")

        r = _app(C).get("/c/")
        assert r.status_code == 200
        assert r.json() == {"caught": True}

    @pytest.mark.asyncio
    async def test_interceptor_receives_correct_execution_context(self):
        captured: list[ExecutionContext] = []

        @interceptor()
        class Spy:
            async def intercept(
                self, ctx: ExecutionContext, call_handler: CallHandler
            ) -> Any:
                captured.append(ctx)
                return await call_handler.handle()

        @use_interceptors(Spy)
        @controller("/items")
        class C:
            @get("/{item_id}")
            async def h(self, item_id: Path[int]) -> dict:
                return {"id": item_id}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        TestClient(app).get("/items/42")

        assert len(captured) == 1
        ctx = captured[0]
        assert ctx.handler_class is C
        assert ctx.route_template == "/items/{item_id}"
        assert ctx.request is not None

    @pytest.mark.asyncio
    async def test_interceptor_reads_set_metadata(self):
        captured_meta: list[Any] = []

        @interceptor()
        class MetaSpy:
            async def intercept(
                self, ctx: ExecutionContext, call_handler: CallHandler
            ) -> Any:
                captured_meta.append(ctx.get_metadata("role", None))
                return await call_handler.handle()

        @use_interceptors(MetaSpy)
        @controller("/c")
        class C:
            @set_metadata("role", "admin")
            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        TestClient(LaurenFactory.create(M)).get("/c/")
        assert captured_meta == ["admin"]


# ---------------------------------------------------------------------------
# Execution order — onion model
# ---------------------------------------------------------------------------


class TestInterceptorOrder:
    @pytest.mark.asyncio
    async def test_single_interceptor_pre_and_post(self):
        events: list[str] = []

        @interceptor()
        class Tracer:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("pre")
                result = await call_handler.handle()
                events.append("post")
                return result

        @use_interceptors(Tracer)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                events.append("handler")
                return {}

        _app(C).get("/c/")
        assert events == ["pre", "handler", "post"]

    @pytest.mark.asyncio
    async def test_multiple_interceptors_onion_order(self):
        """Declared left-to-right: outer → inner. Post-handler: inner → outer."""
        events: list[str] = []

        @interceptor()
        class Outer:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("outer-pre")
                result = await call_handler.handle()
                events.append("outer-post")
                return result

        @interceptor()
        class Inner:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("inner-pre")
                result = await call_handler.handle()
                events.append("inner-post")
                return result

        @use_interceptors(Outer, Inner)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                events.append("handler")
                return {}

        _app(C).get("/c/")
        assert events == [
            "outer-pre",
            "inner-pre",
            "handler",
            "inner-post",
            "outer-post",
        ]

    @pytest.mark.asyncio
    async def test_controller_level_before_method_level(self):
        events: list[str] = []

        @interceptor()
        class ClassLevel:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("class-pre")
                r = await call_handler.handle()
                events.append("class-post")
                return r

        @interceptor()
        class MethodLevel:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("method-pre")
                r = await call_handler.handle()
                events.append("method-post")
                return r

        @use_interceptors(ClassLevel)
        @controller("/c")
        class C:
            @use_interceptors(MethodLevel)
            @get("/")
            async def h(self) -> dict:
                events.append("handler")
                return {}

        _app(C).get("/c/")
        assert events == [
            "class-pre",
            "method-pre",
            "handler",
            "method-post",
            "class-post",
        ]

    @pytest.mark.asyncio
    async def test_global_interceptor_outermost(self):
        events: list[str] = []

        @interceptor()
        class GlobalI:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("global-pre")
                r = await call_handler.handle()
                events.append("global-post")
                return r

        @interceptor()
        class LocalI:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("local-pre")
                r = await call_handler.handle()
                events.append("local-post")
                return r

        @use_interceptors(LocalI)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                events.append("handler")
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M, global_interceptors=[GlobalI])
        TestClient(app).get("/c/")
        assert events == [
            "global-pre",
            "local-pre",
            "handler",
            "local-post",
            "global-post",
        ]


# ---------------------------------------------------------------------------
# DI-injected interceptors
# ---------------------------------------------------------------------------


class TestDIInterceptors:
    @pytest.mark.asyncio
    async def test_injectable_interceptor_receives_dependency(self):
        @injectable()
        class Counter:
            def __init__(self) -> None:
                self.count = 0

            def increment(self) -> None:
                self.count += 1

        @interceptor()
        @injectable()
        class CountingInterceptor:
            def __init__(self, counter: Counter) -> None:
                self._counter = counter

            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                self._counter.increment()
                return await call_handler.handle()

        @use_interceptors(CountingInterceptor)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C], providers=[Counter, CountingInterceptor])
        class M:
            pass

        app = LaurenFactory.create(M)
        client = TestClient(app)
        client.get("/c/")
        client.get("/c/")

        counter = app.container._singletons.get(Counter)
        assert counter is not None
        assert counter.count == 2


# ---------------------------------------------------------------------------
# Interceptors + guards + middleware coexistence
# ---------------------------------------------------------------------------


class TestInterceptorCoexistence:
    @pytest.mark.asyncio
    async def test_middleware_guard_interceptor_order(self):
        """Middleware runs first, then guards, then interceptors, then handler."""
        events: list[str] = []

        from lauren import middleware
        from lauren.types import CallNext, Request, Response

        @middleware
        class MW:
            async def dispatch(self, request: Request, call_next: CallNext) -> Response:
                events.append("mw-pre")
                r = await call_next(request)
                events.append("mw-post")
                return r

        class Guard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                events.append("guard")
                return True

        @interceptor()
        class Inter:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("inter-pre")
                r = await call_handler.handle()
                events.append("inter-post")
                return r

        @use_middlewares(MW)
        @use_guards(Guard)
        @use_interceptors(Inter)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                events.append("handler")
                return {}

        @module(controllers=[C])
        class M:
            pass

        TestClient(LaurenFactory.create(M)).get("/c/")
        assert events == [
            "mw-pre",
            "guard",
            "inter-pre",
            "handler",
            "inter-post",
            "mw-post",
        ]

    @pytest.mark.asyncio
    async def test_guard_denial_skips_interceptor(self):
        """When a guard denies, interceptors never run."""
        calls: list[str] = []

        class DenyGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                calls.append("guard")
                return False

        @interceptor()
        class I:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                calls.append("interceptor")  # should never run
                return await call_handler.handle()

        @use_guards(DenyGuard)
        @use_interceptors(I)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                calls.append("handler")
                return {}

        r = _app(C).get("/c/")
        assert r.status_code == 403
        assert calls == ["guard"]

    @pytest.mark.asyncio
    async def test_interceptor_with_sync_handler(self):
        events: list[str] = []

        @interceptor()
        class Wrap:
            async def intercept(self, ctx, call_handler: CallHandler) -> Any:
                events.append("pre")
                r = await call_handler.handle()
                events.append("post")
                return r

        @use_interceptors(Wrap)
        @controller("/c")
        class C:
            @get("/")
            def h(self) -> dict:  # sync handler
                events.append("sync-handler")
                return {"sync": True}

        r = _app(C).get("/c/")
        assert r.status_code == 200
        assert r.json() == {"sync": True}
        assert events == ["pre", "sync-handler", "post"]
