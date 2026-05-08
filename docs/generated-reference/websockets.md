# WebSockets

First-class WebSocket support via `@ws_controller` gateways.

## Gateway decorators

### `ws_controller`

```python
def ws_controller(path: str = '', tags: list[str] | None = None, summary: str | None = None, description: str | None = None) -> Callable[[C], C]
```

Mark a class as a WebSocket gateway mounted at ``path``.

The ``path`` may contain ``{name}`` parameters just like HTTP routes;
they're parsed out and made available via :attr:`WebSocket.path_params`.

Per the framework-wide rule, ``@ws_controller`` attaches metadata to
the decorated class only. Subclasses do **NOT** inherit gateway
status — they must be re-decorated. This is symmetric with
:func:`~lauren.controller` and keeps inheritance explicit.

``path`` is required in practice; passing an empty string is valid
and registers the gateway at the module root. The bare form
``@ws_controller`` (no parentheses) is rejected with
:class:`~lauren.exceptions.DecoratorUsageError`.

### `on_connect`

```python
def on_connect(fn: F) -> F
```

Mark a method to run after the WebSocket handshake completes.

The marker lives on the function object itself
(``fn.__lauren_ws_on_connect__``). A subclass that overrides this
method without re-applying the decorator will NOT inherit the hook
— symmetric with how ``@get`` and ``@post`` work on HTTP
controllers.

Also accepts :class:`staticmethod` / :class:`classmethod`
descriptors so users can stack the decorator in either order
(``@on_connect`` above or below ``@staticmethod``) — the marker
lands wherever ``setattr`` will accept it.

### `on_message`

```python
def on_message(event: str, summary: str | None = None, description: str | None = None) -> Callable[[F], F]
```

Route one inbound frame event to this method.

The handler's signature declares what validated payload it expects::

    @on_message("chat.send")
    async def send(self, ws: WebSocket, body: Json[ChatMessage]) -> None: ...

During gateway compilation lauren inspects the signature once, picks
out the ``Json[...]`` / path / query / DI extractors, and builds an
immutable dispatch plan — request-time dispatch is pure lookup, no
reflection.

Multiple ``@on_message(...)`` decorators may stack on the same
method to handle several event names; each creates its own
:class:`WsMessageMeta` entry and its own dispatch-table row.

The wildcard event name ``"*"`` matches any event that has no
specific handler — useful for a catch-all logger. The special name
``"__binary__"`` captures binary frames (``bytes``) rather than
JSON-decoded text frames.

### `on_disconnect`

```python
def on_disconnect(fn: F) -> F
```

Mark a method to run when the connection closes.

Runs for both peer-initiated and server-initiated closures. The hook
is best-effort: exceptions raised here are logged but don't affect
the connection (which is already dead) or the response status of the
handshake.

### `on_error`

```python
def on_error(fn: F) -> F
```

Mark a method as the connection's error handler.

The runtime calls the decorated method with the raised exception
whenever a per-frame handler throws something other than
:class:`WebSocketDisconnect`. Returning normally resumes the
connection; raising closes it. Without ``@on_error`` lauren falls
back to sending a structured error frame and keeping the connection
open.

## WebSocket object

### `WebSocket`

```python
class WebSocket(scope: dict[str, Any], receive: Callable[[], Awaitable[dict[str, Any]]], send: Callable[[dict[str, Any]], Awaitable[None]], path_template: str, path_params: dict[str, str], app_state: Any = None)
```

A live WebSocket connection.

Instances are constructed by the ASGI runtime and passed to
``@on_connect`` / ``@on_message`` / ``@on_disconnect`` handlers that
declare a ``ws: WebSocket`` parameter. User code never instantiates
this directly.

The object is intentionally thin: it wraps the ASGI ``receive`` /
``send`` callables and exposes the subset of surface that controller
authors actually need. Typed frame reception is mediated by the
dispatcher so handlers normally work with already-validated Pydantic
models rather than raw frames; the raw :meth:`receive_text` /
:meth:`receive_bytes` / :meth:`receive_json` helpers remain
available for advanced use.

#### `WebSocket.accept`

```python
def accept(self, subprotocol: str | None = None, headers: list[tuple[str, str]] | None = None) -> None
```

Complete the WebSocket handshake.

lauren's runtime calls this for you if ``@on_connect`` returns
normally — controllers therefore only need to call it explicitly
when they want to reject the connection or negotiate a specific
subprotocol before any application logic runs.

#### `WebSocket.receive`

```python
def receive(self) -> dict[str, Any]
```

Pull the next raw ASGI message from the peer.

Handles the ``websocket.disconnect`` message by raising
:class:`WebSocketDisconnect`, so callers that loop with
``while True: await ws.receive()`` automatically terminate on
peer close.

