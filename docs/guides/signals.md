# Signals & Lifecycle Events

> Lauren's `SignalBus` is an in-process pub/sub bus for typed **lifecycle events**. It
> lets you plug in metrics, tracing, audit logging, or any other cross-cutting concern
> without touching framework internals. Listeners are sync or async, error-isolated,
> and dispatched in MRO order so a base-class listener is a firehose of every event.

---

## Two surfaces, one module

`lauren.signals` exposes two related but independent surfaces:

| Surface | What it is | How you use it |
|---|---|---|
| **`SignalBus`** + typed events | In-process pub/sub | Subscribe to `StartupComplete`, `RequestComplete`, etc. |
| **`install_signal_handlers`** | OS signal → graceful shutdown | Wire `SIGTERM` / `SIGINT` to `app.shutdown()` |

This guide focuses on the event bus. For graceful shutdown see
[Graceful Shutdown](../core-concepts/lifecycle.md) and the cheat sheet.

---

## Accessing the bus

Every `LaurenApp` owns a `SignalBus` at `app.signals`:

```python
from lauren import LaurenFactory, module

@module(...)
class AppModule: ...

app = LaurenFactory.create(AppModule)
bus = app.signals           # app-owned SignalBus
```

For process-global registration (before the app is created), use the module-level
`on` decorator which registers against a **default bus** that gets seeded into every
new `LaurenApp`:

```python
from lauren.signals import on, RequestComplete

@on(RequestComplete)
def log_slow(event: RequestComplete) -> None:
    if event.duration_s > 1.0:
        print(f"SLOW: {event.request.path} took {event.duration_s:.2f}s")
```

---

## Built-in events

Five framework events are emitted automatically:

| Event | When fired | Key fields |
|---|---|---|
| `StartupBegin` | First `lifespan.startup` message; `@post_construct` not yet run | `app` |
| `StartupComplete` | Every `@post_construct` hook finished; app ready | `app`, `duration_s` |
| `RequestReceived` | ASGI scope decoded; router not yet run | `request` |
| `RequestComplete` | Response fully sent (success and error paths) | `request`, `response`, `status`, `duration_s`, `error` |
| `ShutdownBegin` | `lifespan.shutdown` or OS signal received | `app` |

Three additional events relate to background tasks:

| Event | When fired | Key fields |
|---|---|---|
| `BackgroundTaskStarted` | Just before a background task executes | `task_id`, `func` |
| `BackgroundTaskComplete` | After successful completion | `task_id`, `func`, `duration_s` |
| `BackgroundTaskFailed` | After an exception is raised | `task_id`, `func`, `error` |

All events inherit from `LifecycleEvent` which carries a `timestamp: float` (monotonic)
so listeners can compute deltas without recording wall-clock time themselves.

---

## Subscribing to events

### Decorator form (most common)

```python
from lauren.signals import RequestComplete

@app.signals.on(RequestComplete)
def on_complete(event: RequestComplete) -> None:
    metrics.record("request.duration", event.duration_s, tags={"status": event.status})
```

### Call form

```python
def my_listener(event: RequestComplete) -> None: ...

app.signals.on(RequestComplete)(my_listener)
```

Both forms are idempotent — registering the same `(event_type, fn)` pair twice is a
no-op. Useful during development with module reloads.

### Async listeners

Async listeners are awaited sequentially in registration order:

```python
@app.signals.on(RequestComplete)
async def async_listener(event: RequestComplete) -> None:
    await audit_db.record(event.request.path, event.status)
```

Sync and async listeners can be freely mixed on the same event type.

### Module-level registration with `@on`

For libraries or startup code that runs before the app is constructed:

```python
from lauren.signals import on, StartupComplete

@on(StartupComplete)
async def warmup(event: StartupComplete) -> None:
    await event.app.container.resolve(CacheWarmer).warm()
```

---

## MRO dispatch — base-class listeners as firehoses

