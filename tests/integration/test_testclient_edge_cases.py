"""Edge cases of the test clients: raw ASGI misbehaviour, back-pressure, lifespan corner cases."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import Lauren, LaurenFactory, Request, controller, get, module
from lauren.testing import AsyncTestClient, TestClient, WsTestClient, lifespan
from lauren.testing._transport import STREAM_BUFFER_LIMIT


def _http_only(handler):
    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        await handler(scope, receive, send)

    return app


# -- transport --------------------------------------------------------------------


def test_streamed_content_must_yield_bytes_or_str() -> None:
    async def handler(scope, receive, send):
        while (await receive()).get("more_body"):
            pass

    with TestClient(_http_only(handler)) as client:
        with pytest.raises(TypeError, match="must yield bytes or str"):
            client.post("/", content=iter([b"ok", 42]))


def test_duplicate_start_and_unknown_messages_are_ignored() -> None:
    async def handler(scope, receive, send):
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.trailers", "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    with TestClient(_http_only(handler)) as client:
        response = client.get("/")
    assert (response.status_code, response.content) == (201, b"done")


def test_a_fast_stream_waits_for_the_reader() -> None:
    sent: list[int] = []

    async def handler(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for n in range(STREAM_BUFFER_LIMIT * 3):
            await send({"type": "http.response.body", "body": b"x", "more_body": True})
            sent.append(n)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    with TestClient(_http_only(handler)) as client:
        with client.stream("GET", "/") as response:
            chunks = response.iter_bytes()
            next(chunks)
            assert len(sent) <= STREAM_BUFFER_LIMIT + 1  # the producer is held back
            assert len(response.read()) == STREAM_BUFFER_LIMIT * 3


def test_closing_a_stream_releases_a_producer_blocked_on_back_pressure() -> None:
    state = {"finished": False}

    async def handler(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await send({"type": "http.response.body", "body": b"x", "more_body": True})
        finally:
            state["finished"] = True

    with TestClient(_http_only(handler)) as client:
        with client.stream("GET", "/") as response:
            next(response.iter_bytes())
    assert state["finished"] is True


def test_stream_surfaces_an_error_raised_before_the_response() -> None:
    async def handler(scope, receive, send):
        raise KeyError("before start")

    with TestClient(_http_only(handler)) as client:
        with pytest.raises(KeyError, match="before start"):
            with client.stream("GET", "/"):
                pass  # pragma: no cover


def test_exception_inside_a_stream_block_still_disconnects() -> None:
    state = {"finished": False}

    async def handler(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await send({"type": "http.response.body", "body": b"x", "more_body": True})
                await asyncio.sleep(0)
        finally:
            state["finished"] = True

    with TestClient(_http_only(handler)) as client:
        with pytest.raises(ZeroDivisionError):
            with client.stream("GET", "/") as response:
                next(response.iter_bytes())
                raise ZeroDivisionError
    assert state["finished"] is True


def test_a_stream_app_that_stops_on_disconnect_is_not_cancelled() -> None:
    state: dict[str, bool] = {}

    async def handler(scope, receive, send):
        await receive()  # the (empty) request body
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        try:
            message = await receive()  # blocks until the client goes away
            state["saw_disconnect"] = message["type"] == "http.disconnect"
        except asyncio.CancelledError:  # pragma: no cover - would mean a premature cancel
            state["cancelled"] = True
            raise

    with TestClient(_http_only(handler)) as client:
        with client.stream("GET", "/") as response:
            next(response.iter_bytes())
    assert state == {"saw_disconnect": True}


def test_an_app_ignoring_cancellation_after_abort_hits_the_timeout() -> None:
    async def handler(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)  # stubbornly keeps running after being cancelled

    # The stubborn task is still running when the client closes: it is cancelled and named.
    with pytest.warns(RuntimeWarning, match="still running after shutdown"):
        with TestClient(_http_only(handler)) as client:
            with pytest.raises(TimeoutError, match="did not stop"):
                with client.stream("GET", "/", timeout=0.05) as response:
                    next(response.iter_bytes())


async def test_cancelling_an_async_request_cancels_the_app() -> None:
    state = {"cancelled": False}
    started = asyncio.Event()

    async def handler(scope, receive, send):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async with AsyncTestClient(_http_only(handler)) as client:
        task = asyncio.ensure_future(client.get("/"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert state["cancelled"] is True


def test_non_latin1_query_strings_are_sent_as_utf8() -> None:
    seen: dict[str, bytes] = {}

    async def handler(scope, receive, send):
        seen["qs"] = scope["query_string"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with TestClient(_http_only(handler)) as client:
        client.get("/?name=Δ")
    assert seen["qs"] == "name=Δ".encode()


# -- foreign lifespan corner cases ---------------------------------------------------


def test_lifespan_startup_that_never_answers_times_out() -> None:
    async def app(scope, receive, send):
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError, match="lifespan startup"):
        with TestClient(app, timeout=0.05):
            pass  # pragma: no cover


def test_lifespan_app_that_returns_after_startup() -> None:
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            return
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with TestClient(app) as client:
        assert client.get("/").status_code == 204


def test_lifespan_shutdown_failure_is_raised() -> None:
    async def app(scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.failed", "message": "flush failed"})

    with pytest.raises(RuntimeError, match="flush failed"):
        with TestClient(app):
            pass


def test_lifespan_app_that_hangs_after_shutdown_is_cancelled() -> None:
    async def app(scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})
        await asyncio.Event().wait()

    with TestClient(app, timeout=0.05):
        pass


# -- client corner cases ------------------------------------------------------------------


@controller("/")
class Boom:
    @get("/boom")
    async def boom(self) -> dict:
        raise RuntimeError("kaboom")

    @get("/ok")
    async def ok(self, request: Request) -> dict:
        return {"ok": True}


@module(controllers=[Boom])
class BoomModule:
    pass


def test_requests_without_the_marker_are_ignored_by_the_error_listener() -> None:
    with TestClient(LaurenFactory.create(BoomModule), raise_server_exceptions=True) as client:
        client.get("/ok")
        client.raise_server_exceptions = False
        assert client.get("/boom").status_code == 500
        client.raise_server_exceptions = True
        with pytest.raises(RuntimeError):
            client.get("/boom")


def test_raise_server_exceptions_on_a_foreign_app_only_propagates_app_errors() -> None:
    async def handler(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b"oops"})

    with TestClient(_http_only(handler), raise_server_exceptions=True) as client:
        assert client.get("/").status_code == 500


@pytest.mark.parametrize("method", ["resolve", "set_session"])
def test_lauren_only_helpers_reject_foreign_apps(method: str) -> None:
    async def handler(scope, receive, send):  # pragma: no cover - never called
        pass

    with TestClient(_http_only(handler)) as client:
        with pytest.raises(TypeError, match="Lauren app"):
            getattr(client, method)(object() if method == "resolve" else {"a": 1})


def test_websocket_explicit_host_and_cookie_headers_win() -> None:
    seen: dict[str, list] = {}

    async def app(scope, receive, send):
        if scope["type"] != "websocket":
            return
        seen["headers"] = scope["headers"]
        await receive()
        await send({"type": "websocket.accept"})
        message = await receive()
        if message["type"] == "websocket.receive":
            await send({"type": "websocket.send", "bytes": message["bytes"]})
        await send({"type": "websocket.close", "code": 1000})

    with TestClient(app) as client:
        client.cookies.set("jar", "1")
        with client.websocket_connect("/ws", headers={"host": "h", "cookie": "c=2"}) as ws:
            ws.send_bytes(b"\x01")
            assert ws.receive_bytes() == b"\x01"
    assert (b"host", b"h") in seen["headers"] and (b"cookie", b"c=2") in seen["headers"]
    assert all(v != b"jar=1" for _, v in seen["headers"])


def test_ws_test_client_accepts_mapping_headers() -> None:
    seen: dict[str, list] = {}

    async def app(scope, receive, send):
        seen["headers"] = scope["headers"]
        await receive()
        await send({"type": "websocket.close", "code": 1000})

    async def run() -> None:
        async with WsTestClient(app).connect("/", headers={"X-Token": "t"}) as ws:
            assert ws.close_code == 1000

    asyncio.run(run())
    assert (b"X-Token", b"t") in seen["headers"]


def test_sync_stream_refuses_async_close() -> None:
    with TestClient(LaurenFactory.create(BoomModule)) as client:
        with client.stream("GET", "/ok") as response:
            with pytest.raises(RuntimeError, match="sync TestClient"):
                asyncio.run(response.aclose())
            response.read()


def test_arequest_from_inside_the_apps_loop_runs_inline() -> None:
    holder: dict[str, Any] = {}
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/inner")
    async def inner() -> dict:
        return {"inner": True}

    @app.get("/outer")
    async def outer() -> dict:
        response = await holder["client"].arequest("GET", "/inner")
        return {"via": response.json()}

    with TestClient(app) as client:
        holder["client"] = client
        assert client.get("/outer").json() == {"via": {"inner": True}}


async def test_async_child_after_the_owner_closed_raises() -> None:
    app = LaurenFactory.create(BoomModule)
    owner = AsyncTestClient(app)
    await owner.get("/ok")
    child = owner.new_session()
    await child.get("/ok")
    await owner.aclose()
    with pytest.raises(RuntimeError, match="was closed"):
        await child.get("/ok")
    await child.aclose()


async def test_async_capture_before_start_uses_the_apps_bus() -> None:
    from lauren import RequestComplete

    client = AsyncTestClient(LaurenFactory.create(BoomModule))
    with client.capture(RequestComplete) as events:
        await client.get("/ok")
    await client.aclose()
    assert len(events) == 1


async def test_lifespan_helper_with_an_already_started_app() -> None:
    app = LaurenFactory.create(BoomModule)
    await app.startup()
    async with lifespan(app) as same:
        assert same is app
    assert app._shutdown_running
