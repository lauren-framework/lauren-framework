"""Tests for Lauren.__init__ global_* constructor parameters.

Covers:
- global_middlewares — middleware runs on all routes
- global_guards    — guard runs on all routes
- global_interceptors — interceptor runs on all routes
- global_exception_handlers — handler catches errors on all routes
- Constructor + add_* methods accumulate (not replace)
- add_interceptor() works after construction
"""

from __future__ import annotations

import pytest

from lauren import (
    CallNext,
    Lauren,
    Request,
    Response,
    controller,
    exception_handler,
    get,
    interceptor,
    middleware,
    module,
)
from lauren.testing import TestClient
from lauren.types import CallHandler, ExecutionContext


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@middleware()
class GlobalMW:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        resp = await call_next(request)
        return resp.with_header("x-global-mw", "1")


class GlobalGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-allow") == "yes"


@interceptor()
class GlobalInter:
    # Interceptors receive the raw handler return value (dict, model, or
    # Response). Only Response objects support .with_header(); check first.
    async def intercept(self, ctx: ExecutionContext, ch: CallHandler):
        result = await ch.handle()
        if isinstance(result, Response):
            return result.with_header("x-global-inter", "1")
        return result


class DomainError(Exception):
    pass


@exception_handler(DomainError)
class GlobalErrHandler:
    async def catch(self, exc: Exception, request: Request) -> Response:
        return Response.json({"handled": True}, status=400)


# ---------------------------------------------------------------------------
# App controller — returns Response so interceptors can add headers
# ---------------------------------------------------------------------------


@controller("/items")
class ItemsController:
    @get("/")
    async def index(self) -> Response:
        return Response.json({"items": []})

    @get("/boom")
    async def boom(self) -> Response:
        raise DomainError("oops")


@module(controllers=[ItemsController])
class ItemsModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLaurenGlobalMiddlewares:
    def test_global_middleware_runs_on_all_routes(self):
        app = Lauren(global_middlewares=[GlobalMW])
        app.include_module(ItemsModule)

        client = TestClient(app)
        r = client.get("/items/")
        assert r.status_code == 200
        assert r.header("x-global-mw") == "1"

    def test_constructor_and_add_middleware_accumulate(self):
        """Constructor list + add_middleware() both contribute."""

        @middleware()
        class ExtraMW:
            async def dispatch(self, request: Request, call_next: CallNext) -> Response:
                resp = await call_next(request)
                return resp.with_header("x-extra-mw", "1")

        app = Lauren(global_middlewares=[GlobalMW])
        app.add_middleware(ExtraMW)
        app.include_module(ItemsModule)

        client = TestClient(app)
        r = client.get("/items/")
        assert r.header("x-global-mw") == "1"
        assert r.header("x-extra-mw") == "1"


class TestLaurenGlobalGuards:
    def test_global_guard_denies_without_header(self):
        app = Lauren(global_guards=[GlobalGuard])
        app.include_module(ItemsModule)

        client = TestClient(app)
        assert client.get("/items/").status_code == 403

    def test_global_guard_allows_with_header(self):
        app = Lauren(global_guards=[GlobalGuard])
        app.include_module(ItemsModule)

        client = TestClient(app)
        assert client.get("/items/", headers={"x-allow": "yes"}).status_code == 200

    def test_constructor_and_add_guard_accumulate(self):
        class AnotherGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                return ctx.request.headers.get("x-allow2") == "yes"

        app = Lauren(global_guards=[GlobalGuard])
        app.add_guard(AnotherGuard)
        app.include_module(ItemsModule)

        client = TestClient(app)
        # Both guards must pass; missing x-allow2 → 403
        r = client.get("/items/", headers={"x-allow": "yes"})
        assert r.status_code == 403