A listener registered on a **base class** receives every subclass event. This makes
`LifecycleEvent` a complete firehose:

```python
from lauren.signals import LifecycleEvent

@app.signals.on(LifecycleEvent)
def trace_all(event: LifecycleEvent) -> None:
    print(type(event).__name__, event.timestamp)
```

Conversely, a listener on a specific subclass (`RequestComplete`) does **not** fire for
sibling events (`StartupComplete`). The MRO walk is most-specific-first:

```python
# Listener on RequestComplete fires ONLY for RequestComplete events.
# Listener on LifecycleEvent fires for ALL events (RequestComplete, StartupBegin, …).
```

---

## Error isolation

Errors inside listeners are **logged but swallowed**. Observability code must not be
able to break the request path. The next listener in registration order always runs,
even if a previous one raised:

```python
@app.signals.on(RequestComplete)
def bad_listener(event: RequestComplete) -> None:
    raise RuntimeError("kaboom")           # logged, swallowed

@app.signals.on(RequestComplete)
def good_listener(event: RequestComplete) -> None:
    metrics.increment("requests")          # still runs
```

---

## Unsubscribing

```python
def my_handler(event: RequestComplete) -> None: ...

app.signals.on(RequestComplete)(my_handler)
# Later:
removed = app.signals.off(RequestComplete, my_handler)   # True if found and removed
```

`off()` returns `True` when removed, `False` when the listener wasn't registered.
Never raises — safe to call unconditionally in teardown code.

### Clear all listeners

```python
app.signals.clear()     # removes every listener from every event type
```

Primarily useful between tests when the same `SignalBus` is reused.

---

## Checking listener count

```python
count = app.signals.listener_count(RequestComplete)
```

Counts all listeners that would fire, including those registered on base classes (same
MRO walk as `emit`). Useful in tests and diagnostics.

---

## `emit_sync` — calling from synchronous contexts

The startup / shutdown phases are not fully async; `emit_sync` is the sync-context
variant. Async listeners are scheduled on the running loop when one is available, or
skipped (with their coroutines closed to prevent "never awaited" warnings) if not:

```python
bus.emit_sync(StartupBegin(app=app))
```

User code rarely calls `emit_sync` directly; the framework uses it internally.

---

## Practical patterns

### Metrics sink on every request

```python
from lauren.signals import RequestComplete

@injectable(scope=Scope.SINGLETON)
class MetricsSink:
    def __init__(self, app: LaurenApp, client: MetricsClient) -> None:
        self._client = client
        app.signals.on(RequestComplete)(self._record)

    def _record(self, event: RequestComplete) -> None:
        self._client.histogram(
            "request.duration",
            event.duration_s,
            tags={"status": str(event.status), "path": event.request.path},
        )
```

Register `MetricsSink` as a singleton provider. The constructor wires itself into the
bus — no explicit subscription call at startup needed.

### Readiness flip on `StartupComplete`

```python
from lauren.signals import StartupComplete

ready = {"ok": False}

@app.signals.on(StartupComplete)
def flip_ready(event: StartupComplete) -> None:
    ready["ok"] = True

@get("/health/ready")
async def readiness(self) -> dict:
    return {"ready": ready["ok"]}
```

### Background task failure alerting

```python
from lauren.signals import BackgroundTaskFailed

@app.signals.on(BackgroundTaskFailed)
async def alert(event: BackgroundTaskFailed) -> None:
    await pagerduty.trigger(
        f"Background task {event.task_id!r} failed: {event.error}"
    )
```

### Slow request alerting via error field

```python
from lauren.signals import RequestComplete

@app.signals.on(RequestComplete)
def detect_errors(event: RequestComplete) -> None:
    if event.error is not None:
        sentry.capture_exception(event.error)
    if event.status >= 500:
        oncall.page(f"5xx on {event.request.path}")
```

---

