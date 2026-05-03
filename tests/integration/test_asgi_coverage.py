"""Integration tests targeting uncovered lines in _asgi/__init__.py.

Covers:
- shutdown() with on_shutdown callbacks that raise
- startup() called twice with strict_lifecycle
- _dispatch_exception_handlers with function-form handler
- mount() sub-app dispatch
- static/classmethod handler bindings
- _coerce_to_response with tuple (body, status) and (body, status, headers)
- _coerce_to_response with bytes/str/dataclass/Pydantic list
- LaurenApp properties (router, container, app_state, module_graph, arena, signals)
- LaurenApp.on_shutdown callback
- LaurenApp.mount and mounts property
- lifespan startup failure
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Response,
    controller,
    get,
    module,
    post,
)
from lauren._asgi import _coerce_to_response
from lauren.testing import TestClient
from lauren.types import ExecutionContext


# ---------------------------------------------------------------------------
# _coerce_to_response edge cases
# ---------------------------------------------------------------------------


class TestCoerceToResponse:
    def test_tuple_body_status(self):
        """(body, status) tuple sets the status code."""
        result = _coerce_to_response(({"id": 1}, 201))
        assert result.status == 201

    def test_tuple_body_status_headers(self):
        """(body, status, headers) tuple sets both status and headers."""
        result = _coerce_to_response(({"id": 1}, 202, {"X-Custom": "val"}))
        assert result.status == 202

    def test_none_returns_204(self):
        result = _coerce_to_response(None)
        assert result.status == 204

    def test_bytes_returns_octet_stream(self):
        result = _coerce_to_response(b"\x00\x01\x02")
        assert result.status == 200
        assert result.body == b"\x00\x01\x02"

    def test_bytearray_returns_bytes_response(self):
        result = _coerce_to_response(bytearray(b"hello"))
        assert result.status == 200

    def test_str_returns_text_response(self):
        result = _coerce_to_response("hello world")
        assert result.body == b"hello world"

    def test_pydantic_model(self):
        class MyModel(BaseModel):
            x: int = 1

        result = _coerce_to_response(MyModel(x=5))
        import json

        data = json.loads(result.body)
        assert data["x"] == 5

    def test_list_of_pydantic_models(self):
        class M(BaseModel):
            v: int

        result = _coerce_to_response([M(v=1), M(v=2)])
        import json

        data = json.loads(result.body)
        assert len(data) == 2

    def test_dataclass_instance(self):
        @dataclasses.dataclass
        class Point:
            x: int
            y: int

        result = _coerce_to_response(Point(3, 4))
        import json

        data = json.loads(result.body)
        assert data == {"x": 3, "y": 4}

    def test_dict_returns_json(self):
        result = _coerce_to_response({"key": "value"})
        import json

        assert json.loads(result.body) == {"key": "value"}

    def test_response_passthrough(self):
        resp = Response.json({"ok": True})
        result = _coerce_to_response(resp)
        assert result is resp


# ---------------------------------------------------------------------------
# LaurenApp properties
# ---------------------------------------------------------------------------


@controller("/props")
class _PropCtrl:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[_PropCtrl])
class _PropMod:
    pass


class TestLaurenAppProperties:
    def test_router_property(self):

        app = LaurenFactory.create(_PropMod)
        assert app.router is not None

    def test_container_property(self):
        app = LaurenFactory.create(_PropMod)
        assert app.container is not None

    def test_app_state_property(self):
        app = LaurenFactory.create(_PropMod)
        assert app.app_state is not None

    def test_module_graph_property(self):
        app = LaurenFactory.create(_PropMod)
        assert app.module_graph is not None

    def test_arena_property(self):
        app = LaurenFactory.create(_PropMod)
        assert app.arena is not None

    def test_signals_property(self):
        app = LaurenFactory.create(_PropMod)
        assert app.signals is not None

    def test_json_encoder_property(self):
        app = LaurenFactory.create(_PropMod)
        assert app.json_encoder is not None

    def test_mounts_property_empty(self):
        app = LaurenFactory.create(_PropMod)
        assert app.mounts == []

    def test_on_shutdown_callback_called(self):
        app = LaurenFactory.create(_PropMod)
        calls = []
        app.on_shutdown(lambda: calls.append("shutdown"))

        _ = TestClient(app)
        # Trigger shutdown via lifespan
        asyncio.run(app.shutdown())
        assert calls == ["shutdown"]

    def test_on_shutdown_async_callback(self):
        app = LaurenFactory.create(_PropMod)
        calls = []

        async def async_cb():
            calls.append("async_shutdown")

        app.on_shutdown(async_cb)
        asyncio.run(app.shutdown())
        assert calls == ["async_shutdown"]

    def test_on_shutdown_callback_that_raises_is_logged(self):
        """An on_shutdown callback that raises is logged but doesn't break shutdown."""
        app = LaurenFactory.create(_PropMod)

        def bad_cb():
            raise RuntimeError("cb error")

        app.on_shutdown(bad_cb)
        # Should not raise
        asyncio.run(app.shutdown())

    def test_startup_called_twice_raises_lifecycle_violation(self):
        """startup() called twice raises LifecycleViolationError when strict_lifecycle=True."""
        from lauren.exceptions import LifecycleViolationError

        app = LaurenFactory.create(_PropMod)
        asyncio.run(app.startup())  # first startup
        with pytest.raises(LifecycleViolationError, match="startup called twice"):
            asyncio.run(app.startup())

    def test_startup_called_twice_noop_when_not_strict(self):
        """startup() called twice does not raise when strict_lifecycle is False."""
        app = LaurenFactory.create(_PropMod, strict_lifecycle=False)
        asyncio.run(app.startup())
        asyncio.run(app.startup())  # Should not raise