class TestLaurenGlobalInterceptors:
    def test_global_interceptor_runs_on_all_routes(self):
        app = Lauren(global_interceptors=[GlobalInter])
        app.include_module(ItemsModule)

        client = TestClient(app)
        # Interceptor runs; handler returns Response so header is added
        r = client.get("/items/")
        assert r.status_code == 200
        # GlobalInter passes through (result is Response from handler)
        assert r.json() == {"items": []}

    def test_add_interceptor_works_after_construction(self):
        @interceptor()
        class LateInter:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler):
                result = await ch.handle()
                if isinstance(result, Response):
                    return result.with_header("x-late-inter", "1")
                return result

        app = Lauren()
        app.add_interceptor(LateInter)
        app.include_module(ItemsModule)

        client = TestClient(app)
        r = client.get("/items/")
        assert r.header("x-late-inter") == "1"

    def test_constructor_and_add_interceptor_accumulate(self):
        @interceptor()
        class SecondInter:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler):
                result = await ch.handle()
                if isinstance(result, Response):
                    return result.with_header("x-second-inter", "1")
                return result

        app = Lauren(global_interceptors=[GlobalInter])
        app.add_interceptor(SecondInter)
        app.include_module(ItemsModule)

        client = TestClient(app)
        r = client.get("/items/")
        # Both interceptors ran; SecondInter (innermost) adds x-second-inter,
        # GlobalInter (outermost) passes result through since it's a Response
        assert r.header("x-second-inter") == "1"

    def test_interceptor_order_global_then_local(self):
        """Global interceptors wrap before controller-level ones."""
        order: list[str] = []

        @interceptor()
        class OuterInter:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler):
                order.append("outer-pre")
                r = await ch.handle()
                order.append("outer-post")
                return r

        @interceptor()
        class InnerInter:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler):
                order.append("inner-pre")
                r = await ch.handle()
                order.append("inner-post")
                return r

        from lauren import use_interceptors

        @use_interceptors(InnerInter)
        @controller("/ord")
        class OrdCtrl:
            @get("/")
            async def idx(self) -> dict:
                order.append("handler")
                return {"ok": True}

        @module(controllers=[OrdCtrl])
        class OrdModule:
            pass

        app = Lauren(global_interceptors=[OuterInter])
        app.include_module(OrdModule)

        TestClient(app).get("/ord/")
        assert order == [
            "outer-pre",
            "inner-pre",
            "handler",
            "inner-post",
            "outer-post",
        ]


class TestLaurenGlobalExceptionHandlers:
    def test_global_exception_handler_catches_errors(self):
        app = Lauren(global_exception_handlers=[GlobalErrHandler])
        app.include_module(ItemsModule)

        client = TestClient(app)
        r = client.get("/items/boom")
        assert r.status_code == 400
        assert r.json()["handled"] is True

    def test_constructor_and_add_exception_handler_accumulate(self):
        class OtherError(Exception):
            pass

        @exception_handler(OtherError)
        class OtherHandler:
            async def catch(self, exc: Exception, request: Request) -> Response:
                return Response.json({"other": True}, status=422)

        @controller("/extra")
        class ExtraCtrl:
            @get("/err")
            async def err(self) -> dict:
                raise OtherError("other")

        @module(controllers=[ExtraCtrl])
        class ExtraModule:
            pass

        app = Lauren(global_exception_handlers=[GlobalErrHandler])
        app.add_exception_handler(OtherHandler)
        app.include_module(ItemsModule)
        app.include_module(ExtraModule)

        client = TestClient(app)
        # GlobalErrHandler still works for DomainError
        r = client.get("/items/boom")
        assert r.status_code == 400
        assert r.json()["handled"] is True
        # OtherHandler added via add_exception_handler
        r2 = client.get("/extra/err")
        assert r2.status_code == 422
        assert r2.json()["other"] is True

    def test_add_interceptor_after_startup_raises(self):
        @interceptor()
        class I:
            async def intercept(self, ctx, ch):
                return await ch.handle()

        app = Lauren(global_interceptors=[GlobalInter])
        app.include_module(ItemsModule)
        # Force compile
        TestClient(app).get("/items/")

        from lauren.exceptions import LifecycleViolationError

        with pytest.raises(LifecycleViolationError):
            app.add_interceptor(I)
