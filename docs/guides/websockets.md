# WebSockets

> Lauren makes WebSockets a **first-class peer of HTTP**: same module-and-controller mental model, same DI container, same strict-inheritance rule, same in-process test client. Declare a class with `@ws_controller(path)`, annotate its methods with `@on_connect` / `@on_message("event")` / `@on_disconnect`, and the framework builds an immutable dispatch table at startup.

## When to reach for WebSockets

A few signs your feature wants a WebSocket gateway rather than a plain HTTP route:

* The browser needs **server-pushed updates** (live chat, presence, notifications, stock tickers, multiplayer state).
* Each client maintains **session state** the server cares about (current room, subscriptions, cursor positions).
* The traffic is **bidirectional and chatty** — many small messages each way, where the HTTP request/response framing would be wasteful.

For one-way push (server → browser only), [Server-Sent Events](server-sent-events.md) are usually a simpler fit. Reach for WebSockets when the client also needs to send.

## A minimal echo gateway

```python title="app/gateways.py"
from lauren import WebSocket, ws_controller, on_connect, on_message

@ws_controller("/echo")
class EchoGateway:
    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"event": "hello", "msg": "connected"})

    @on_message("ping")
    async def ping(self, ws: WebSocket) -> None:
        await ws.send_json({"event": "pong"})
```

Register the gateway in a module's `controllers` list — exactly the same as an HTTP controller:

```python title="app/main.py"
from lauren import LaurenFactory, module
from .gateways import EchoGateway

@module(controllers=[EchoGateway])
class AppModule:
    pass

import asyncio
app = LaurenFactory.create(AppModule)
# app is an ASGI callable — serve with uvicorn:  uvicorn app.main:app
```

That's it. A WebSocket client connecting to `ws://localhost:8000/echo` receives the `hello` frame on connect and gets a `pong` for every `{"event": "ping"}` it sends.

## What `@ws_controller` does

`@ws_controller(path)` attaches a `WsControllerMeta` payload to the class and **auto-marks the class as `@injectable(scope=Scope.REQUEST)`**. That means:

* Each WebSocket connection gets its own gateway instance.
* The constructor can take any DI dependency — singletons, request-scoped services, the `BroadcastGroup` provider — exactly like an HTTP controller.
* The `path` may contain `{name}` parameters; they're parsed out and made available via `ws.path_params`.

```python
@ws_controller("/chat/{room_id}")
class ChatGateway:
    def __init__(self, repo: ChatRepository) -> None:
        self.repo = repo

    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        room = ws.path_params["room_id"]
        await ws.accept()
```

## Lifecycle hooks

Three method-level decorators describe a connection's lifecycle:

| Decorator | Runs when | Notes |
|---|---|---|
| `@on_connect` | After the ASGI handshake completes | Call `await ws.accept()` to accept; return without accepting to reject. |
| `@on_message("event")` | A frame with `{"event": "name", ...}` arrives | One method per event name. Multiple decorators stack. |
| `@on_disconnect` | The connection closes (peer or server-initiated) | Best-effort. Exceptions here are logged but don't affect the response. |
| `@on_error` | Any exception other than `WebSocketDisconnect` escapes a handler | Returning normally resumes the dispatch loop. |

```python
from lauren import on_connect, on_disconnect, on_error, on_message

@ws_controller("/feed")
class FeedGateway:
    @on_connect
    async def joined(self, ws: WebSocket) -> None: ...

    @on_message("subscribe")
    async def subscribe(self, ws: WebSocket, body: Json[Subscribe]) -> None: ...

    @on_message("unsubscribe")
    async def unsubscribe(self, ws: WebSocket) -> None: ...

    @on_disconnect
    async def left(self, ws: WebSocket) -> None: ...

    @on_error
    async def caught(self, ws: WebSocket, exc: Exception) -> None:
        await ws.send_json({"error": {"code": "internal", "message": str(exc)}})
```

## Typed messages with Pydantic

Just like HTTP `Json[Model]`, a `@on_message` handler that takes `body: Json[T]` runs through Pydantic validation. The validator is built **once** at startup; per-frame dispatch is pure traversal.

```python
from pydantic import BaseModel
from lauren import Json

class ChatMessage(BaseModel):
    text: str
    mentions: list[str] = []

@ws_controller("/chat/{room_id}")
class ChatGateway:
    @on_message("chat.send")
    async def send(self, ws: WebSocket, body: Json[ChatMessage]) -> None:
        # body is a fully-validated ChatMessage instance
        ...
```