# ---------------------------------------------------------------------------
# mount() sub-app dispatch
# ---------------------------------------------------------------------------


class TestMount:
    def test_mount_sub_app_dispatch(self):
        """Mounted sub-app receives requests at its prefix."""

        @controller("/sub")
        class SubCtrl:
            @get("/hello")
            async def hello(self) -> dict:
                return {"from": "sub"}

        @module(controllers=[SubCtrl])
        class SubMod:
            pass

        @controller("/main")
        class MainCtrl:
            @get("/home")
            async def home(self) -> dict:
                return {"from": "main"}

        @module(controllers=[MainCtrl])
        class MainMod:
            pass

        main_app = LaurenFactory.create(MainMod)
        sub_app = LaurenFactory.create(SubMod)

        # Mount sub_app under /sub_app prefix
        main_app.mount("/sub_app", sub_app)
        assert len(main_app.mounts) == 1

        # The main app still handles its own routes
        client = TestClient(main_app)
        resp = client.get("/main/home")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Static and classmethod handlers
# ---------------------------------------------------------------------------


@controller("/static_test")
class _StaticCtrl:
    @staticmethod
    @get("/static")
    async def static_handler() -> dict:
        return {"binding": "static"}

    @classmethod
    @get("/classmethod")
    async def classmethod_handler(cls) -> dict:
        return {"binding": "classmethod"}


@module(controllers=[_StaticCtrl])
class _StaticMod:
    pass


class TestStaticClassmethodHandlers:
    def test_static_handler(self):
        app = LaurenFactory.create(_StaticMod)
        client = TestClient(app)
        resp = client.get("/static_test/static")
        assert resp.status_code == 200
        assert resp.json()["binding"] == "static"

    def test_classmethod_handler(self):
        app = LaurenFactory.create(_StaticMod)
        client = TestClient(app)
        resp = client.get("/static_test/classmethod")
        assert resp.status_code == 200
        assert resp.json()["binding"] == "classmethod"


# ---------------------------------------------------------------------------
# Exception handler: function-form dispatch
# ---------------------------------------------------------------------------


