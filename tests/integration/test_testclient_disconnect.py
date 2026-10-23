"""Server-faithful ``receive()`` and simulated client aborts (``disconnect_after=``)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import LaurenFactory, Request, controller, module, post
from lauren.testing import AsyncTestClient, TestClient


class Recorder:
    """Raw ASGI app recording what ``receive()`` returns and when."""

    def __init__(self, *, wait_for_disconnect_first: bool = False, respond: bool = True) -> None:
        self.messages: list[str] = []
        self.wait_first = wait_for_disconnect_first
        self.respond = respond
        self.finished = False
        self.body = b""

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return  # no lifespan support
        try:
            while True:
                message = await receive()
                self.messages.append(message["type"])
                if message["type"] == "http.disconnect":
                    break
                self.body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            if self.wait_first:
                self.messages.append((await receive())["type"])
            if self.respond:
                await send({"type": "http.response.start", "status": 200, "headers": [(b"x-a", b"1")]})
                await send({"type": "http.response.body", "body": b"partial", "more_body": True})
                self.messages.append("sent-final")
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                # After the final body chunk the client is gone.
                self.messages.append((await receive())["type"])
        finally:
            self.finished = True


def test_receive_reports_disconnect_only_after_the_final_body_chunk() -> None:
    app = Recorder()
    with TestClient(app) as client:
        response = client.post("/", content=b"payload")
    assert app.messages == ["http.request", "sent-final", "http.disconnect"]
    assert (response.status_code, response.content, response.aborted) == (200, b"partial", False)
    assert app.body == b"payload"


def test_disconnect_after_wakes_a_blocked_receive() -> None:
    app = Recorder(wait_for_disconnect_first=True)
    with TestClient(app) as client:
        response = client.get("/", disconnect_after=0.01)
    assert app.messages[:2] == ["http.request", "http.disconnect"]
    assert response.aborted is True
    assert response.status_code is None and response.content == b""
    assert app.finished


def test_aborted_call_returns_only_after_the_app_finished() -> None:
    app = Recorder(wait_for_disconnect_first=True)
    with TestClient(app) as client:
        client.get("/", disconnect_after=0.01)
        assert app.finished is True


def test_disconnect_after_that_fires_after_completion_is_ignored() -> None:
    app = Recorder()
    with TestClient(app) as client:
        response = client.get("/", disconnect_after=30)
    assert response.aborted is False


def test_streamed_request_body_is_delivered_chunk_by_chunk() -> None:
    app = Recorder()
    with TestClient(app) as client:
        client.post("/", content=iter([b"a", b"b", "c"]))
    assert app.body == b"abc"
    assert app.messages[:4] == ["http.request"] * 4  # three chunks + the final empty message


def test_async_iterable_request_body() -> None:
    async def chunks():
        yield b"x"
        yield b"y"

    app = Recorder()
    with TestClient(app) as client:
        client.post("/", content=chunks())
    assert app.body == b"xy"


def test_disconnect_while_body_streams_truncates_the_body() -> None:
    async def endless():
        yield b"first"
        await asyncio.Event().wait()
        yield b"never"  # pragma: no cover

    app = Recorder(respond=False)
    with TestClient(app) as client:
        response = client.post("/", content=endless(), disconnect_after=0.05)
    assert app.body == b"first"
    assert app.messages == ["http.request", "http.disconnect"]
    assert response.aborted is True


def test_request_body_on_lauren_stops_at_disconnect() -> None:
    seen: dict[str, bytes] = {}

    @controller("/")
    class Upload:
        @post("/upload")
        async def upload(self, request: Request) -> dict:
            seen["body"] = await request.body()
            return {}

    @module(controllers=[Upload])
    class UploadModule:
        pass

    async def endless():
        yield b"part-1;"
        await asyncio.Event().wait()
        yield b"never"  # pragma: no cover

    with TestClient(LaurenFactory.create(UploadModule)) as client:
        response = client.post("/upload", content=endless(), disconnect_after=0.05)
    assert seen["body"] == b"part-1;"  # Request.body() treats http.disconnect as end of body
    assert response.aborted is True


def test_send_after_abort_is_dropped() -> None:
    async def late_responder(scope, receive, send):
        if scope["type"] != "http":
            return
        await receive()
        assert (await receive())["type"] == "http.disconnect"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"too late"})

    with TestClient(late_responder) as client:
        response = client.get("/", disconnect_after=0.01)
    assert (response.status_code, response.content, response.aborted) == (None, b"", True)


def test_app_that_never_responds_is_reported_as_500() -> None:
    async def silent(scope, receive, send):
        await receive()

    with TestClient(silent) as client:
        assert client.get("/").status_code == 500


def test_app_exceptions_propagate() -> None:
    async def broken(scope, receive, send):
        raise LookupError("boom")

    with TestClient(broken) as client:
        with pytest.raises(LookupError, match="boom"):
            client.get("/")


async def test_async_client_disconnect_after() -> None:
    app = Recorder(wait_for_disconnect_first=True)
    async with AsyncTestClient(app) as client:
        response = await client.get("/", disconnect_after=0.01)
    assert response.aborted is True
    assert app.finished