#### `WebSocket.receive_text`

```python
def receive_text(self) -> str
```

Await the next text frame, returning its string payload.

#### `WebSocket.receive_bytes`

```python
def receive_bytes(self) -> bytes
```

Await the next binary frame.

#### `WebSocket.receive_json`

```python
def receive_json(self) -> Any
```

Await the next text frame and JSON-decode it.

#### `WebSocket.send_text`

```python
def send_text(self, data: str) -> None
```

#### `WebSocket.send_bytes`

```python
def send_bytes(self, data: bytes) -> None
```

#### `WebSocket.send_json`

```python
def send_json(self, data: Any) -> None
```

Serialize ``data`` and send it as a text frame.

Handles Pydantic models (``model_dump(mode="json")``),
dataclasses, and standard JSON types via a permissive default
handler so rich domain objects can be sent without manual
coercion.

#### `WebSocket.close`

```python
def close(self, code: int = 1000, reason: str = '') -> None
```

Initiate a server-side close.

Idempotent: calling :meth:`close` after the connection has
already terminated is a no-op. The close code / reason are
preserved on the instance for ``@on_disconnect`` hooks.

## Broadcast

### `BroadcastGroup`

```python
class BroadcastGroup()
```

A named set of :class:`WebSocket` connections.

Rooms (chat, presence, realtime dashboards) compose out of
:class:`BroadcastGroup` instances keyed by opaque group names.
Because the registry is a plain dict keyed by string, groups are
auto-created on first reference — no extra ceremony.

Usage::

    @injectable()
    class BroadcastRegistry(BroadcastGroup):
        pass  # BroadcastGroup is already a subclassable provider

    @ws_controller("/chat/{room_id}")
    class ChatGateway:
        def __init__(self, rooms: BroadcastGroup) -> None:
            self._rooms = rooms

        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            rid = ws.path_params["room_id"]
            await self._rooms.subscribe(rid, ws)

The default :class:`BroadcastGroup` is safe for single-process
deployments. Production multi-worker setups should subclass it and
back it with Redis Pub/Sub (or similar) — the same controller code
works unchanged because the fan-out surface is just ``subscribe``
/ ``unsubscribe`` / ``broadcast``.

#### `BroadcastGroup.subscribe`

```python
def subscribe(self, group: str, ws: WebSocket) -> None
```

Add ``ws`` to ``group``. Idempotent.

#### `BroadcastGroup.unsubscribe`

```python
def unsubscribe(self, group: str, ws: WebSocket) -> None
```

Remove ``ws`` from ``group``. Safe to call if not a member.

#### `BroadcastGroup.unsubscribe_all`

```python
def unsubscribe_all(self, ws: WebSocket) -> None
```

Remove ``ws`` from every group it's a member of.

Called automatically by the runtime on disconnect so leaked
subscriptions don't accumulate after clients drop off — but
controllers can also invoke it eagerly during cleanup logic.

#### `BroadcastGroup.broadcast`

```python
def broadcast(self, group: str, message: Any, as_bytes: bool = False, exclude: WebSocket | None = None) -> int
```

Deliver ``message`` to every subscriber of ``group``.

Returns the count of frames actually sent — callers can use it
for basic observability. Dead connections (those whose
:meth:`WebSocket.send_json` raises) are detected and removed
from the group automatically so broadcast storms don't repeat
doomed sends.

``exclude`` lets a broadcaster skip echoing the message back to
the original sender, the common pattern for chat UIs.

#### `BroadcastGroup.groups`

```python
def groups(self) -> list[str]
```

#### `BroadcastGroup.members`

```python
def members(self, group: str) -> list[WebSocket]
```

#### `BroadcastGroup.member_count`

```python
def member_count(self, group: str) -> int
```

## Socket.IO

### `socketio_controller`

```python
def socketio_controller(path: str, ping_interval_ms: int = 25000, ping_timeout_ms: int = 20000, max_payload_bytes: int = 1000000) -> Callable[[type], type]
```

Mount a class as a Socket.IO endpoint at ``path``.

Internally:

1. Discovers every method marked with :func:`on_socketio_event`.
2. Synthesizes ``@on_connect`` / ``@on_message`` / ``@on_disconnect``
   hooks on the class that drive the Socket.IO protocol state
   machine, dispatching inbound packets to the user's
   :func:`@on_socketio_event` methods.
3. Applies :func:`@ws_controller <lauren.ws_controller>` so the
   existing WebSocket runtime (DI, lifecycle, middleware) mounts
   the route exactly like a hand-written WS gateway.

The synthesised hooks live on the class's own ``__dict__``, never
on a parent class, so the framework rule "inheritance does NOT
propagate metadata" is preserved.

