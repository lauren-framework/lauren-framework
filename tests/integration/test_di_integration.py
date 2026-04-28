"""Integration tests for DI across real handlers."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


from lauren import (
    Depends,
    LaurenFactory,
    Response,
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
# Simple service injection
# ---------------------------------------------------------------------------


@injectable()
class Counter:
    def __init__(self):
        self.value = 0

    def inc(self) -> int:
        self.value += 1
        return self.value


@controller("/count")
class CountController:
    def __init__(self, counter: Counter):
        self.counter = counter

    @get("/")
    async def get_count(self) -> Response:
        return Response.json({"count": self.counter.inc()})


@module(controllers=[CountController], providers=[Counter])
class CountModule:
    pass


class TestSingletonInjection:
    def test_singleton_shared_across_requests(self):
        app = LaurenFactory.create(CountModule)
        client = TestClient(app)
        assert client.get("/count/").json()["count"] == 1
        assert client.get("/count/").json()["count"] == 2
        assert client.get("/count/").json()["count"] == 3


# ---------------------------------------------------------------------------
# Multi-level dependency injection
# ---------------------------------------------------------------------------


@injectable()
class Config:
    def __init__(self):
        self.name = "prod"


@injectable()
class Db:
    def __init__(self, config: Config):
        self.config = config


@injectable()
class UserRepo:
    def __init__(self, db: Db):
        self.db = db


@controller("/info")
class InfoController:
    def __init__(self, repo: UserRepo):
        self.repo = repo

    @get("/")
    async def info(self) -> dict:
        return {"config": self.repo.db.config.name}


@module(controllers=[InfoController], providers=[Config, Db, UserRepo])
class InfoModule:
    pass


class TestDeepDI:
    def test_multi_level(self):
        app = LaurenFactory.create(InfoModule)
        client = TestClient(app)
        r = client.get("/info/")
        assert r.json() == {"config": "prod"}


# ---------------------------------------------------------------------------
# Protocol binding
# ---------------------------------------------------------------------------


@runtime_checkable
class Greeter(Protocol):
    def greet(self) -> str: ...


@injectable(provides=[Greeter])
class FormalGreeter:
    def greet(self) -> str:
        return "Good day."


@controller("/greet")
class GreetController:
    def __init__(self, g: Greeter):  # type: ignore[valid-type]
        self.g = g

    @get("/")
    async def greet(self) -> dict:
        return {"msg": self.g.greet()}


@module(controllers=[GreetController], providers=[FormalGreeter])
class GreetModule:
    pass


class TestProtocol:
    def test_protocol_injection(self):
        app = LaurenFactory.create(GreetModule)
        client = TestClient(app)
        assert client.get("/greet/").json() == {"msg": "Good day."}


# ---------------------------------------------------------------------------
# Request-scoped services
# ---------------------------------------------------------------------------


@injectable(scope=Scope.REQUEST)
class ReqScoped:
    _counter = 0

    def __init__(self):
        ReqScoped._counter += 1
        self.id = ReqScoped._counter


@controller("/req")
class ReqController:
    @get("/")
    async def index(
        self,
        a: Depends[ReqScoped],
        b: Depends[ReqScoped],
    ) -> dict:
        # Same instance within one request
        return {"same": a is b, "aid": a.id, "bid": b.id}


@module(controllers=[ReqController], providers=[ReqScoped])
class ReqModule:
    pass


class TestRequestScope:
    def test_request_scope_single_instance_per_request(self):
        ReqScoped._counter = 0  # reset state
        app = LaurenFactory.create(ReqModule)
        client = TestClient(app)
        r = client.get("/req/")
        assert r.json()["same"] is True
        r2 = client.get("/req/")
        # Different request => different id
        assert r2.json()["aid"] != r.json()["aid"]


# ---------------------------------------------------------------------------
# Lifecycle hooks invoked via real app startup
# ---------------------------------------------------------------------------


class LifecycleProbe:
    calls: list[str] = []


@injectable()
class StartupService:
    @post_construct
    async def init(self) -> None:
        LifecycleProbe.calls.append("post")

    @pre_destruct
    async def cleanup(self) -> None:
        LifecycleProbe.calls.append("pre")


@controller("/ok")
class OkController:
    def __init__(self, s: StartupService):
        self.s = s

    @get("/")
    async def ok(self) -> dict:
        return {"ok": True}


@module(controllers=[OkController], providers=[StartupService])
class LifecycleModule:
    pass


class TestLifecycleInApp:
    def test_post_and_pre_construct(self):
        LifecycleProbe.calls = []

        async def run():
            app = LaurenFactory.create(LifecycleModule)
            client = TestClient(app)
            r = client.get("/ok/")
            assert r.json() == {"ok": True}
            await app.shutdown()

        asyncio.run(run())
        assert LifecycleProbe.calls == ["post", "pre"]
