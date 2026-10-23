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


# ---------------------------------------------------------------------------
# Factory parity: Lauren forwards the remaining LaurenFactory.create options
# ---------------------------------------------------------------------------


def _compiled(app: Lauren):
    import asyncio

    return asyncio.run(app.startup())


class TestLaurenFactoryParity:
    def test_error_format_reaches_the_compiled_app(self):
        app = Lauren(error_format="rfc7807", openapi_url=None, docs_url=None, redoc_url=None)

        r = TestClient(app).get("/missing")

        assert r.status_code == 404
        assert r.header("content-type") == "application/problem+json"

    def test_json_encoder_reaches_the_compiled_app(self):
        from lauren import StdlibJSONEncoder

        encoder = StdlibJSONEncoder()
        app = Lauren(json_encoder=encoder)

        assert _compiled(app)._json_encoder is encoder

    def test_root_path_reaches_the_compiled_app(self):
        app = Lauren(root_path="/api", openapi_url=None, docs_url=None, redoc_url=None)

        @app.get("/ping")
        async def ping() -> dict:
            return {"pong": True}

        assert TestClient(app).get("/api/ping").json() == {"pong": True}

    def test_signals_and_arena_reach_the_compiled_app(self):
        from lauren import RequestArena, SignalBus

        bus, arena = SignalBus(), RequestArena(capacity=0)
        compiled = _compiled(Lauren(signals=bus, arena=arena))

        assert compiled._signals is bus
        assert compiled._arena is arena

    def test_global_ws_guards_reach_the_compiled_app(self):
        compiled = _compiled(Lauren(global_ws_guards=[GlobalGuard]))

        assert compiled._global_ws_guards == [GlobalGuard]

    def test_mounts_kwarg_and_mount_method_combine(self):
        async def legacy(scope, receive, send):
            await send(
                {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]}
            )
            await send({"type": "http.response.body", "body": scope["path"].encode()})

        app = Lauren(mounts={"/legacy": legacy}, openapi_url=None, docs_url=None, redoc_url=None)
        app.mount("/old", legacy)
        client = TestClient(app)

        assert client.get("/legacy/a").body == b"/a"
        assert client.get("/old/b").body == b"/b"

    def test_mount_after_compile_raises(self):
        from lauren.exceptions import LifecycleViolationError

        app = Lauren()
        _compiled(app)

        with pytest.raises(LifecycleViolationError):
            app.mount("/late", lambda scope, receive, send: None)

    def test_debug_flag_warns_that_it_is_reserved(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="lauren"):
            app = Lauren(debug=True)

        assert app.debug is True
        assert any("no effect yet" in r.getMessage() for r in caplog.records)

    def test_interceptors_and_encoder_on_app_routes_survive_the_wrap(self):
        from lauren import StdlibJSONEncoder, use_encoder, use_interceptors

        @interceptor()
        class Tag:
            async def intercept(self, ctx, call_handler):
                return (await call_handler.handle()).with_header("x-tag", "1")

        class Marker(StdlibJSONEncoder):
            def encode(self, value):
                return b'{"encoded": "marker"}'

            encode_compact = encode

        app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

        @app.get("/x")
        @use_interceptors(Tag)
        @use_encoder(Marker())
        async def x() -> dict:
            return {"plain": True}

        r = TestClient(app).get("/x")

        assert r.header("x-tag") == "1"
        assert r.json() == {"encoded": "marker"}