Wire format: every inbound frame is a JSON object with at least an `event` key. The remaining fields are the Pydantic payload. A frame `{"event": "chat.send", "text": "hi", "mentions": []}` matches the handler above.

### Discriminated unions

For heterogeneous payloads under the same event name, use a Pydantic discriminated union — the same primitive HTTP `Json[T]` extractors support:

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field

class ImageEvent(BaseModel):
    kind: Literal["image"]
    url: str

class TextEvent(BaseModel):
    kind: Literal["text"]
    content: str

Event = Annotated[Union[ImageEvent, TextEvent], Field(discriminator="kind")]

@ws_controller("/feed")
class FeedGateway:
    @on_message("post")
    async def post(self, ws: WebSocket, body: Json[Event]) -> None:
        if isinstance(body, ImageEvent):
            ...
        else:
            ...
```

### The wildcard handler and binary frames

Two reserved event names extend the dispatch surface:

* `@on_message("*")` — matches any event without a more specific handler. Useful for catch-all logging or compatibility shims.
* `@on_message("__binary__")` — receives **binary** frames as raw `bytes` (rather than decoded JSON text frames).

```python
@ws_controller("/files")
class FilesGateway:
    @on_message("__binary__")
    async def chunk(self, ws: WebSocket, data: bytes) -> None:
        # data is the raw bytes from a binary frame.
        ...

    @on_message("*")
    async def fallback(self, ws: WebSocket, body: dict) -> None:
        # Handles any event name we didn't explicitly route above.
        await ws.send_json({"error": "unknown_event"})
```

## The `WebSocket` API

`WebSocket` instances are constructed by the runtime and passed to handlers that declare a `ws: WebSocket` parameter. User code never instantiates the class directly.

### Reception

```python
text = await ws.receive_text()        # next text frame
binary = await ws.receive_bytes()     # next binary frame
data = await ws.receive_json()        # next text frame, JSON-decoded
msg = await ws.receive()              # raw ASGI message
```

The dispatcher already calls these for typed `@on_message` handlers — most user code only needs them for advanced patterns (stream uploads, custom protocols).

### Emission

```python
await ws.send_text("hi")
await ws.send_bytes(b"\x00\x01")
await ws.send_json({"event": "chat.recv", "text": "hi"})
```

`send_json` handles Pydantic models, dataclasses, datetimes, UUIDs, and the rest of Lauren's permissive JSON encoder set.

Unlike early releases, `send_json(...)` also respects the app-wide encoder you
pass to `LaurenFactory.create(..., json_encoder=...)`. If the rest of your app
uses `OrjsonEncoder`, `MsgspecEncoder`, or `PydanticEncoder`, WebSocket JSON
frames now use the same encoder family instead of silently falling back to the
stdlib path.

### Termination

```python
await ws.close(code=1000, reason="bye")
```

Idempotent — calling `close` after the connection already terminated is a no-op. The close code and reason are preserved on the instance for `@on_disconnect` hooks.

### Properties

| Property | Purpose |
|---|---|
| `ws.path` | Concrete request path (e.g. `/chat/42`). |
| `ws.path_template` | Templated path (e.g. `/chat/{room_id}`). |
| `ws.path_params` | Parsed path parameters as a dict. |
| `ws.headers` | Case-insensitive headers map. |
| `ws.query_string` | Raw query bytes. |
| `ws.state` | Per-connection state — same shape as `Request.state`. |
| `ws.app_state` | Sealed app-level state. |
| `ws.client_subprotocols` | Tuple of subprotocols the client offered. |
| `ws.subprotocol` | The one the server selected (set during `accept()`). |
| `ws.connected` | `True` while the connection is open. |
| `ws.connection_state` | `"connecting"` / `"open"` / `"closed"`. |
| `ws.close_code` / `ws.close_reason` | Filled in on close. |

## Authorisation

Reject unauthenticated connections by `close()`-ing without `accept()`:

```python
@ws_controller("/private")
class PrivateGateway:
    def __init__(self, jwt: JwtService) -> None:
        self.jwt = jwt

    @on_connect
    async def auth(self, ws: WebSocket) -> None:
        token = ws.headers.get("authorization", "")
        if not token.startswith("Bearer "):
            await ws.close(code=4401, reason="unauthorised")
            return
        try:
            claims = self.jwt.decode(token[7:])
        except InvalidToken:
            await ws.close(code=4401, reason="invalid token")
            return
        ws.state.set("user_id", claims["sub"])
        await ws.accept()
