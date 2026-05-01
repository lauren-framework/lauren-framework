"""Integration tests for the interceptor mechanism.

End-to-end tests using ``httpx.AsyncClient`` + ``ASGITransport`` to drive
real ``LaurenApp`` instances. Covers the full pipeline including DI, global
interceptors, error transformation, response enrichment, and all placement
combinations.

NOTE: intentionally NOT using ``from __future__ import annotations`` so
class references in type hints are evaluated at definition time.
"""

from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

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
from lauren.exceptions import HTTPError
from lauren.types import CallHandler, ExecutionContext


class NotFoundError(HTTPError):
    status_code = 404
    code = "not_found"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build(ctrl_cls: type, providers: list | None = None) -> "LaurenApp":  # type: ignore[name-defined]
    @module(controllers=[ctrl_cls], providers=providers or [])
    class M:
        pass

    return LaurenFactory.create(M)


@pytest_asyncio.fixture
async def client_factory():
    """Return an async function that creates a TestClient for a given app."""
    clients: list[httpx.AsyncClient] = []

    async def factory(app: Any) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await c.__aenter__()
        clients.append(c)
        return c

    yield factory

    for c in clients:
        await c.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Basic interception
# ---------------------------------------------------------------------------


class TestBasicInterception:
    @pytest.mark.asyncio
    async def test_result_enrichment(self, client_factory):
        @interceptor()
        class AddTimestamp:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                result = await ch.handle()
                if isinstance(result, dict):
                    result["_ts"] = 42
                return result

        @use_interceptors(AddTimestamp)
        @controller("/items")
        class C:
            @get("/{id}")
            async def get_item(self, id: Path[int]) -> dict:
                return {"id": id}

        app = _build(C)
        c = await client_factory(app)
        r = await c.get("/items/7")
        assert r.status_code == 200
        assert r.json() == {"id": 7, "_ts": 42}

    @pytest.mark.asyncio
    async def test_short_circuit_returns_cached(self, client_factory):
        call_count = [0]

        @interceptor()
        class CacheHit:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                return {"cached": True}  # never calls handle()

        @use_interceptors(CacheHit)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                call_count[0] += 1
                return {"cached": False}

        app = _build(C)
        c = await client_factory(app)
        r = await c.get("/c/")
        assert r.json() == {"cached": True}
        assert call_count[0] == 0

    @pytest.mark.asyncio
    async def test_error_transformed_to_200(self, client_factory):
        @interceptor()
        class Rescue:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                try:
                    return await ch.handle()
                except NotFoundError:
                    return {"fallback": True}

        @use_interceptors(Rescue)
        @controller("/c")
        class C:
            @get("/{id}")
            async def h(self, id: Path[int]) -> dict:
                raise NotFoundError("not found")

        app = _build(C)
        c = await client_factory(app)
        r = await c.get("/c/5")
        assert r.status_code == 200
        assert r.json() == {"fallback": True}

    @pytest.mark.asyncio
    async def test_interceptor_injects_response_header(self, client_factory):
        from lauren.types import Response

        @interceptor()
        class AddHeader:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                result = await ch.handle()
                if isinstance(result, Response):
                    return result.with_header("x-intercepted", "yes")
                return result

        @use_interceptors(AddHeader)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"ok": True}

        app = _build(C)
        c = await client_factory(app)
        r = await c.get("/c/")
        # The dict return is coerced to a Response AFTER interceptors, so
        # the interceptor sees the raw dict.  Verify the header path by
        # returning a Response directly from the handler:
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_interceptor_on_response_object(self, client_factory):
        from lauren.types import Response

        @interceptor()
        class AddHeader:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                result = await ch.handle()
                if isinstance(result, Response):
                    return result.with_header("x-intercepted", "yes")
                return result

        @use_interceptors(AddHeader)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> Any:
                return Response.json({"ok": True})

        app = _build(C)
        c = await client_factory(app)
        r = await c.get("/c/")
        assert r.headers.get("x-intercepted") == "yes"
        assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Multiple interceptors — onion order
# ---------------------------------------------------------------------------


