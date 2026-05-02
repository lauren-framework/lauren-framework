# Socket.IO

> Lauren ships a first-class **Socket.IO v5 / Engine.IO v4** adapter. The official
> Socket.IO JavaScript, TypeScript, Swift, and Kotlin clients connect with no
> wire-level work in your code. The adapter reuses the existing WebSocket runtime —
> DI, lifecycle hooks, middleware, and strict inheritance rules all apply.

## When to reach for Socket.IO

Socket.IO adds its own protocol on top of WebSockets (session IDs, acknowledgements,
automatic reconnect). Reach for it when:

* You ship an **official Socket.IO client** (the JS/TS npm package, the mobile SDKs)
  and want first-class ACK support.
* You need **event namespacing** via the Socket.IO event name rather than building your
  own dispatch protocol on top of raw WebSocket frames.

For raw WebSocket gateways (custom protocols, non-Socket.IO clients), use
[`@ws_controller`](websockets.md) directly.

---

## A minimal Socket.IO controller

```python title="app/gateways.py"
from lauren.socketio import socketio_controller, on_socketio_event, SocketIOConnection

@socketio_controller("/socket.io/")
class ChatGateway:
    @on_socketio_event("connect")
    async def on_connect(self, conn: SocketIOConnection) -> None:
        await conn.emit("welcome", {"sid": conn.sid})

    @on_socketio_event("chat:message")
    async def on_message(self, conn: SocketIOConnection, payload: dict) -> dict:
        # Returning a value sends an ACK back to the sender automatically.
        return {"echo": payload}

    @on_socketio_event("disconnect")
    async def on_disconnect(self, conn: SocketIOConnection) -> None:
        print(f"Client {conn.sid} disconnected")
```

Register it in a module exactly like an HTTP controller or WebSocket gateway:

```python title="app/main.py"
from lauren import LaurenFactory, module
from app.gateways import ChatGateway

@module(controllers=[ChatGateway])
class AppModule: pass

app = LaurenFactory.create(AppModule)
```

JavaScript client:

```javascript
import { io } from "socket.io-client";

const socket = io("http://localhost:8000", { transports: ["websocket"] });

socket.on("connect", () => console.log("connected", socket.id));
socket.on("welcome", (data) => console.log("welcome", data));

// Emit with an ACK callback:
socket.emit("chat:message", { text: "hi" }, (ack) => {
    console.log("echo:", ack);  // { "echo": { "text": "hi" } }
});
```

!!! note "WebSocket-only transport"
    The adapter implements the **WebSocket transport** of Engine.IO v4 only. Long-polling
    fallback is out of scope. Pass `transports: ["websocket"]` in the client to skip the
    polling upgrade handshake.

---

## `@socketio_controller(path, ...)`

Mounts a class as a Socket.IO endpoint.

```python
@socketio_controller(
    "/socket.io/",
    ping_interval_ms=25_000,   # Engine.IO ping interval (default 25 s)
    ping_timeout_ms=20_000,    # Engine.IO ping timeout (default 20 s)
    max_payload_bytes=1_000_000,  # Max incoming frame size (default 1 MB)
)
class MyGateway:
    ...
```

Under the hood, `@socketio_controller` does three things:

1. Discovers all `@on_socketio_event`-marked methods.
2. Synthesizes `@on_connect` / `@on_message("*")` / `@on_disconnect` hooks that drive
   the Engine.IO + Socket.IO protocol state machine.
3. Applies `@ws_controller(path)` so the existing WebSocket runtime (DI, middleware,
   lifecycle) mounts the route.

The synthesized hooks live on the class's own `__dict__`, never a parent class, so
Lauren's **strict inheritance rule** is preserved.

### Strict inheritance

Like every other class-level decorator in Lauren, `@socketio_controller` does **not**
propagate to subclasses. A subclass must re-decorate:

```python
@socketio_controller("/v1/chat")
class ChatV1: ...

class ChatV2(ChatV1):
    pass   # NOT a Socket.IO controller — registering raises MetadataInheritanceError

@socketio_controller("/v2/chat")
class ChatV2(ChatV1):
    pass   # OK — explicit opt-in
```