```

WebSocket close codes in the **4000–4999** range are reserved for application-defined protocols — `4401` for "unauthorised" is a common convention.

## Broadcasting and rooms

`BroadcastGroup` is a DI-injectable provider that maintains named sets of subscribers. It's the substrate for chat rooms, presence, real-time dashboards, and any other "fan-out a message to N connections" pattern.

```python
from lauren import BroadcastGroup

@ws_controller("/chat/{room_id}")
class ChatGateway:
    def __init__(self, rooms: BroadcastGroup) -> None:
        self._rooms = rooms

    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        await ws.accept()
        room_id = ws.path_params["room_id"]
        await self._rooms.subscribe(room_id, ws)
        await self._rooms.broadcast(
            room_id,
            {"event": "presence", "type": "joined"},
            exclude=ws,
        )

    @on_message("chat.send")
    async def send(self, ws: WebSocket, body: Json[ChatMessage]) -> None:
        room_id = ws.path_params["room_id"]
        await self._rooms.broadcast(
            room_id,
            {"event": "chat.recv", "text": body.text},
            exclude=ws,
        )

    @on_disconnect
    async def left(self, ws: WebSocket) -> None:
        # The framework auto-calls ``unsubscribe_all`` on disconnect, but
        # explicit announcements (e.g. a "user left" broadcast) belong here.
        room_id = ws.path_params.get("room_id")
        if room_id:
            await self._rooms.broadcast(
                room_id,
                {"event": "presence", "type": "left"},
            )

@module(
    controllers=[ChatGateway],
    providers=[BroadcastGroup],
)
class AppModule:
    pass
```

`BroadcastGroup` API at a glance:

| Method | Purpose |
|---|---|
| `await group.subscribe(name, ws)` | Add `ws` to `name`. Idempotent. |
| `await group.unsubscribe(name, ws)` | Remove. Safe even if not a member. |
| `await group.unsubscribe_all(ws)` | Remove from every group. Auto-called on disconnect. |
| `await group.broadcast(name, msg, *, as_bytes=False, exclude=None)` | Send to every subscriber. Returns the count of frames actually sent. Detects and prunes dead sockets automatically. |
| `group.groups()` | List active group names. |
| `group.member_count(name)` | Count subscribers in a group. |

### Multi-worker production

The bundled `BroadcastGroup` is **single-process**. Two workers behind a load balancer each have their own independent membership map; clients in different worker processes don't see each other's broadcasts.

For multi-worker production, **subclass `BroadcastGroup` and back the membership store with Redis Pub/Sub** (or NATS, MQTT, …). The same controller code works unchanged because the public API — `subscribe` / `unsubscribe` / `broadcast` / `unsubscribe_all` — stays the same:

```python
class RedisBroadcastGroup(BroadcastGroup):
    def __init__(self, redis: Redis) -> None:
        super().__init__()
        self._redis = redis
        # ... pub/sub plumbing ...

    async def broadcast(self, group, message, **kw):
        # Local fan-out + publish to other workers via Redis.
        local = await super().broadcast(group, message, **kw)
        await self._redis.publish(f"ws:{group}", json.dumps(message))
        return local
```

## Connection-scoped state

`ws.state` is a `State` instance — same surface as `request.state` on the HTTP side. Use it to stash per-connection data that handlers should re-read on every frame:

```python
@ws_controller("/feed")
class FeedGateway:
    @on_connect
    async def joined(self, ws: WebSocket) -> None:
        await ws.accept()
        ws.state.set("subscribed_topics", set())

    @on_message("subscribe")
    async def subscribe(self, ws: WebSocket, body: Json[SubscribeMsg]) -> None:
        topics = ws.state.get("subscribed_topics")
        topics.add(body.topic)

    @on_message("unsubscribe")
    async def unsubscribe(self, ws: WebSocket, body: Json[UnsubscribeMsg]) -> None:
        topics = ws.state.get("subscribed_topics")
        topics.discard(body.topic)
