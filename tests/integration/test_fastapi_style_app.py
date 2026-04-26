"""FastAPI-style :class:`Lauren` application tests.

``Lauren`` is the imperative counterpart to the NestJS-style
``LaurenFactory.create`` entry point. It exposes ``@app.get``/``@app.post``
etc. and ``app.include_module`` for mixing declarative modules into the
same application.

These tests cover the FastAPI parity claim end-to-end: zero-config
construction, verb decorators, module inclusion, middleware, lifecycle
hooks, state, OpenAPI, and built-in docs endpoints.
"""

# Intentional: no ``from __future__ import annotations``. Several tests
# declare classes inside test methods and rely on live annotations.

from typing import Annotated

import pytest
from pydantic import BaseModel

from lauren import (
    CallNext,
    Depends,
    Json,
    Lauren,
    Path,
    PathField,
    Query,
    QueryField,
    Request,
    Response,
    controller,
    get,
    injectable,
    middleware,
    module,
    pipe,
)
from lauren.exceptions import DecoratorUsageError, LifecycleViolationError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Construction & defaults
# ---------------------------------------------------------------------------


class TestZeroConfigConstruction:
    @pytest.mark.asyncio
    async def test_default_construction_has_no_required_args(self):
        app = Lauren()  # no kwargs at all
        # Sensible defaults match the FastAPI convention.
        assert app.title == "lauren application"
        assert app.version == "1.0.0"
        assert app.debug is False

    @pytest.mark.asyncio
    async def test_default_enables_docs(self):
        app = Lauren()

        @app.get("/")
        async def root() -> dict:
            return {"ok": True}

        client = TestClient(app)
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200

    @pytest.mark.asyncio
    async def test_can_disable_docs(self):
        app = Lauren(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/")
        async def root() -> dict:
            return {"ok": True}

        client = TestClient(app)
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# @app.get / @app.post etc. cover every HTTP verb
# ---------------------------------------------------------------------------


class TestMethodDecorators:
    @pytest.mark.asyncio
    async def test_every_verb_registers_a_route(self):
        app = Lauren()

        @app.get("/r")
        async def h_get() -> dict:
            return {"v": "GET"}

        @app.post("/r")
        async def h_post() -> dict:
            return {"v": "POST"}

        @app.put("/r")
        async def h_put() -> dict:
            return {"v": "PUT"}

        @app.patch("/r")
        async def h_patch() -> dict:
            return {"v": "PATCH"}

        @app.delete("/r")
        async def h_delete() -> dict:
            return {"v": "DELETE"}

        client = TestClient(app)
        for verb in ("get", "post", "put", "patch", "delete"):
            resp = getattr(client, verb)("/r")
            assert resp.status_code == 200
            assert resp.json()["v"] == verb.upper()

    @pytest.mark.asyncio
    async def test_extractors_and_pipes_work(self):
        app = Lauren()

        def to_upper(v: str) -> str:
            return v.upper()

        @app.get("/users/{uid}")
        async def show(
            uid: Annotated[Path[int], PathField(ge=1)],
            nickname: Query[str] = QueryField(default="anon") | pipe(to_upper),
        ) -> dict:
            return {"uid": uid, "nickname": nickname}

        client = TestClient(app)
        r = client.get("/users/7?nickname=alice")
        assert r.status_code == 200
        assert r.json() == {"uid": 7, "nickname": "ALICE"}
        # PathField(ge=1) still validates.
        assert client.get("/users/0").status_code >= 400

    @pytest.mark.asyncio
    async def test_json_body_with_pydantic(self):
        class Create(BaseModel):
            name: str
            tag: str | None = None

        app = Lauren()

        @app.post("/items", response_model=Create)
        async def create(body: Json[Create]) -> Create:
            return body

        client = TestClient(app)
        r = client.post("/items", json={"name": "widget", "tag": "new"})
        assert r.status_code == 200
        assert r.json() == {"name": "widget", "tag": "new"}

    def test_method_decorator_without_parens_rejected(self):
        app = Lauren()
        with pytest.raises(DecoratorUsageError):

            @app.get
            async def handler() -> dict:
                return {}


# ---------------------------------------------------------------------------
# include_module \u2014 mix declarative modules with the imperative API
# ---------------------------------------------------------------------------


@injectable()
class Counter:
    def __init__(self):
        self.n = 0

    def inc(self) -> int:
        self.n += 1
        return self.n


@controller("/counter")
class CounterController:
    def __init__(self, counter: Counter):
        self.counter = counter

    @get("/bump")
    async def bump(self) -> dict:
        return {"n": self.counter.inc()}


@module(
    controllers=[CounterController],
    providers=[Counter],
    exports=[Counter],
)
class CounterModule:
    pass


class TestIncludeModule:
    @pytest.mark.asyncio
    async def test_include_module_registers_controller(self):
        app = Lauren()
        app.include_module(CounterModule)

        @app.get("/")
        async def root() -> dict:
            return {"hello": "world"}

        client = TestClient(app)
        assert client.get("/").json() == {"hello": "world"}
        assert client.get("/counter/bump").json() == {"n": 1}
        assert client.get("/counter/bump").json() == {"n": 2}

    @pytest.mark.asyncio
    async def test_app_level_route_can_depend_on_module_provider(self):
        """An ``@app.get`` handler that pulls a provider exported by an
        included module via ``Depends[...]``."""
        app = Lauren()
        app.include_module(CounterModule)

        @app.get("/total")
        async def total(counter: Depends[Counter]) -> dict:
            return {"total": counter.n}

        client = TestClient(app)
        client.get("/counter/bump")  # n -> 1
        client.get("/counter/bump")  # n -> 2
        assert client.get("/total").json() == {"total": 2}

    def test_include_module_deduplicates(self):
        app = Lauren()
        app.include_module(CounterModule)
        app.include_module(CounterModule)
        app.include_module(CounterModule)
        # Internal list keeps single entry \u2014 compilation succeeds.
        assert app._modules.count(CounterModule) == 1


# ---------------------------------------------------------------------------
# Middleware registration
# ---------------------------------------------------------------------------


class TestAddMiddleware:
    @pytest.mark.asyncio
    async def test_middleware_runs_around_every_request(self):
        @middleware
        class Stamp:
            async def dispatch(self, request: Request, call_next: CallNext) -> Response:
                resp = await call_next(request)
                return resp.with_header("x-stamp", "hello")

        app = Lauren()
        app.add_middleware(Stamp)

        @app.get("/ping")
        async def ping() -> dict:
            return {"pong": True}

        r = TestClient(app).get("/ping")
        assert r.header("x-stamp") == "hello"


# ---------------------------------------------------------------------------
# Lifecycle hooks: on_startup / on_shutdown
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    @pytest.mark.asyncio
    async def test_on_startup_runs_before_first_request(self):
        events: list[str] = []

        app = Lauren()

        @app.on_startup
        def boot() -> None:
            events.append("startup")

        @app.get("/")
        async def root() -> dict:
            events.append("request")
            return {"ok": True}

        TestClient(app).get("/")
        assert events == ["startup", "request"]

    @pytest.mark.asyncio
    async def test_async_startup_awaited(self):
        events: list[str] = []

        app = Lauren()

        @app.on_startup
        async def boot() -> None:
            events.append("started")

        @app.get("/")
        async def root() -> dict:
            return {}

        TestClient(app).get("/")
        assert events == ["started"]

    @pytest.mark.asyncio
    async def test_on_shutdown_runs(self):
        events: list[str] = []
        app = Lauren()

        @app.on_shutdown
        def die() -> None:
            events.append("shutdown")

        @app.get("/")
        async def root() -> dict:
            return {}

        await app.startup()
        await app.shutdown()
        assert events == ["shutdown"]


# ---------------------------------------------------------------------------
# Post-compilation immutability
# ---------------------------------------------------------------------------


class TestPostCompileImmutability:
    @pytest.mark.asyncio
    async def test_cannot_add_route_after_startup(self):
        app = Lauren()

        @app.get("/")
        async def root() -> dict:
            return {}

        await app.startup()
        with pytest.raises(LifecycleViolationError):

            @app.get("/too-late")
            async def late() -> dict:
                return {}

    @pytest.mark.asyncio
    async def test_cannot_include_module_after_startup(self):
        app = Lauren()
        await app.startup()
        with pytest.raises(LifecycleViolationError):
            app.include_module(CounterModule)

    @pytest.mark.asyncio
    async def test_openapi_before_startup_fails(self):
        app = Lauren()
        with pytest.raises(LifecycleViolationError):
            app.openapi()


# ---------------------------------------------------------------------------
# Router inclusion (Lauren -> Lauren)
# ---------------------------------------------------------------------------


class TestIncludeRouter:
    @pytest.mark.asyncio
    async def test_router_merge_with_prefix(self):
        users = Lauren()

        @users.get("/{uid}")
        async def show(uid: Path[int]) -> dict:
            return {"uid": uid}

        app = Lauren()
        app.include_router(users, prefix="/api/users")

        client = TestClient(app)
        assert client.get("/api/users/42").json() == {"uid": 42}

    @pytest.mark.asyncio
    async def test_router_module_and_middleware_merged(self):
        @middleware
        class Tag:
            async def dispatch(self, request: Request, call_next: CallNext) -> Response:
                r = await call_next(request)
                return r.with_header("x-sub", "1")

        sub = Lauren()
        sub.include_module(CounterModule)
        sub.add_middleware(Tag)

        app = Lauren()
        app.include_router(sub)

        r = TestClient(app).get("/counter/bump")
        assert r.status_code == 200
        assert r.header("x-sub") == "1"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TestAppState:
    @pytest.mark.asyncio
    async def test_state_is_shared_across_requests(self):
        app = Lauren()
        app.state.db = {"users": {}}

        @app.get("/add/{name}")
        async def add(name: Path[str], request: Request) -> dict:
            request.app_state.get("db")["users"][name] = True
            return {"stored": True}

        @app.get("/count")
        async def count(request: Request) -> dict:
            return {"n": len(request.app_state.get("db")["users"])}

        c = TestClient(app)
        c.get("/add/alice")
        c.get("/add/bob")
        assert c.get("/count").json() == {"n": 2}


# ---------------------------------------------------------------------------
# OpenAPI integration
# ---------------------------------------------------------------------------


class TestFastApiOpenAPI:
    @pytest.mark.asyncio
    async def test_openapi_title_version_from_constructor(self):
        app = Lauren(title="My API", version="3.2.1", description="Demo")

        @app.get("/")
        async def root() -> dict:
            return {}

        await app.startup()
        schema = app.openapi()
        assert schema["info"]["title"] == "My API"
        assert schema["info"]["version"] == "3.2.1"
        assert schema["info"]["description"] == "Demo"

    @pytest.mark.asyncio
    async def test_openapi_combines_app_and_module_routes(self):
        app = Lauren()
        app.include_module(CounterModule)

        @app.get("/hello")
        async def hello() -> dict:
            return {}

        await app.startup()
        schema = app.openapi()
        assert "/hello" in schema["paths"]
        assert "/counter/bump" in schema["paths"]