### `on_socketio_event`

```python
def on_socketio_event(event: str, summary: str | None = None) -> Callable[[F], F]
```

Mark a method as the handler for a Socket.IO ``event``.

Two reserved names route to lifecycle hooks instead of client
emits:

* ``"connect"`` -- invoked once after the Socket.IO handshake
  completes successfully. Useful for sending a welcome message,
  joining the connection to broadcast groups, or rejecting the
  connection by raising an exception.
* ``"disconnect"`` -- invoked once when the transport closes
  (peer- or server-initiated). Use this to clean up subscriptions
  or persistent state.

Every other event name is dispatched on inbound EVENT packets.
The handler signature determines what's injected:

* ``self`` -- the controller instance (DI-built).
* ``conn: SocketIOConnection`` -- the per-connection facade.
* **Positional payload args** -- the JSON args the client sent,
  in order. ``async def chat(self, conn, payload)`` matches a
  ``socket.emit("chat", {...})`` call from JS.
* **Return value** -- if the client supplied an ack callback, the
  handler's return value is forwarded as the ack args. Returning
  a tuple sends multiple ack args; returning a single value sends
  one; returning ``None`` sends a single ``null`` arg.

The decorator follows the framework convention: it attaches
metadata to the function via ``setattr`` and returns the original
object unchanged. Subclasses that override the handler without
re-applying the decorator do NOT inherit the marker.

### `SocketIOConnection`

```python
class SocketIOConnection(ws: WebSocket, sid: str, namespace: str = _proto.DEFAULT_NAMESPACE)
```

A live Socket.IO client connection.

Instances are constructed by the adapter once the Socket.IO
handshake succeeds and passed to every user event handler. The
object is intentionally small: it wraps the underlying
:class:`~lauren.WebSocket` and adds Socket.IO-aware send helpers
(:meth:`emit`, :meth:`send_ack`, :meth:`disconnect`).

Lifecycle
---------

* The handshake (Engine.IO OPEN, Socket.IO CONNECT) is completed
  before the connection is exposed to user code.
* The connection lives until either the peer disconnects, the
  server calls :meth:`disconnect`, or the underlying WebSocket is
  closed.
* The user's ``on_socketio_event("disconnect")`` handler is
  guaranteed to run once on closure, even if a previous event
  handler raised.

Concurrency
-----------

Outbound emits go through a per-connection ``asyncio.Lock`` to keep
frames atomic on the wire. Without the lock, two concurrent
``conn.emit(...)`` calls could interleave their bytes and confuse
the JS client.

#### `SocketIOConnection.emit`

```python
def emit(self, event: str, args: Any = ()) -> None
```

Emit a Socket.IO event to this client.

``args`` are sent as positional payload elements, mirroring the
JS client's ``socket.emit(event, ...args)`` signature. Any
JSON-able value is acceptable: lauren's permissive default
handler turns Pydantic models, dataclasses, datetimes, etc.
into wire-friendly JSON.

#### `SocketIOConnection.send_ack`

```python
def send_ack(self, ack_id: int, args: Any = ()) -> None
```

Send an ACK packet for an event the client previously emitted.

Most user code doesn't call this directly: returning a value
from an event handler triggers an automatic ACK reply. Exposed
for cases where the ack must be sent asynchronously (e.g.
after kicking off a background task).

#### `SocketIOConnection.disconnect`

```python
def disconnect(self) -> None
```

Initiate a graceful Socket.IO + transport closure.

Sends the Socket.IO ``DISCONNECT`` packet, then the Engine.IO
``CLOSE`` packet, then closes the underlying WebSocket. The
sequence matches what the official Socket.IO server does so
the JS client surfaces the disconnect cleanly.

## Exceptions

### `WebSocketError`

```python
class WebSocketError(message: str = '', close_code: int | None = None, detail: dict[str, Any] | None = None)
```

Base class for WebSocket-layer errors.

### `WebSocketDisconnect`

```python
class WebSocketDisconnect
```

Raised inside a handler when the peer closes the connection.

Handlers that loop over :meth:`WebSocket.receive_text` or the various
typed helpers may catch this to release resources; the runtime always
catches it as the normal end-of-connection signal and runs
``@on_disconnect`` hooks.

### `WebSocketValidationError`

```python
class WebSocketValidationError
```

Inbound frame failed validation against a Pydantic model.

Raised from the typed dispatcher; the runtime catches it, sends a
structured error frame back to the client, and continues the
connection (mismatched frames should not terminate the session).

### `WebSocketRouteNotFoundError`

```python
class WebSocketRouteNotFoundError
```

The handshake path doesn't match any registered ``@ws_controller``.