```

For *application*-level data that's read-only after startup (config, registries), inject providers via the gateway's constructor instead.

## Real-world: AI agent event push

The `lauren-ai-chatbot` backend demonstrates a production-grade pattern where a WebSocket gateway acts as a live side-channel for agent observability events — tool calls, token usage, balance changes — while the primary response flows over SSE.

### The problem

Browsers cannot set custom HTTP headers during a WebSocket upgrade (the `Upgrade` request is browser-controlled). Passing credentials in the URL query string is therefore the standard workaround for authenticated WebSocket connections.

The backend solves this with two endpoints:

1. `POST /api/banking/ws-token` — issues a short-lived, HMAC-signed token (120-second TTL). The browser calls this over the already-authenticated HTTP path before opening the socket.
2. `WS /ws/banking?token=<token>` — the gateway reads the token from the query string, verifies it, and accepts.

### Token issuance (HTTP → WebSocket handoff)

```python title="app/ws/ws_token_controller.py"
@use_guards(SignatureGuard)
@controller("/api/banking")
class WsTokenController:
    def __init__(self, token_service: WsTokenService) -> None:
        self._svc = token_service

    @post("/ws-token")
    async def issue_token(self, body: Json[WsTokenRequest]) -> dict:
        token = self._svc.create_token(body.user_id)
        return {"token": token, "expires_in": 120}
```

### The gateway

`BankingWsGateway` uses Lauren's `Query[str]` extractor to pluck the token from the query string — no `@on_connect` body parsing needed:

```python title="app/ws/ws_gateway.py"
from lauren import Query
from lauren.websockets import WebSocket, WebSocketDisconnect, on_connect, on_disconnect, ws_controller

from app.ws.event_forwarder import EventForwarder
from app.ws.token_service import WsTokenService


@ws_controller("/ws/banking")
class BankingWsGateway:
    def __init__(self, forwarder: EventForwarder, token_service: WsTokenService) -> None:
        self._forwarder = forwarder
        self._token_service = token_service
        self._user_id: str | None = None

    @on_connect
    async def connect(self, ws: WebSocket, token: Query[str]) -> None:
        user_id = self._token_service.verify_token(token)
        if not user_id:
            await ws.close(code=4401, reason="invalid or expired token")
            raise WebSocketDisconnect("unauthorized", close_code=4401)
        self._user_id = user_id
        await self._forwarder.register(user_id, ws)

    @on_disconnect
    async def disconnect(self, ws: WebSocket) -> None:
        if self._user_id:
            await self._forwarder.unregister(self._user_id, ws)
```

Two points worth noting:

* The gateway is `Scope.REQUEST` (the `@ws_controller` default) — each connection has its own instance, so `self._user_id` is connection-private.
* Authentication is entirely in `@on_connect`. If verification fails the connection is closed before `register()` is ever called.

### EventForwarder — the signal-to-socket bridge

`EventForwarder` is a **singleton** that holds all live WebSocket registrations and subscribes to `SignalBus` events in its constructor:

```python title="app/ws/event_forwarder.py"
from lauren import Scope, injectable
from lauren.websockets import WebSocket
from lauren_ai import AgentRunComplete, ModelCallComplete, ToolCallComplete, ToolCallStarted

from app.ai.signals import signal_bus
from app.banking.bank_db import BankDatabase, Transaction
from app.ws.context import current_user_id