class TestMultipleInterceptorOrder:
    @pytest.mark.asyncio
    async def test_three_interceptors_wrap_correctly(self, client_factory):
        log: list[str] = []

        @interceptor()
        class A:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                log.append("A-pre")
                r = await ch.handle()
                log.append("A-post")
                return r

        @interceptor()
        class B:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                log.append("B-pre")
                r = await ch.handle()
                log.append("B-post")
                return r

        @interceptor()
        class C_inter:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                log.append("C-pre")
                r = await ch.handle()
                log.append("C-post")
                return r

        @use_interceptors(A, B, C_inter)
        @controller("/c")
        class Ctrl:
            @get("/")
            async def h(self) -> dict:
                log.append("H")
                return {}

        app = _build(Ctrl)
        c = await client_factory(app)
        await c.get("/c/")
        assert log == ["A-pre", "B-pre", "C-pre", "H", "C-post", "B-post", "A-post"]

    @pytest.mark.asyncio
    async def test_result_passes_through_chain(self, client_factory):
        @interceptor()
        class Multiply:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                result = await ch.handle()
                result["n"] *= 10
                return result

        @interceptor()
        class AddOne:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                result = await ch.handle()
                result["n"] += 1
                return result

        # Multiply is outer, AddOne is inner.
        # Handler returns {"n": 5}.
        # AddOne runs first on return: {"n": 6}.
        # Multiply runs second: {"n": 60}.
        @use_interceptors(Multiply, AddOne)
        @controller("/c")
        class Ctrl:
            @get("/")
            async def h(self) -> dict:
                return {"n": 5}

        app = _build(Ctrl)
        c = await client_factory(app)
        r = await c.get("/c/")
        assert r.json() == {"n": 60}


# ---------------------------------------------------------------------------
# Global interceptors
# ---------------------------------------------------------------------------


class TestGlobalInterceptors:
    @pytest.mark.asyncio
    async def test_global_interceptor_runs_on_all_routes(self, client_factory):
        calls: list[str] = []

        @interceptor()
        class GlobalLogger:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                calls.append(f"global:{ctx.route_template}")
                return await ch.handle()

        @controller("/a")
        class A:
            @get("/")
            async def a(self) -> dict:
                return {"route": "a"}

        @controller("/b")
        class B:
            @get("/")
            async def b(self) -> dict:
                return {"route": "b"}

        @module(controllers=[A, B])
        class M:
            pass

        app = LaurenFactory.create(M, global_interceptors=[GlobalLogger])
        c = await client_factory(app)
        await c.get("/a/")
        await c.get("/b/")
        assert "/a" in calls[0]
        assert "/b" in calls[1]

    @pytest.mark.asyncio
    async def test_global_before_local_interceptor(self, client_factory):
        log: list[str] = []

        @interceptor()
        class GlobalI:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                log.append("global-pre")
                r = await ch.handle()
                log.append("global-post")
                return r

        @interceptor()
        class LocalI:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                log.append("local-pre")
                r = await ch.handle()
                log.append("local-post")
                return r

        @use_interceptors(LocalI)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                log.append("H")
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M, global_interceptors=[GlobalI])
        c = await client_factory(app)
        await c.get("/c/")
        assert log == ["global-pre", "local-pre", "H", "local-post", "global-post"]


# ---------------------------------------------------------------------------
# DI-injected interceptors
# ---------------------------------------------------------------------------


class TestDIInterceptorIntegration:
    @pytest.mark.asyncio
    async def test_singleton_interceptor_shared_across_requests(self, client_factory):
        @injectable()
        class RequestLog:
            def __init__(self) -> None:
                self.entries: list[str] = []

        @interceptor()
        @injectable()
        class LoggingInterceptor:
            def __init__(self, log: RequestLog) -> None:
                self._log = log

            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                self._log.entries.append(ctx.route_template)
                return await ch.handle()

        @use_interceptors(LoggingInterceptor)
        @controller("/c")
        class C:
            @get("/{id}")
            async def h(self, id: Path[int]) -> dict:
                return {"id": id}

        @module(controllers=[C], providers=[RequestLog, LoggingInterceptor])
        class M:
            pass

        app = LaurenFactory.create(M)
        c = await client_factory(app)
        await c.get("/c/1")
        await c.get("/c/2")

        log = app.container._singletons.get(RequestLog)
        assert log is not None
        assert len(log.entries) == 2

    @pytest.mark.asyncio
    async def test_request_scoped_interceptor(self, client_factory):
        from lauren.types import Scope

        call_ids: list[int] = []

        @interceptor()
        @injectable(scope=Scope.REQUEST)
        class RequestScopedInterceptor:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                call_ids.append(id(self))
                return await ch.handle()

        @use_interceptors(RequestScopedInterceptor)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C], providers=[RequestScopedInterceptor])
        class M:
            pass

        app = LaurenFactory.create(M)
        c = await client_factory(app)
        await c.get("/c/")
        await c.get("/c/")

        # Each request should get a fresh interceptor instance.
        assert len(call_ids) == 2
        assert call_ids[0] != call_ids[1]


# ---------------------------------------------------------------------------
# ExecutionContext integrity
# ---------------------------------------------------------------------------


