---
name: websocket-rooms
description: Implements WebSocket connection pooling and room-based broadcast in a Lauren application using BroadcastGroup. Use when building chat rooms, live feeds, or any feature where multiple WebSocket connections need to receive the same messages.
---

> Use `codemap find "BroadcastGroup"` to check if a broadcast group is already registered before adding a new one.

# WebSocket Connection Pool & Room Management

`BroadcastGroup` is Lauren's built-in injectable for managing named rooms. Each room is identified by a string key; connections subscribe and receive broadcasts atomically.

## Gateway implementation

```python
from __future__ import annotations
from lauren import injectable, Scope, module, Json
from lauren.websockets import (
    ws_controller, on_connect, on_disconnect, on_message,
    WebSocket, BroadcastGroup,
)
from pydantic import BaseModel

class ChatMessage(BaseModel):
    room: str
    content: str

# BroadcastGroup is not @injectable by default — subclass and register it.
@injectable(scope=Scope.SINGLETON)
class ChatRooms(BroadcastGroup):
    pass

@ws_controller("/ws/chat")
class ChatGateway:
    def __init__(self, rooms: ChatRooms) -> None:
        self._rooms = rooms

    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        pass  # accept is implicit; add auth checks here

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
```

## Module registration

```python
from lauren import module

@module(imports=[ChatModule])
class AppModule:
    pass
```

## Testing with WsTestClient

```python
import asyncio
from lauren import LaurenFactory
from lauren.testing import WsTestClient

app = LaurenFactory.create(ChatModule)

async def test_join_and_broadcast():
    async with WsTestClient(app).connect("/ws/chat") as ws1:
        async with WsTestClient(app).connect("/ws/chat") as ws2:
            # Both clients join the same room
            await ws1.send_json({"event": "join", "room": "lobby"})
            r1 = await ws1.receive_json()
            assert r1["event"] == "joined"

            await ws2.send_json({"event": "join", "room": "lobby"})
            r2 = await ws2.receive_json()
            assert r2["event"] == "joined"

            # ws1 sends a message; both should receive it
            await ws1.send_json({"event": "send", "room": "lobby", "content": "hello"})
            msg1 = await ws1.receive_json()
            msg2 = await ws2.receive_json()
            assert msg1["content"] == "hello"
            assert msg2["content"] == "hello"

asyncio.run(test_join_and_broadcast())
```

## Key points

- `BroadcastGroup` must be listed in `providers` in the module that owns the gateway.
- `subscribe(room, ws)` is idempotent — calling it twice for the same connection has no effect.
- `unsubscribe_all(ws)` removes the connection from every room it joined; always call this in `@on_disconnect`.
- The default `BroadcastGroup` is in-process only. For multi-worker production deployments, subclass it with a Redis Pub/Sub backend using the same `subscribe / unsubscribe / broadcast / unsubscribe_all` surface.
- Message routing is event-name based: `{"event": "join", ...}` dispatches to the `@on_message("join")` handler.
