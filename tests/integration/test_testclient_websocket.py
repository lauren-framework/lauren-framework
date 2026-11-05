"""WebSockets through the clients: sync sessions on the client loop, jar cookies, envelopes."""

from __future__ import annotations

import asyncio

import pytest

from lauren import (
    Json,
    LaurenFactory,
    Response,
    WebSocket,
    controller,
    get,
    module,
    on_connect,
    on_message,
    ws_controller,
)
from lauren.testing import AsyncTestClient, TestClient, WebSocketTestSession, WsTestClient


@ws_controller("/chat")
class ChatGateway:
    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"event": "welcome", "data": {"cookie": ws.headers.get("cookie")}})

    @on_message("chat.send")
    async def send(self, ws: WebSocket, body: Json[dict]) -> None:
        await ws.send_json({"event": "chat.echo", "data": body})

    @on_message("raw")
    async def raw(self, ws: WebSocket) -> None:
        await ws.send_text("plain")
        await ws.send_bytes(b"\x00\x01")


@ws_controller("/silent")
class SilentGateway:
    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        await ws.accept()


@controller("/")
class LoginController:
    @get("/login")
    async def login(self) -> Response:
        return Response.json({}).with_cookie("sid", "abc")


@module(controllers=[ChatGateway, SilentGateway, LoginController])
class ChatModule:
    pass


def _app():
    return LaurenFactory.create(ChatModule)


def test_sync_websocket_round_trip_with_event_envelope() -> None:
    with TestClient(_app()) as client:
        with client.websocket_connect("/chat") as ws:
            assert ws.accepted
            assert ws.receive_event()["event"] == "welcome"
            ws.send_event("chat.send", {"text": "hi"})
            assert ws.receive_event() == {"event": "chat.echo", "data": {"text": "hi"}}


def test_websocket_carries_the_jar_cookies() -> None:
    with TestClient(_app()) as client:
        client.get("/login")
        with client.websocket_connect("/chat") as ws:
            assert ws.receive_json()["data"]["cookie"] == "sid=abc"


def test_text_bytes_and_raw_messages() -> None:
    with TestClient(_app()) as client:
        with client.websocket_connect("/chat") as ws:
            ws.receive_json()
            ws.send_json({"event": "raw"})
            assert ws.receive_text() == "plain"
            assert ws.receive_bytes() == b"\x00\x01"
            ws.send_text('{"event": "chat.send", "data": {"n": 1}}')
            assert ws.receive()["type"] == "websocket.send"


def test_receive_timeout_raises_instead_of_hanging() -> None:
    with TestClient(_app()) as client:
        with client.websocket_connect("/silent", timeout=0.05) as ws:
            with pytest.raises(TimeoutError, match="WebSocket"):
                ws.receive_text()


def test_explicit_close_reports_code() -> None:
    with TestClient(_app()) as client:
        with client.websocket_connect("/chat") as ws:
            ws.receive_json()
            ws.close(1000)
        assert ws.close_code is None or ws.close_code == 1000
        assert ws.close_reason == "" and ws.accepted_subprotocol is None


def test_unknown_route_is_rejected() -> None:
    with TestClient(_app()) as client:
        with client.websocket_connect("/nowhere") as ws:
            assert not ws.accepted
            assert ws.close_code == 1008


async def test_async_client_websocket_with_cookies() -> None:
    async with AsyncTestClient(_app()) as client:
        await client.get("/login")
        async with client.websocket_connect("/chat") as ws:
            welcome = await ws.receive_event()
            assert welcome["data"]["cookie"] == "sid=abc"
            await ws.send_event("chat.send", {"x": 1})
            assert (await ws.receive_event())["data"] == {"x": 1}


async def test_ws_test_client_server_timeout_is_configurable() -> None:
    session = WsTestClient(_app()).connect("/chat", server_timeout=0.5)
    assert isinstance(session, WebSocketTestSession)
    async with session as ws:
        await ws.receive_json()
    assert session._server_timeout == 0.5


async def test_receive_event_rejects_non_object_frames() -> None:
    @ws_controller("/list")
    class ListGateway:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json([1, 2])

    @module(controllers=[ListGateway])
    class ListModule:
        pass

    async with WsTestClient(LaurenFactory.create(ListModule)).connect("/list") as ws:
        with pytest.raises(RuntimeError, match="JSON object"):
            await ws.receive_event()


def test_server_wind_down_timeout_message_names_the_limit() -> None:
    async def run() -> None:
        session = WebSocketTestSession.__new__(WebSocketTestSession)
        session._server_timeout = 0.01
        session._server_task = asyncio.ensure_future(asyncio.sleep(60))
        with pytest.raises(RuntimeError, match=r"\(0.01s\)"):
            await session._wait_server()

    asyncio.run(run())
