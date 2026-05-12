"""Integration tests for skill 23: WebSocket Connection Pool & Room Management."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lauren import Json, LaurenFactory, Scope, injectable, module
from lauren.testing import WsTestClient
from lauren.websockets import (
    BroadcastGroup,
    WebSocket,
    on_connect,
    on_disconnect,
    on_message,
    ws_controller,
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    room: str
    content: str


# ---------------------------------------------------------------------------
# Injectable BroadcastGroup subclass
# BroadcastGroup itself is not @injectable — subclass it and register.
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class ChatRooms(BroadcastGroup):
    pass


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


@ws_controller("/ws/chat")
class ChatGateway:
    def __init__(self, rooms: ChatRooms) -> None:
        self._rooms = rooms

    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        pass  # accept is implicit

    @on_message("join")
    async def join_room(self, ws: WebSocket, body: Json[dict]) -> None:
        room = body.get("room", "general")
        await self._rooms.subscribe(room, ws)
        await ws.send_json({"event": "joined", "room": room})

    @on_message("send")
    async def send_message(self, ws: WebSocket, body: Json[ChatMessage]) -> None:
        await self._rooms.broadcast(body.room, {"event": "message", "content": body.content})

    @on_message("leave")
    async def leave_room(self, ws: WebSocket, body: Json[dict]) -> None:
        room = body.get("room", "general")
        await self._rooms.unsubscribe(room, ws)
        await ws.send_json({"event": "left", "room": room})

    @on_disconnect
    async def disconnect(self, ws: WebSocket) -> None:
        await self._rooms.unsubscribe_all(ws)


@module(controllers=[ChatGateway], providers=[ChatRooms])
class ChatModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebSocketRooms:
    def test_join_room_receives_joined_event(self) -> None:
        app = LaurenFactory.create(ChatModule)

        async def run() -> None:
            async with WsTestClient(app).connect("/ws/chat") as ws:
                await ws.send_json({"event": "join", "room": "general"})
                reply = await ws.receive_json()
                assert reply["event"] == "joined"
                assert reply["room"] == "general"

        asyncio.run(run())

    def test_join_default_room(self) -> None:
        app = LaurenFactory.create(ChatModule)

        async def run() -> None:
            async with WsTestClient(app).connect("/ws/chat") as ws:
                await ws.send_json({"event": "join"})
                reply = await ws.receive_json()
                assert reply["room"] == "general"

        asyncio.run(run())

    def test_broadcast_to_room_members(self) -> None:
        app = LaurenFactory.create(ChatModule)

        async def run() -> None:
            client = WsTestClient(app)
            async with client.connect("/ws/chat") as ws1:
                async with client.connect("/ws/chat") as ws2:
                    # Both join the same room
                    await ws1.send_json({"event": "join", "room": "lobby"})
                    await ws1.receive_json()  # joined ack

                    await ws2.send_json({"event": "join", "room": "lobby"})
                    await ws2.receive_json()  # joined ack

                    # ws1 sends a message — both should receive it
                    await ws1.send_json({"event": "send", "room": "lobby", "content": "hello everyone"})

                    msg1 = await ws1.receive_json()
                    msg2 = await ws2.receive_json()

                    assert msg1["event"] == "message"
                    assert msg1["content"] == "hello everyone"
                    assert msg2["event"] == "message"
                    assert msg2["content"] == "hello everyone"

        asyncio.run(run())

    def test_message_only_goes_to_subscribed_room(self) -> None:
        app = LaurenFactory.create(ChatModule)

        received: list[dict] = []

        async def run() -> None:
            client = WsTestClient(app)
            async with client.connect("/ws/chat") as ws1:
                async with client.connect("/ws/chat") as ws2:
                    await ws1.send_json({"event": "join", "room": "room-a"})
                    await ws1.receive_json()

                    await ws2.send_json({"event": "join", "room": "room-b"})
                    await ws2.receive_json()

                    # ws1 broadcasts to room-a; ws2 should NOT receive it
                    await ws1.send_json({"event": "send", "room": "room-a", "content": "private"})
                    msg = await ws1.receive_json()
                    received.append(msg)
                    assert msg["content"] == "private"

        asyncio.run(run())
        assert len(received) == 1

    def test_leave_room(self) -> None:
        app = LaurenFactory.create(ChatModule)

        async def run() -> None:
            async with WsTestClient(app).connect("/ws/chat") as ws:
                await ws.send_json({"event": "join", "room": "test-room"})
                await ws.receive_json()  # joined

                await ws.send_json({"event": "leave", "room": "test-room"})
                reply = await ws.receive_json()
                assert reply["event"] == "left"
                assert reply["room"] == "test-room"

        asyncio.run(run())