@injectable(scope=Scope.SINGLETON)
class EventForwarder:
    def __init__(self, db: BankDatabase) -> None:
        self._connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

        # Register agent lifecycle signal handlers once at construction time.
        signal_bus.on(ModelCallComplete)(self._on_model_complete)
        signal_bus.on(ToolCallStarted)(self._on_tool_started)
        signal_bus.on(ToolCallComplete)(self._on_tool_complete)
        signal_bus.on(AgentRunComplete)(self._on_run_complete)

        # Balance changes broadcast to ALL connected users.
        db.add_transfer_listener(self._on_transfer)

    async def register(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.setdefault(user_id, []).append(ws)

    async def unregister(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            bucket = self._connections.get(user_id)
            if bucket and ws in bucket:
                bucket.remove(ws)

    async def send_to_user(self, user_id: str, payload: dict) -> None:
        """Unicast to every socket registered for this user; prune dead ones."""
        async with self._lock:
            targets = list(self._connections.get(user_id, []))
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                bucket = self._connections.get(user_id, [])
                for ws in dead:
                    if ws in bucket:
                        bucket.remove(ws)

    async def broadcast(self, payload: dict) -> None:
        """Multicast to every connected socket across all users."""
        async with self._lock:
            all_ws = [ws for bucket in self._connections.values() for ws in bucket]
        for ws in all_ws:
            try:
                await ws.send_json(payload)
            except Exception:
                pass  # dead socket — ignored; cleanup happens on the user's next send

    async def _on_model_complete(self, event: ModelCallComplete) -> None:
        user_id = current_user_id.get()          # ContextVar set by the HTTP handler
        if not user_id:
            return
        usage = event.usage
        await self.send_to_user(user_id, {
            "type": "token_usage",
            "model": event.model,
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "cost_usd": event.cost_usd,
        })

    async def _on_transfer(self, tx: Transaction, from_balance: float, to_balance: float) -> None:
        await self.broadcast({
            "type": "balance_changed",
            "from_user": tx.from_user,
            "to_user": tx.to_user,
            "amount": tx.amount,
            "balances": {tx.from_user: from_balance, tx.to_user: to_balance},
        })
```

Key design decisions:

* **Dead-socket pruning inside `send_to_user`.** Rather than a background sweeper, dead sockets are detected and removed lazily on the next send attempt. This keeps the cleanup O(1) per message.
* **`broadcast` swallows send errors.** Balance-changed notifications are best-effort; a failing socket is cleaned up the next time `send_to_user` runs for that user.
* **Constructor wiring.** Registering signal handlers in `__init__` makes the wiring explicit and DI-traceable — no `@post_construct` or startup hook needed.

### Routing events to the right user with `ContextVar`

The HTTP chat handler sets a `ContextVar` **before** calling `AgentRunner.run()`. `SignalBus.emit` uses `asyncio.gather` internally, which copies the current `contextvars.Context` into every spawned coroutine — so signal handlers fired inside the agent loop automatically inherit the right `user_id`:

```python title="app/ws/context.py"
from contextvars import ContextVar

# Holds the authenticated user_id for the duration of an agent run.
# asyncio.gather() copies ContextVar state into spawned tasks, so
# this value is visible inside SignalBus handlers called via emit().
current_user_id: ContextVar[str | None] = ContextVar("ws_current_user_id", default=None)
```

```python title="app/ai/chat_banking_controller.py (excerpt)"
async def generate():
    current_user_id.set(account.user_id)   # ← pins the user for signal handlers
    response = await self._runner.run(
        self._crm_agent,
        full_prompt,
        execution_context=exec_ctx,
    )
    ...
```

!!! note "Why not reset() the ContextVar?"
    `EventStream`'s keep-alive path wraps each `__anext__()` in a fresh `asyncio.Task`. Every task gets a **copy** of the context at the point it's spawned, so `reset()` with a `Token` from a different task raises `ValueError("created in a different Context")`. Since each task's context copy is discarded automatically on exit, no manual reset is needed.

### Module wiring

```python title="app/ws/ws_module.py"
@module(
    imports=[CryptoModule, BankingModule],
    providers=[EventForwarder, WsTokenService],
    controllers=[BankingWsGateway, WsTokenController],
    exports=[EventForwarder],
)
class WsModule:
    pass
```

`EventForwarder` is exported so `AIModule` (which owns the chat controller that sets the `ContextVar`) can import it if needed. `BankingModule` is imported to satisfy `EventForwarder`'s `BankDatabase` dependency.

See the [Signals guide](signals.md#real-world-routing-agent-signals-to-websocket-clients) for the corresponding `SignalBus` setup.

---

## Strict inheritance — opt-in only

Like every other class-level decorator in Lauren, `@ws_controller` does **not** propagate to subclasses. A subclass that wants to be a gateway has to redecorate.

```python
@ws_controller("/v1/chat")
class ChatV1: ...

class ChatV2(ChatV1):
    pass    # NOT a gateway. Registering it raises MetadataInheritanceError.

@ws_controller("/v2/chat")
class ChatV2(ChatV1):
    pass    # OK — explicit opt-in.
```

Method-level markers (`@on_connect`, `@on_message`, `@on_disconnect`, `@on_error`) attach to the function itself. A subclass that **overrides** a method without re-applying the decorator loses the marker — symmetric with how `@get` and `@post` work on HTTP controllers.

See [Class Inheritance Rules](../core-concepts/inheritance.md) for the full justification.

## Error handling

Lauren ships four WebSocket-specific error classes, all subclassing `WebSocketError`:

| Class | Meaning |
|---|---|
| `WebSocketError` | Base class. |
| `WebSocketDisconnect(close_code=...)` | The peer closed. Raised by `ws.receive_*` calls; treat as the loop-exit signal. |
| `WebSocketValidationError` | A frame failed Pydantic validation. The runtime sends a structured error frame back to the client and continues. |
| `WebSocketRouteNotFoundError` | No gateway matched the path during the handshake. |

`@on_error` is the catch-all hook for **anything else** raised inside a `@on_message` handler. Returning normally from `@on_error` resumes the dispatch loop — useful for sending error frames without dropping the connection:

```python
@on_error
async def caught(self, ws: WebSocket, exc: Exception) -> None:
    await ws.send_json({
        "error": {
            "code": "internal_error",
            "message": str(exc),
        }
    })
```

## Testing

Drive a real app through `WsTestClient` — no real socket, no timing flakiness:

```python
import asyncio
from lauren import LaurenFactory, module
from lauren.testing import WsTestClient
from app.gateways import EchoGateway

@module(controllers=[EchoGateway])
class AppModule:
    pass

async def test_ping_pong():
    app = LaurenFactory.create(AppModule)
    client = WsTestClient(app)
    async with client.connect("/echo") as ws:
        # ``hello`` event from @on_connect:
        hello = await ws.receive_json()
        assert hello["event"] == "hello"
        # Round-trip:
        await ws.send_json({"event": "ping"})
        reply = await ws.receive_json()
        assert reply["event"] == "pong"
```

Connection options include `headers={...}`, `subprotocols=[...]`, and `query_string="..."`, mirroring what a real client would send. The session context-manager guarantees the server task is awaited at exit, so any unhandled server-side exception propagates into the test harness instead of getting silently swallowed.

## Rejection patterns

There are three idiomatic ways to reject a connection inside `@on_connect`:

| Pattern | Code | Notes |
|---------|------|-------|
| `close()` then `return` | `await ws.close(code=4401); return` | Preferred. Sends close frame, exits hook cleanly. |
| `raise WebSocketDisconnect` | `raise WebSocketDisconnect("reason", close_code=4401)` | Equivalent — the runtime sends the close frame for you. |
| `close()` then `raise` | `await ws.close(...); raise WebSocketDisconnect(...)` | Also safe. The runtime detects the connection is already closed and skips the second close frame. |

All three patterns produce the same client-visible close code. Mixing `close()` with `raise WebSocketDisconnect` in the same handler is safe because Lauren tracks connection state internally and never sends a duplicate close frame to the ASGI transport.

## Best practices

* **Accept explicitly when authorising.** Calling `await ws.accept()` is the contract that signals "the handshake succeeded". Calling `close()` *before* `accept()` rejects the connection with the `4xxx` code you choose. Skip both, and the framework will accept by default — convenient for trivial gateways but error-prone for anything authenticated.
* **Use `BroadcastGroup` for fan-out, not a list of WebSockets.** Dead-socket detection, race-safe membership, and multi-worker swap-in all come for free.
* **Keep `@on_message` handlers small.** They're the hot path. Expensive work (DB writes, blocking I/O) should be wrapped in an async task that doesn't block the dispatch loop.
* **Disconnect cleanup is best-effort.** Don't put logic that *must* succeed inside `@on_disconnect` — the connection is already gone and the hook may itself fail. Lifecycle-critical work belongs in a `@pre_destruct` on a singleton service that the gateway uses.
* **Mind the close codes.** `1000` is normal close; `1003` is "unexpected payload type"; `1011` is "internal error"; `4000–4999` is the application range. The browser receives them as `event.code` on `EventSource`/`WebSocket` close events, so consistency matters for client-side reconnection logic.

## See also

* [Server-Sent Events](server-sent-events.md) — for one-way push when you don't need bidirectional traffic.
* [Signals & Lifecycle Events](signals.md) — `SignalBus` and the `ContextVar` routing pattern shown in the real-world example above.
* [Class Inheritance Rules](../core-concepts/inheritance.md) — why subclassing a `@ws_controller` doesn't auto-mount.
* [Custom Guards](custom-guards.md) — for HTTP-style authorisation; on WebSockets the equivalent is an `@on_connect` check that closes with a 4xxx code.
* [Reference → Error Catalog](../reference/errors.md) — all 28 framework error classes.
