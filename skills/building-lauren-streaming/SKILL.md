---
name: building-lauren-streaming
description: Writes Lauren SSE endpoints with EventStream/ServerSentEvent and WebSocket gateways with ws_controller/on_connect/on_message/on_disconnect/BroadcastGroup. Use when adding real-time streaming or bidirectional communication to a Lauren app.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Lauren Streaming

## Server-Sent Events (SSE)

SSE sends a one-way stream from server to browser over plain HTTP. The browser reconnects automatically.

```python
from lauren import controller, get
from lauren.sse import EventStream, ServerSentEvent

@controller("/events")
class EventsController:
    def __init__(self, queue: EventQueue) -> None:
        self._q = queue

    @get("/stream")
    async def stream(self) -> EventStream:
        async def producer():
            async for event in self._q.subscribe():
                yield ServerSentEvent(
                    event=event.kind,     # maps to EventSource.addEventListener("kind", …)
                    data=event.payload,   # any JSON-serializable value or string
                    id=str(event.seq),    # Last-Event-ID on reconnect
                )

        return EventStream(producer(), keep_alive=15.0)
```

### ServerSentEvent fields

```python
ServerSentEvent(
    data={"msg": "hello"},   # JSON / str / bytes / Pydantic model — required for data events
    event="chat.message",    # optional event type name
    id="42",                 # optional event ID for resumability
    retry=3000,              # optional reconnect delay in ms
    comment="keep-alive",    # emits a `: comment\n\n` line (no data)
)
```

- `data=None` with `comment=...` → comment-only (heartbeat) frame.
- Plain `str` or `dict` items yielded from the async generator are auto-promoted to `ServerSentEvent`.
- JSON payloads use Lauren's active encoder, so app-level `json_encoder=` and
  HTTP `@use_encoder(...)` overrides affect SSE framing too.

### EventStream options

```python
EventStream(
    generator,
    keep_alive=15.0,              # heartbeat every 15 seconds (default: None = disabled)
    keep_alive_comment="ping",    # text used in the heartbeat comment line
)
```

`keep_alive` prevents intermediary proxies from closing idle connections.

If the stream mostly emits Pydantic models, route it through the Pydantic-native encoder:

```python
from lauren import use_encoder
from lauren.serialization import PydanticEncoder

@get("/stream")
@use_encoder(PydanticEncoder())
async def stream(self) -> EventStream:
    ...
```

### Typed streaming alternative

For a single typed schema (Pydantic, dataclass, `msgspec.Struct`, or `Discriminated[A|B,"key"]`), use `StreamingResponse[T]` (content-negotiated SSE / NDJSON):

```python
from lauren.streaming import StreamingResponse

@get("/typed")
async def typed_stream(self) -> StreamingResponse[EventDto]:
    async def gen():
        async for item in self._svc.subscribe():
            yield item  # EventDto instances
    return StreamingResponse(gen())
```

Choose `EventStream` for heterogeneous events or explicit SSE control; choose `StreamingResponse[T]` for a single schema.

## WebSocket gateways

See [websockets.md](websockets.md) for the full WebSocket API.

```python
from lauren.websockets import ws_controller, on_connect, on_disconnect, on_message, WebSocket

@ws_controller("/chat/{room_id}")
class ChatGateway:
    def __init__(self, rooms: BroadcastGroup) -> None:
        self._rooms = rooms

    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        room = ws.path_params["room_id"]
        await ws.accept()
        await self._rooms.subscribe(room, ws)

    @on_message("chat.send")
    async def send(self, ws: WebSocket, body: Json[ChatMessage]) -> None:
        room = ws.path_params["room_id"]
        await self._rooms.broadcast(room, {"msg": body.text})

    @on_disconnect
    async def left(self, ws: WebSocket) -> None:
        await self._rooms.unsubscribe_all(ws)
```

Register in a module just like an HTTP controller:

```python
@module(controllers=[ChatGateway], providers=[BroadcastGroup])
class ChatModule:
    pass
```

When you call `ws.send_json(...)`, the gateway uses the app-wide encoder from
`LaurenFactory.create(..., json_encoder=...)` rather than silently falling back
to the stdlib JSON path.

## Socket.IO adapter

`socketio_controller` / `on_socketio_event` let official Socket.IO v4+ clients
talk to a lauren backend over WebSocket transport — no wire-level protocol work needed:

```python
from lauren import module
from lauren.socketio import SocketIOConnection, on_socketio_event, socketio_controller

@socketio_controller("/socket.io/")
class ChatGateway:
    @on_socketio_event("connect")
    async def on_connect(self, conn: SocketIOConnection) -> None:
        await conn.emit("welcome", {"sid": conn.sid})

    @on_socketio_event("chat:message")
    async def on_message(self, conn: SocketIOConnection, payload: dict) -> dict:
        return {"echo": payload}  # ACK back to sender

    @on_socketio_event("disconnect")
    async def on_disconnect(self, conn: SocketIOConnection) -> None:
        ...

@module(controllers=[ChatGateway])
class App:
    pass

app = LaurenFactory.create(App)
```

Key differences from raw `@ws_controller`:
- `SocketIOConnection` provides `emit(event, data)`, `sid`, and `rooms`
- `@on_socketio_event("connect")` fires after the Engine.IO handshake
- Returning a dict from the handler sends a Socket.IO ACK to the sender
- Compatible with the official `socket.io-client` JS/TS/Swift/Kotlin SDKs
