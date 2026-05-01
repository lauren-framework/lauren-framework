# Lauren WebSockets — Reference

## Contents
- [Gateway skeleton](#gateway-skeleton)
- [WebSocket object](#websocket-object)
- [Message dispatch](#message-dispatch)
- [BroadcastGroup](#broadcastgroup)
- [Wiring into a module](#wiring-into-a-module)

---

## Gateway skeleton

```python
from lauren.websockets import (
    ws_controller, on_connect, on_disconnect, on_message, WebSocket, BroadcastGroup
)
from lauren.types import Json

@ws_controller("/ws/{room_id}", tags=["chat"])
class ChatGateway:
    """WebSocket gateway — one instance per connection (REQUEST-scoped by default)."""

    def __init__(self, rooms: BroadcastGroup) -> None:
        self._rooms = rooms

    @on_connect
    async def connected(self, ws: WebSocket) -> None:
        await ws.accept()
        room = ws.path_params["room_id"]
        await self._rooms.subscribe(room, ws)
        await ws.send_json({"event": "connected", "room": room})

    @on_message("chat.send")
    async def on_chat(self, ws: WebSocket, body: Json[ChatMessage]) -> None:
        room = ws.path_params["room_id"]
        await self._rooms.broadcast(room, {"event": "chat.message", "text": body.text})

    @on_message("*")          # catch-all for unrecognised events
    async def unknown(self, ws: WebSocket) -> None:
        await ws.send_json({"error": "unknown event"})

    @on_disconnect
    async def disconnected(self, ws: WebSocket) -> None:
        await self._rooms.unsubscribe_all(ws)
```

### Decorator rules

| Decorator | Fires when | Notes |
|---|---|---|
| `@on_connect` | Handshake completes | Call `ws.accept()` here; must be called before sending |
| `@on_message("event")` | Frame with matching `"event"` key arrives | Multiple decorators stack on one method |
| `@on_message("*")` | Any unhandled event | Wildcard catch-all |
| `@on_message("__binary__")` | Raw binary frames | Receives `bytes` payload |
| `@on_disconnect` | Connection closes (either side) | Best-effort; exceptions are logged |

Decorators **do not** inherit to subclasses — re-decorate in the subclass.

---

## WebSocket object

```python
ws: WebSocket

# Introspection
ws.path            # "/ws/room1"
ws.path_params     # {"room_id": "room1"}
ws.headers         # Headers — request headers from handshake
ws.query_string    # bytes
ws.state           # per-connection mutable State
ws.connected       # bool

# Handshake
await ws.accept(subprotocol="chat")   # must be called before sending
await ws.close(code=1000, reason="done")

# Sending
await ws.send_text("hello")
await ws.send_bytes(b"\x00\x01")
await ws.send_json({"event": "pong"})

# Receiving (low-level — usually not needed; use @on_message instead)
text = await ws.receive_text()
data = await ws.receive_bytes()
obj  = await ws.receive_json()
```

---

## Message dispatch

Inbound frames must be JSON objects with an `"event"` string field:

```json
{"event": "chat.send", "text": "hello"}
```

Lauren maps `event` → the matching `@on_message` handler, then extracts `Json[T]` parameters from the same frame object. Path/query/DI extractors also work in `@on_message` methods.

Frames that fail JSON decode → sent back as a structured error if `@on_message("__binary__")` is not declared.

---

## BroadcastGroup

Fan-out to a named set of connections. Safe for single-process deployments.

```python
from lauren.websockets import BroadcastGroup

# Provided as a singleton via DI; inject into the gateway
class ChatGateway:
    def __init__(self, rooms: BroadcastGroup) -> None:
        self._rooms = rooms

    @on_connect
    async def connected(self, ws: WebSocket) -> None:
        await ws.accept()
        await self._rooms.subscribe("room1", ws)

    @on_disconnect
    async def gone(self, ws: WebSocket) -> None:
        await self._rooms.unsubscribe_all(ws)

# Broadcasting
await rooms.broadcast("room1", {"msg": "hello"})        # to all in group
await rooms.broadcast("room1", data, exclude={ws})      # skip the sender
await rooms.broadcast_text("room1", "raw text")

# Membership
await rooms.subscribe("room1", ws)
await rooms.unsubscribe("room1", ws)
await rooms.unsubscribe_all(ws)       # remove ws from every group
groups = rooms.groups()               # names of non-empty groups
members = rooms.members("room1")      # frozenset[WebSocket]
```

For multi-process (multiple uvicorn workers), subclass `BroadcastGroup` and back it with Redis Pub/Sub.

---

## Wiring into a module

```python
from lauren import module
from lauren.websockets import BroadcastGroup

@module(
    controllers=[ChatGateway],        # ws_controller classes go in controllers=
    providers=[BroadcastGroup],       # BroadcastGroup is a regular DI provider
)
class ChatModule:
    pass
```