---

## `@on_socketio_event(event_name)`

Marks a method as the handler for a named Socket.IO event. Always requires parentheses.

```python
@on_socketio_event("chat:message")
async def handle(self, conn: SocketIOConnection, payload: dict) -> dict:
    ...
```

### Reserved names

Two names map to lifecycle hooks rather than inbound events:

| Name | Runs when | Notes |
|---|---|---|
| `"connect"` | After the Socket.IO handshake completes | Raise an exception to reject the connection with `CONNECT_ERROR` |
| `"disconnect"` | When the transport closes (peer or server) | Cleanup subscriptions, broadcast "user left", etc. |

### Handler signature

1. `self` — the controller instance (DI-built, request-scoped).
2. `conn: SocketIOConnection` — the per-connection facade.
3. **Positional payload args** — the JSON arguments the client sent with `socket.emit(event, ...args)`.

Excess inbound args are silently dropped. Missing declared args are padded with `None`.

### ACKs (automatic)

Return a value from the handler and Lauren forwards it as the ACK args when the client
supplied an ACK callback (`socket.emit(event, payload, callback)`):

```python
@on_socketio_event("ping")
async def ping(self, conn: SocketIOConnection) -> dict:
    return {"pong": True}     # automatically sent as the ACK
```

Return a `tuple` to send multiple ACK args:

```python
@on_socketio_event("echo")
async def echo(self, conn: SocketIOConnection, value: str) -> tuple:
    return (value, len(value))    # two ACK args
```

Returning `None` sends a single `null` arg to the ACK callback.

---

## `SocketIOConnection`

The per-connection facade passed to every event handler.

### Properties

| Property | Type | Description |
|---|---|---|
| `conn.sid` | `str` | Stable session identifier, generated at handshake time. |
| `conn.namespace` | `str` | Socket.IO namespace (`"/"` always). |
| `conn.connected` | `bool` | `True` while the underlying transport is open. |
| `conn.websocket` | `WebSocket` | Escape hatch to the raw Lauren `WebSocket`. |
| `conn.app_state` | `State` | App-level state (sealed after startup). |

### `emit(event, *args)`

Send a Socket.IO event to this client:

```python
await conn.emit("notification", {"type": "mention", "user": "ada"})
await conn.emit("batch", item1, item2, item3)   # multiple args
```

Outbound emits go through a per-connection `asyncio.Lock` so concurrent calls from
different coroutines never interleave frames.

### `send_ack(ack_id, *args)`

Manually send an ACK for an event the client emitted with a callback. Most code doesn't
call this directly — returning from the handler triggers it automatically. Use
`send_ack` when you need to acknowledge asynchronously after kicking off a background
task:

```python
@on_socketio_event("start_job")
async def start_job(self, conn: SocketIOConnection, config: dict) -> None:
    handle = self._scheduler.queue(config, on_done=lambda r: asyncio.create_task(
        conn.send_ack(ack_id, r)
    ))
    # Return None — ACK is sent asynchronously when the job completes.
```

### `disconnect()`

Gracefully close the connection from the server side:

```python
await conn.disconnect()
```

Sends the Socket.IO `DISCONNECT` packet, the Engine.IO `CLOSE` packet, and closes the
underlying WebSocket in sequence. Idempotent.

---

## Dependency injection

`@socketio_controller` auto-applies `@injectable(scope=Scope.REQUEST)` via
`@ws_controller`. Each connection gets its own gateway instance. The constructor can
take any DI dependency:

```python
from lauren import injectable, Scope
from lauren.socketio import socketio_controller, on_socketio_event, SocketIOConnection

@socketio_controller("/socket.io/")
class RoomGateway:
    def __init__(self, repo: RoomRepository) -> None:
        self._repo = repo

    @on_socketio_event("connect")
    async def joined(self, conn: SocketIOConnection) -> None:
        rooms = await self._repo.list_public()
        await conn.emit("rooms", rooms)
```

Register `RoomRepository` as a provider in the module:

```python
@module(controllers=[RoomGateway], providers=[RoomRepository])
class AppModule: pass
```