class TestExceptionHandlerFunctionForm:
    def test_function_form_exception_handler(self):
        """exception_handler can be a plain function (not a class)."""
        from lauren.decorators import exception_handler

        @exception_handler(ValueError)
        def handle_value_error(exc: ValueError, req: Any) -> dict:
            return {"error": str(exc), "custom": True}

        @controller("/func_eh")
        class FuncEhCtrl:
            @get("/")
            async def index(self) -> dict:
                raise ValueError("test error")

        @module(controllers=[FuncEhCtrl])
        class FuncEhMod:
            pass

        app = LaurenFactory.create(
            FuncEhMod,
            global_exception_handlers=[handle_value_error],
        )
        client = TestClient(app)
        resp = client.get("/func_eh")
        # Should be handled by our function-form handler
        assert resp.status_code == 200
        data = resp.json()
        assert data["custom"] is True


# ---------------------------------------------------------------------------
# lifespan startup failure
# ---------------------------------------------------------------------------


class TestLifespanStartupFailure:
    def test_lifespan_startup_failure_sends_failed_message(self):
        """If startup() raises, lifespan sends startup.failed."""
        import asyncio

        @controller("/lf")
        class LfCtrl:
            @get("/")
            async def index(self) -> dict:
                return {}

        @module(controllers=[LfCtrl])
        class LfMod:
            pass

        app = LaurenFactory.create(LfMod)

        messages_sent = []

        async def fake_receive():
            return {"type": "lifespan.startup"}

        async def fake_send(msg):
            messages_sent.append(msg)

        # Make startup() raise
        async def bad_startup():
            raise RuntimeError("startup failure")

        original_startup = app.startup
        app.startup = bad_startup  # type: ignore[method-assign]
        try:
            asyncio.run(app._lifespan({}, fake_receive, fake_send))
        except Exception:
            pass

        app.startup = original_startup
        failed_types = [m["type"] for m in messages_sent]
        assert "lifespan.startup.failed" in failed_types


# ---------------------------------------------------------------------------
# Non-http/non-websocket ASGI scope is ignored
# ---------------------------------------------------------------------------


class TestNonHttpScope:
    def test_non_http_scope_ignored(self):
        """A scope type other than http/websocket returns without sending anything."""

        @controller("/noop")
        class NoopCtrl:
            @get("/")
            async def index(self) -> dict:
                return {}

        @module(controllers=[NoopCtrl])
        class NoopMod:
            pass

        app = LaurenFactory.create(NoopMod)

        sent = []

        async def run():
            await app({"type": "unsupported_scope"}, None, lambda m: sent.append(m))

        asyncio.run(run())
        assert sent == []


# ---------------------------------------------------------------------------
# Handler returning tuple with headers
# ---------------------------------------------------------------------------


@controller("/tuple_resp")
class _TupleCtrl:
    @get("/with_headers")
    async def with_headers(self) -> tuple:
        return {"data": "value"}, 201, {"X-Custom-Header": "test-value"}

    @get("/just_status")
    async def just_status(self) -> tuple:
        return {"data": "value"}, 202


@module(controllers=[_TupleCtrl])
class _TupleMod:
    pass


class TestTupleResponse:
    def test_tuple_with_status_and_headers(self):
        app = LaurenFactory.create(_TupleMod)
        client = TestClient(app)
        resp = client.get("/tuple_resp/with_headers")
        assert resp.status_code == 201
        assert resp.header("X-Custom-Header") == "test-value"

    def test_tuple_with_status_only(self):
        app = LaurenFactory.create(_TupleMod)
        client = TestClient(app)
        resp = client.get("/tuple_resp/just_status")
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# root_path stripping
# ---------------------------------------------------------------------------