## Real-world: routing agent signals to WebSocket clients

The `lauren-ai-chatbot` backend wires `SignalBus` to a WebSocket gateway so that every LLM token usage, tool call, and fund transfer is pushed to the browser in real time — without the HTTP SSE channel having to know about WebSockets at all.

### Shared bus singleton

Both `main.py` and `ai_module.py` need the same bus instance. Rather than importing from either of those (which would create a circular dependency), a dedicated module holds the singleton:

```python title="app/ai/signals.py"
from lauren_ai import SignalBus

# One instance for the whole application.
# Centralising it here avoids circular imports between main.py and ai_module.py.
signal_bus: SignalBus = SignalBus()
```

### Wiring signals to WebSocket delivery

`EventForwarder` is a `Scope.SINGLETON` injectable that subscribes to every agent-lifecycle event in its constructor. Because it's a singleton, the subscription happens once at startup and the handler list is stable for the application's lifetime:

```python title="app/ws/event_forwarder.py"
from lauren import Scope, injectable
from lauren_ai import AgentRunComplete, ModelCallComplete, ToolCallComplete, ToolCallStarted

from app.ai.signals import signal_bus
from app.ws.context import current_user_id


@injectable(scope=Scope.SINGLETON)
class EventForwarder:
    def __init__(self, db: BankDatabase) -> None:
        self._connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

        # All four subscriptions happen here — pure DI wiring, no @post_construct.
        signal_bus.on(ModelCallComplete)(self._on_model_complete)
        signal_bus.on(ToolCallStarted)(self._on_tool_started)
        signal_bus.on(ToolCallComplete)(self._on_tool_complete)
        signal_bus.on(AgentRunComplete)(self._on_run_complete)

    async def _on_model_complete(self, event: ModelCallComplete) -> None:
        user_id = current_user_id.get()   # which user's browser should receive this?
        if not user_id:
            return
        await self.send_to_user(user_id, {
            "type": "token_usage",
            "model": event.model,
            "input_tokens": event.usage.input_tokens if event.usage else 0,
            "output_tokens": event.usage.output_tokens if event.usage else 0,
            "cost_usd": event.cost_usd,
            "duration_ms": round(event.duration_ms),
        })

    async def _on_tool_started(self, event: ToolCallStarted) -> None:
        user_id = current_user_id.get()
        if not user_id:
            return
        await self.send_to_user(user_id, {
            "type": "tool_started",
            "tool_name": event.tool_name,
            "tool_use_id": event.tool_use_id,
        })

    async def _on_run_complete(self, event: AgentRunComplete) -> None:
        user_id = current_user_id.get()
        if not user_id:
            return
        usage = event.total_usage
        await self.send_to_user(user_id, {
            "type": "run_complete",
            "turns": event.turns,
            "total_cost_usd": event.total_cost_usd,
            "total_tokens": (usage.input_tokens + usage.output_tokens) if usage else 0,
        })
```

### Routing to the right user with `ContextVar`

The handlers above call `current_user_id.get()` — a `contextvars.ContextVar` — to find out which user is currently running an agent. `SignalBus.emit` uses `asyncio.gather` internally, which **copies the calling task's `Context` into every spawned coroutine**. This means any `ContextVar` set before `emit` is visible inside the listeners without any extra wiring.

The HTTP chat controller sets the variable before calling `AgentRunner.run()`:

```python title="app/ai/chat_banking_controller.py (excerpt)"
async def generate():
    # Pin the authenticated user so every signal handler fired during
    # the agent loop can call current_user_id.get() to route its payload.
    current_user_id.set(account.user_id)

    response = await self._runner.run(
        self._crm_agent,
        full_prompt,
        execution_context=exec_ctx,
    )
    ...
```

And the variable is defined in its own module to prevent import cycles:

```python title="app/ws/context.py"
from contextvars import ContextVar

# asyncio.gather() copies ContextVar state into spawned tasks, so this
# value is visible inside SignalBus handlers called via emit().
current_user_id: ContextVar[str | None] = ContextVar("ws_current_user_id", default=None)
```

### Why this works without explicit passing

The flow end-to-end:

```
HTTP POST /api/banking/chat
  └─ BankingChatController.stream()
       └─ current_user_id.set("alice")      ← pins context
       └─ AgentRunner.run(crm_agent, ...)
            └─ tool calls → LLM → tool calls
            └─ signal_bus.emit(ModelCallComplete(...))
                 └─ asyncio.gather copies Context
                 └─ EventForwarder._on_model_complete()
                      └─ current_user_id.get() → "alice"  ← reads from copied context
                      └─ ws.send_json({"type": "token_usage", ...})
                           └─ browser receives event on WS /ws/banking?token=...
```

No signal argument carries the user id. No global lock is needed. The async task scheduler propagates it automatically.

### Database callbacks as pseudo-signals

The `BankDatabase` transfer listener follows the same delivery pattern, but uses a plain Python callback list instead of a `SignalBus`:

```python title="app/ws/event_forwarder.py (excerpt)"
# Registered in EventForwarder.__init__:
db.add_transfer_listener(self._on_transfer)

async def _on_transfer(self, tx: Transaction, from_balance: float, to_balance: float) -> None:
    """Broadcast balance update to ALL connected users after any transfer."""
    await self.broadcast({
        "type": "balance_changed",
        "from_user": tx.from_user,
        "to_user": tx.to_user,
        "amount": tx.amount,
        "balances": {tx.from_user: from_balance, tx.to_user: to_balance},
    })
```

Balance changes are broadcast (not unicast) because every connected user's UI should refresh — the person who received the transfer should see their new balance immediately, not just the sender.

See the [WebSockets guide](websockets.md#real-world-ai-agent-event-push) for the corresponding gateway, token service, and module wiring.

---

## Snapshot semantics

During `emit`, a snapshot of the current listener list is taken before iteration
begins. A listener that registers a new sibling during emit does **not** see the
new sibling fire in the same `emit` call — it fires on the next one:

```python
@bus.on(StartupBegin)
def registrar(event: LifecycleEvent) -> None:
    bus.on(StartupBegin)(late_listener)  # registers, but late_listener won't fire yet
```

This prevents accidentally creating unbounded dispatch loops.

---

## Testing with signals

```python
from lauren.signals import RequestComplete, BackgroundTaskFailed

# Capture events in a list:
events: list[RequestComplete] = []
app.signals.on(RequestComplete)(events.append)

client = TestClient(app)
client.get("/users/1")

assert len(events) == 1
assert events[0].status == 200

# Don't forget to clear between tests:
app.signals.clear()
```

---

## OS signal integration (graceful shutdown)

```python
from lauren.signals import install_signal_handlers, wait_for_shutdown

# Wire SIGTERM + SIGINT to app.shutdown():
event = install_signal_handlers(app, drain_timeout=30)

# In your main loop:
await wait_for_shutdown(event)
```

`install_signal_handlers` returns an `asyncio.Event` that fires when a signal is
received. `wait_for_shutdown` is a thin wrapper for `await event.wait()`.

The handlers are idempotent — multiple signals don't trigger multiple shutdowns.

See also the [Lifecycle Hooks](../core-concepts/lifecycle.md) guide for the four-phase
shutdown sequence.

---

## See also

* [WebSockets](websockets.md) — the gateway, `EventForwarder`, and token service from the real-world example above.
* [Background Tasks](background-tasks.md) — `BackgroundTaskStarted/Complete/Failed` signals.
* [Lifecycle Hooks](../core-concepts/lifecycle.md) — `@post_construct` / `@pre_destruct` and shutdown phases.
* [Reference → Cheat Sheet](../reference/cheat-sheet.md) — one-line signal patterns.