class TestExecutionContextIntegrity:
    @pytest.mark.asyncio
    async def test_ctx_has_correct_handler_class_and_func(self, client_factory):
        ctxs: list[ExecutionContext] = []

        @interceptor()
        class CtxCapture:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                ctxs.append(ctx)
                return await ch.handle()

        @use_interceptors(CtxCapture)
        @controller("/things")
        class ThingsController:
            @get("/{thing_id}")
            async def get_thing(self, thing_id: Path[int]) -> dict:
                return {"thing": thing_id}

        app = _build(ThingsController)
        c = await client_factory(app)
        await c.get("/things/99")

        assert len(ctxs) == 1
        ctx = ctxs[0]
        assert ctx.handler_class is ThingsController
        assert ctx.handler_func.__name__ == "get_thing"
        assert ctx.route_template == "/things/{thing_id}"

    @pytest.mark.asyncio
    async def test_ctx_metadata_from_set_metadata(self, client_factory):
        metas: list[Any] = []

        @interceptor()
        class MetaReader:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                metas.append(ctx.get_metadata("cache_ttl", None))
                return await ch.handle()

        @use_interceptors(MetaReader)
        @controller("/c")
        class C:
            @set_metadata("cache_ttl", 300)
            @get("/")
            async def h(self) -> dict:
                return {}

        app = _build(C)
        c = await client_factory(app)
        await c.get("/c/")
        assert metas == [300]


# ---------------------------------------------------------------------------
# Interceptors with guards and middleware
# ---------------------------------------------------------------------------


class TestInterceptorWithGuardsAndMiddleware:
    @pytest.mark.asyncio
    async def test_full_pipeline_order(self, client_factory):
        log: list[str] = []

        from lauren import middleware
        from lauren.types import CallNext, Request, Response

        @middleware
        class MW:
            async def dispatch(self, req: Request, call_next: CallNext) -> Response:
                log.append("mw-in")
                r = await call_next(req)
                log.append("mw-out")
                return r

        class AllowGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                log.append("guard")
                return True

        @interceptor()
        class Inter:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                log.append("inter-in")
                r = await ch.handle()
                log.append("inter-out")
                return r

        @use_middlewares(MW)
        @use_guards(AllowGuard)
        @use_interceptors(Inter)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                log.append("handler")
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        c = await client_factory(app)
        await c.get("/c/")
        assert log == ["mw-in", "guard", "inter-in", "handler", "inter-out", "mw-out"]

    @pytest.mark.asyncio
    async def test_guard_deny_skips_interceptor_and_handler(self, client_factory):
        log: list[str] = []

        class DenyGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                log.append("guard-deny")
                return False

        @interceptor()
        class ShouldNotRun:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                log.append("interceptor")  # must not appear
                return await ch.handle()

        @use_guards(DenyGuard)
        @use_interceptors(ShouldNotRun)
        @controller("/c")
        class C:
            @get("/")
            async def h(self) -> dict:
                log.append("handler")  # must not appear
                return {}

        app = _build(C)
        c = await client_factory(app)
        r = await c.get("/c/")
        assert r.status_code == 403
        assert log == ["guard-deny"]


# ---------------------------------------------------------------------------
# Method-level vs controller-level scoping
# ---------------------------------------------------------------------------


class TestInterceptorScoping:
    @pytest.mark.asyncio
    async def test_method_level_interceptor_only_on_that_route(self, client_factory):
        calls: list[str] = []

        @interceptor()
        class MethodOnly:
            async def intercept(self, ctx, ch: CallHandler) -> Any:
                calls.append("intercepted")
                return await ch.handle()

        @controller("/c")
        class C:
            @use_interceptors(MethodOnly)
            @get("/a")
            async def a(self) -> dict:
                return {"route": "a"}

            @get("/b")
            async def b(self) -> dict:
                return {"route": "b"}

        app = _build(C)
        c = await client_factory(app)
        await c.get("/c/b")
        assert calls == []
        await c.get("/c/a")
        assert calls == ["intercepted"]

    @pytest.mark.asyncio
    async def test_controller_level_interceptor_on_all_routes(self, client_factory):
        calls: list[tuple[str, str]] = []

        @interceptor()
        class CtrlLevel:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                calls.append(("intercepted", ctx.route_template))
                return await ch.handle()

        @use_interceptors(CtrlLevel)
        @controller("/c")
        class C:
            @get("/a")
            async def a(self) -> dict:
                return {"route": "a"}

            @get("/b")
            async def b(self) -> dict:
                return {"route": "b"}

        app = _build(C)
        c = await client_factory(app)
        await c.get("/c/a")
        await c.get("/c/b")
        routes = [r for _, r in calls]
        assert "/c/a" in routes
        assert "/c/b" in routes