---

## Broadcasting with `BroadcastGroup`

Socket.IO controllers share the `BroadcastGroup` provider with plain WebSocket
gateways. Inject it via the constructor:

```python
from lauren import BroadcastGroup
from lauren.socketio import socketio_controller, on_socketio_event, SocketIOConnection

@socketio_controller("/socket.io/")
class ChatGateway:
    def __init__(self, rooms: BroadcastGroup) -> None:
        self._rooms = rooms

    @on_socketio_event("connect")
    async def on_connect(self, conn: SocketIOConnection) -> None:
        await conn.emit("welcome", {"sid": conn.sid})

    @on_socketio_event("join")
    async def join_room(self, conn: SocketIOConnection, data: dict) -> None:
        room = data.get("room", "lobby")
        await self._rooms.subscribe(room, conn.websocket)
        await self._rooms.broadcast(room, {"event": "joined", "sid": conn.sid}, exclude=conn.websocket)

    @on_socketio_event("message")
    async def send_message(self, conn: SocketIOConnection, data: dict) -> None:
        room = data.get("room", "lobby")
        await self._rooms.broadcast(room, {"event": "message", "text": data.get("text", "")})

    @on_socketio_event("disconnect")
    async def on_disconnect(self, conn: SocketIOConnection) -> None:
        await self._rooms.unsubscribe_all(conn.websocket)
```

---

## Error handling

### Connection rejection

Raise any exception in the `"connect"` handler to reject the connection. The adapter
sends a `CONNECT_ERROR` packet so the JS client's `connect_error` listener fires:

```python
@on_socketio_event("connect")
async def on_connect(self, conn: SocketIOConnection) -> None:
    token = conn.websocket.headers.get("authorization", "")
    if not token.startswith("Bearer "):
        raise PermissionError("missing token")   # → CONNECT_ERROR to client
    conn.websocket.state.set("user_id", self._jwt.decode(token[7:])["sub"])
    await conn.emit("welcome", {"sid": conn.sid})
```

### Event handler errors

Exceptions from event handlers (other than `"connect"`) are caught by the adapter. If
the event had an ACK callback, the error is forwarded as `{"error": str(exc)}`. The
framework's `@on_error` hook on the underlying `@ws_controller` is not automatically
fired — the Socket.IO adapter manages its own error lifecycle.

---

## Out-of-scope limitations

| Feature | Status |
|---|---|
| HTTP long-polling transport | Not supported — use `transports: ["websocket"]` |
| Binary attachments (`BINARY_EVENT` / `BINARY_ACK`) | Not supported |
| Multiple Socket.IO namespaces per controller | Not supported — use multiple `@socketio_controller` classes at different paths |
| Client-side `socket.rooms` membership | Not built-in — use `BroadcastGroup` |

---

## Testing

Drive a Socket.IO controller through `WsTestClient`:

```python
import asyncio, json
from lauren.testing import WsTestClient

async def test_chat_echo():
    app = LaurenFactory.create(AppModule)
    client = WsTestClient(app)
    async with client.connect("/socket.io/") as ws:
        # Engine.IO OPEN frame
        open_frame = await ws.receive_text()
        # Socket.IO CONNECT ack
        connect_ack = await ws.receive_text()

        # Send a chat:message event (Socket.IO wire format)
        payload = json.dumps([4, "chat:message", {"text": "hello"}])
        await ws.send_text("4" + json.dumps(["chat:message", {"text": "hello"}]))

        # Read ACK
        raw = await ws.receive_text()
        ...
```

!!! tip
    For simpler integration tests, consider using the actual `socket.io-client` npm
    package in a subprocess or via `anyio.run_process`. The WsTestClient works but
    requires manual Engine.IO / Socket.IO framing.

---

## See also

* [WebSockets](websockets.md) — raw WebSocket gateways for custom protocols or non-Socket.IO clients.
* [Custom Guards](custom-guards.md) — HTTP-style guard composition (authentication checks belong in the `"connect"` handler for Socket.IO).
* [Reference → Cheat Sheet](../reference/cheat-sheet.md) — one-line patterns.
