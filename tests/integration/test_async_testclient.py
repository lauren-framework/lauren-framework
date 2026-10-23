"""``AsyncTestClient`` and ``lauren.testing.lifespan`` on the test's own event loop."""

from __future__ import annotations

import asyncio

import pytest

from lauren import (
    Json,
    Lauren,
    LaurenFactory,
    controller,
    get,
    injectable,
    module,
    post,
    post_construct,
    pre_destruct,
)
from lauren.testing import AsyncTestClient, TestClient, lifespan


def _build(events: list[str]):
    @injectable()
    class Resource:
        @post_construct
        async def open(self) -> None:
            self.loop = asyncio.get_running_loop()
            events.append("open")

        @pre_destruct
        async def close(self) -> None:
            events.append("close")

    @controller("/")
    class Api:
        def __init__(self, resource: Resource) -> None:
            self.resource = resource

        @get("/loop")
        async def loop(self) -> dict:
            return {"same_loop": self.resource.loop is asyncio.get_running_loop()}

        @post("/echo")
        async def echo(self, body: Json[dict]) -> dict:
            return body

    @module(controllers=[Api], providers=[Resource])
    class ApiModule:
        pass

    return LaurenFactory.create(ApiModule)


async def test_async_with_runs_startup_and_shutdown_on_this_loop() -> None:
    events: list[str] = []
    async with AsyncTestClient(_build(events)) as client:
        response = await client.get("/loop")
        assert response.json() == {"same_loop": True}
        assert client.app.container.singletons()  # started
    assert events == ["open", "close"]


async def test_every_verb_is_awaitable() -> None:
    async with AsyncTestClient(_build([])) as client:
        assert (await client.post("/echo", json={"a": 1})).json() == {"a": 1}
        for verb in ("put", "patch", "delete", "head", "options"):
            response = await getattr(client, verb)("/loop")
            assert response.status_code in (200, 404, 405)


async def test_bare_async_client_starts_on_first_request_and_aclose_shuts_down() -> None:
    events: list[str] = []
    client = AsyncTestClient(_build(events))
    assert events == []
    await client.get("/loop")
    assert events == ["open"]
    await client.aclose()
    await client.aclose()
    assert events == ["open", "close"]


async def test_closed_async_client_refuses_requests() -> None:
    client = AsyncTestClient(_build([]))
    await client.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await client.get("/loop")


async def test_app_driven_by_a_sync_client_cannot_join_another_loop() -> None:
    app = _build([])
    sync_client = TestClient(app)
    with pytest.raises(RuntimeError, match="already driven by TestClient"):
        await AsyncTestClient(app).get("/loop")
    sync_client.close()


def test_sync_client_cannot_join_an_app_driven_by_an_async_client() -> None:
    app = _build([])

    async def main() -> None:
        client = AsyncTestClient(app)
        await client.get("/loop")
        with pytest.raises(RuntimeError, match="already driven by AsyncTestClient"):
            TestClient(app)
        await client.aclose()

    asyncio.run(main())


def test_async_client_used_from_another_loop_raises() -> None:
    client = AsyncTestClient(_build([]))
    asyncio.run(client.get("/loop"))
    with pytest.raises(RuntimeError, match="another event loop"):
        asyncio.run(client.get("/loop"))


async def test_second_async_client_on_the_same_loop_shares_the_driver() -> None:
    app = _build([])
    async with AsyncTestClient(app) as first:
        second = AsyncTestClient(app)
        assert (await second.get("/loop")).status_code == 200
        with pytest.raises(RuntimeError, match="new_session"):
            async with AsyncTestClient(app):
                pass  # pragma: no cover
        assert (await first.get("/loop")).status_code == 200


async def test_async_new_session() -> None:
    events: list[str] = []
    async with AsyncTestClient(_build(events)) as client:
        other = client.new_session(headers={"x": "1"})
        async with other:
            assert (await other.get("/loop")).status_code == 200
        assert events == ["open"]
    assert events == ["open", "close"]


@injectable()
class Greeter:
    def hello(self) -> str:
        return "hi"


async def test_async_resolve() -> None:
    @module(providers=[Greeter])
    class GreeterModule:
        pass

    async with AsyncTestClient(LaurenFactory.create(GreeterModule)) as client:
        assert (await client.resolve(Greeter)).hello() == "hi"


async def test_async_capture_before_start_needs_a_lauren_app() -> None:
    async def raw(scope, receive, send):  # pragma: no cover - never called
        pass

    with pytest.raises(TypeError, match="started client"):
        with AsyncTestClient(raw).capture():
            pass  # pragma: no cover


async def test_lifespan_helper_for_third_party_clients() -> None:
    httpx = pytest.importorskip("httpx")
    events: list[str] = []
    app = _build(events)
    async with lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/loop")).json() == {"same_loop": True}
    assert events == ["open", "close"]


async def test_lifespan_helper_with_a_lauren_app() -> None:
    events: list[str] = []
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)
    app.on_shutdown(lambda: events.append("shutdown"))

    @app.get("/x")
    async def handler() -> dict:
        return {}

    async with lifespan(app):
        assert app._compiled._started
    assert events == ["shutdown"]


async def test_lifespan_helper_with_a_foreign_app() -> None:
    events: list[str] = []

    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        while True:
            message = await receive()
            events.append(message["type"])
            await send({"type": message["type"] + ".complete"})
            if message["type"] == "lifespan.shutdown":
                return

    async with lifespan(app):
        assert events == ["lifespan.startup"]
    assert events == ["lifespan.startup", "lifespan.shutdown"]
