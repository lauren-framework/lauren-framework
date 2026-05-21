"""Integration tests for generator-based injectable lifecycle.

Exercises the full HTTP stack:

* REQUEST-scoped generator opens/closes a resource per request.
* SINGLETON-scoped generator runs setup at startup and teardown at shutdown.
* Generator with its own DI dependencies resolves them correctly.
* ``finally`` block in the generator runs even when the handler raises.
* Multiple requests each get their own REQUEST-scoped generator instance.
* Callers never see the internal ``_GeneratorContextWrapper``.
"""

from __future__ import annotations

import asyncio


from lauren import (
    Depends,
    LaurenFactory,
    Response,
    Scope,
    controller,
    get,
    injectable,
    module,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# REQUEST-scoped generator — one instance per request
# ---------------------------------------------------------------------------

_req_events: list[str] = []


@injectable(scope=Scope.REQUEST)
def request_resource():
    _req_events.append("open")
    yield {"id": len(_req_events)}
    _req_events.append("close")


@controller("/req-gen")
class ReqGenController:
    @get("/")
    async def read(self, res: Depends[request_resource]) -> Response:
        return Response.json(res)


@module(controllers=[ReqGenController], providers=[request_resource])
class ReqGenModule:
    pass


class TestRequestScopedGenerator:
    def setup_method(self):
        _req_events.clear()

    def test_resource_opened_and_closed_per_request(self):
        app = LaurenFactory.create(ReqGenModule)
        client = TestClient(app)
        client.get("/req-gen/")
        assert _req_events.count("open") == 1
        assert _req_events.count("close") == 1

        client.get("/req-gen/")
        assert _req_events.count("open") == 2
        assert _req_events.count("close") == 2

    def test_caller_receives_yielded_value_not_wrapper(self):
        app = LaurenFactory.create(ReqGenModule)
        client = TestClient(app)
        r = client.get("/req-gen/")
        data = r.json()
        assert "id" in data


# ---------------------------------------------------------------------------
# SINGLETON-scoped generator — setup at startup, teardown at shutdown
# ---------------------------------------------------------------------------

_singleton_events: list[str] = []


@injectable(scope=Scope.SINGLETON)
def singleton_resource():
    _singleton_events.append("singleton-open")
    yield "singleton-conn"
    _singleton_events.append("singleton-close")


@controller("/sing-gen")
class SingletonGenController:
    def __init__(self, res: Depends[singleton_resource]) -> None:
        self._res = res

    @get("/")
    async def read(self) -> Response:
        return Response.json({"conn": self._res})


@module(controllers=[SingletonGenController], providers=[singleton_resource])
class SingletonGenModule:
    pass


class TestSingletonScopedGenerator:
    def setup_method(self):
        _singleton_events.clear()

    def test_setup_at_startup_teardown_at_shutdown(self):
        async def run():
            app = LaurenFactory.create(SingletonGenModule)
            client = TestClient(app)
            r = client.get("/sing-gen/")
            assert r.status_code == 200
            assert r.json() == {"conn": "singleton-conn"}
            assert _singleton_events == ["singleton-open"]
            await app.shutdown()

        asyncio.run(run())
        assert _singleton_events == ["singleton-open", "singleton-close"]

    def test_singleton_instantiated_once_across_requests(self):
        app = LaurenFactory.create(SingletonGenModule)
        client = TestClient(app)
        client.get("/sing-gen/")
        client.get("/sing-gen/")
        client.get("/sing-gen/")
        assert _singleton_events.count("singleton-open") == 1


# ---------------------------------------------------------------------------
# Generator with DI dependencies
# ---------------------------------------------------------------------------

_dep_events: list[str] = []


@injectable()
class DbConfig:
    def __init__(self) -> None:
        self.dsn = "sqlite://test"


@injectable(scope=Scope.REQUEST)
def db_session(cfg: DbConfig):
    _dep_events.append(f"connect:{cfg.dsn}")
    yield {"session": cfg.dsn}
    _dep_events.append("disconnect")


@controller("/db-gen")
class DbGenController:
    @get("/")
    async def read(self, session: Depends[db_session]) -> Response:
        return Response.json(session)


@module(controllers=[DbGenController], providers=[DbConfig, db_session])
class DbGenModule:
    pass


class TestGeneratorWithDeps:
    def setup_method(self):
        _dep_events.clear()

    def test_generator_deps_resolved_correctly(self):
        app = LaurenFactory.create(DbGenModule)
        client = TestClient(app)
        r = client.get("/db-gen/")
        assert r.status_code == 200
        assert r.json() == {"session": "sqlite://test"}
        assert _dep_events == ["connect:sqlite://test", "disconnect"]


# ---------------------------------------------------------------------------
# finally block always runs even when handler raises
# ---------------------------------------------------------------------------

_finally_events: list[str] = []


@injectable(scope=Scope.REQUEST)
def critical_resource():
    _finally_events.append("acquire")
    try:
        yield {"handle": "h1"}
    finally:
        _finally_events.append("release")


@controller("/finally-gen")
class FinallyGenController:
    @get("/ok")
    async def ok(self, res: Depends[critical_resource]) -> Response:
        return Response.json(res)

    @get("/fail")
    async def fail(self, res: Depends[critical_resource]) -> Response:
        raise RuntimeError("handler boom")


@module(controllers=[FinallyGenController], providers=[critical_resource])
class FinallyGenModule:
    pass


class TestFinallyBlock:
    def setup_method(self):
        _finally_events.clear()

    def test_finally_runs_on_successful_request(self):
        app = LaurenFactory.create(FinallyGenModule)
        client = TestClient(app)
        r = client.get("/finally-gen/ok")
        assert r.status_code == 200
        assert _finally_events == ["acquire", "release"]

    def test_finally_runs_when_handler_raises(self):
        app = LaurenFactory.create(FinallyGenModule)
        client = TestClient(app)
        r = client.get("/finally-gen/fail")
        assert r.status_code == 500
        assert _finally_events == ["acquire", "release"]


# ---------------------------------------------------------------------------
# Async generator provider
# ---------------------------------------------------------------------------

_async_events: list[str] = []


@injectable(scope=Scope.REQUEST)
async def async_resource():
    _async_events.append("async-open")
    yield "async-value"
    _async_events.append("async-close")


@controller("/async-gen")
class AsyncGenController:
    @get("/")
    async def read(self, res: Depends[async_resource]) -> Response:
        return Response.json({"value": res})


@module(controllers=[AsyncGenController], providers=[async_resource])
class AsyncGenModule:
    pass


class TestAsyncGeneratorProvider:
    def setup_method(self):
        _async_events.clear()

    def test_async_generator_opens_and_closes(self):
        app = LaurenFactory.create(AsyncGenModule)
        client = TestClient(app)
        r = client.get("/async-gen/")
        assert r.status_code == 200
        assert r.json() == {"value": "async-value"}
        assert _async_events == ["async-open", "async-close"]