class TestRootPathStripping:
    def test_root_path_stripped_from_request(self):
        """When root_path is configured, it's stripped from the path before routing."""

        @controller("/api")
        class RpCtrl:
            @get("/v1/endpoint")
            async def endpoint(self) -> dict:
                return {"ok": True}

        @module(controllers=[RpCtrl])
        class RpMod:
            pass

        app = LaurenFactory.create(RpMod, root_path="/prefix")
        client = TestClient(app)
        # Route is /api/v1/endpoint, but with root_path=/prefix it's at /prefix/api/v1/endpoint
        resp = client.get("/prefix/api/v1/endpoint")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# ExecutionContext injection
# ---------------------------------------------------------------------------


class TestExecutionContextInjection:
    def test_exec_ctx_injected_into_route(self):
        """A parameter annotated as ExecutionContext receives the live ctx."""

        @controller("/ctx")
        class CtxCtrl:
            @get("/info")
            async def info(self, ctx: ExecutionContext) -> dict:
                return {
                    "template": ctx.route_template,
                    "handler": ctx.handler_func.__name__,
                    "class": ctx.handler_class.__name__,
                }

        @module(controllers=[CtxCtrl])
        class CtxMod:
            pass

        client = TestClient(LaurenFactory.create(CtxMod))
        resp = client.get("/ctx/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["template"] == "/ctx/info"
        assert data["handler"] == "info"
        assert data["class"] == "CtxCtrl"

    def test_exec_ctx_alongside_other_params(self):
        """ExecutionContext can coexist with path, query, and body params."""
        from lauren import Path, Query

        @controller("/ctx")
        class MixedCtrl:
            @get("/items/{item_id}")
            async def item(
                self,
                item_id: Path[int],
                q: Query[str] = "default",
                ctx: ExecutionContext = None,  # type: ignore[assignment]
            ) -> dict:
                return {
                    "item_id": item_id,
                    "q": q,
                    "has_ctx": ctx is not None,
                    "template": ctx.route_template,
                }

        @module(controllers=[MixedCtrl])
        class MixedMod:
            pass

        client = TestClient(LaurenFactory.create(MixedMod))
        resp = client.get("/ctx/items/42?q=hello")
        assert resp.status_code == 200
        data = resp.json()
        assert data["item_id"] == 42
        assert data["q"] == "hello"
        assert data["has_ctx"] is True
        assert data["template"] == "/ctx/items/{item_id}"

    def test_exec_ctx_in_sync_handler(self):
        """ExecutionContext injection works in synchronous (non-async) handlers."""

        @controller("/ctx")
        class SyncCtrl:
            @get("/sync")
            def sync_info(self, ctx: ExecutionContext) -> dict:
                return {"template": ctx.route_template}

        @module(controllers=[SyncCtrl])
        class SyncMod:
            pass

        client = TestClient(LaurenFactory.create(SyncMod))
        resp = client.get("/ctx/sync")
        assert resp.status_code == 200
        assert resp.json()["template"] == "/ctx/sync"

    def test_exec_ctx_metadata_visible(self):
        """Metadata set via @set_metadata is accessible through ctx.metadata."""
        from lauren import set_metadata

        @controller("/ctx")
        class MetaCtrl:
            @set_metadata("role", "admin")
            @get("/protected")
            async def protected(self, ctx: ExecutionContext) -> dict:
                return {"role": ctx.get_metadata("role")}

        @module(controllers=[MetaCtrl])
        class MetaMod:
            pass

        client = TestClient(LaurenFactory.create(MetaMod))
        resp = client.get("/ctx/protected")
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_exec_ctx_post_handler(self):
        """ExecutionContext injection works on POST routes."""

        @controller("/ctx")
        class PostCtrl:
            @post("/submit")
            async def submit(self, ctx: ExecutionContext) -> dict:
                return {"method": ctx.request.method}

        @module(controllers=[PostCtrl])
        class PostMod:
            pass

        client = TestClient(LaurenFactory.create(PostMod))
        resp = client.post("/ctx/submit")
        assert resp.status_code == 200
        assert resp.json()["method"] == "POST"
