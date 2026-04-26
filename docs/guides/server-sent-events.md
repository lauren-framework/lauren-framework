# Server-Sent Events

> **Server-Sent Events** (SSE) are a one-way streaming protocol layered on plain HTTP. They give browsers a `text/event-stream` feed they can consume with `new EventSource(url)`, which automatically reconnects on transport errors and forwards a `Last-Event-ID` header for resumability. Lauren ships first-class SSE primitives (`EventStream`, `ServerSentEvent`, `last_event_id`) that slot directly into the existing handler-return pipeline.

## When to choose SSE over WebSockets

SSE and WebSockets are often presented as alternatives, but they target different shapes of problem:

| Choose **SSE** when… | Choose **WebSockets** when… |
|---|---|
| Traffic is **server → browser only** | Traffic is **bidirectional** |
| You want **automatic reconnect + resumability** in the browser for free | You need binary frames or heavy custom protocols |
| You're behind plain HTTP/1.1 infrastructure (load balancers, CDNs) | You control the deployment all the way to the edge |
| Each event is small and JSON-shaped | Per-frame size and structure vary widely |
| You need **a few hundred** concurrent streams per server | You need many thousands of long-lived sockets |

A non-exhaustive list of perfect fits for SSE: **live notifications**, **dashboard ticks**, **progress reporting** for long-running jobs, **chat-message read** receipts, **AI text-streaming responses** ("typing…" tokens), **log tail**, **build/CI feed**.

If you also need the browser to *send* messages back, reach for [WebSockets](websockets.md) instead.

## A minimal SSE endpoint

```python title="app/feeds.py"
import asyncio
from lauren import EventStream, ServerSentEvent, controller, get

@controller("/feed")
class FeedController:
    @get("/")
    async def stream(self) -> EventStream:
        async def producer():
            for i in range(10):
                yield ServerSentEvent(
                    event="tick",
                    data={"seq": i, "value": i * 0.5},
                )
                await asyncio.sleep(1.0)
        return EventStream(producer())
```

That's a complete, working endpoint. The browser side is just as small:

```javascript
const es = new EventSource("/feed/");
es.addEventListener("tick", (event) => {
    const payload = JSON.parse(event.data);
    console.log(payload.seq, payload.value);
});
```

What `EventStream` does for you:

* Sets the `Content-Type: text/event-stream; charset=utf-8` header.
* Sets `Cache-Control: no-cache`, `Connection: keep-alive`, and `X-Accel-Buffering: no` (the nginx opt-out).
* Frames each yielded item per the [HTML living standard](https://html.spec.whatwg.org/multipage/server-sent-events.html).
* Optionally inserts keep-alive heartbeats so idle connections survive proxy timeouts.

## What you can yield from the producer

The async iterable wrapped by `EventStream` accepts five shapes — **mix them freely**:

| Yielded value | Wire result |
|---|---|
| `ServerSentEvent(...)` | Emitted as-is with whatever `event`/`id`/`retry`/`data`/`comment` you set. |
| `str` | Wrapped in `ServerSentEvent(data=...)`. |
| `bytes` | Decoded as UTF-8 and wrapped. |
| `dict` (mapping) | Promoted via `ServerSentEvent.from_dict(...)` — keys `data`/`event`/`id`/`retry`/`comment` recognised. |
| any other value | JSON-encoded (Pydantic models, dataclasses, datetimes, UUIDs, …) and wrapped as `data`. |

```python
from datetime import datetime
from pydantic import BaseModel

class Tick(BaseModel):
    seq: int
    at: datetime

async def producer():
    yield "hello"                                      # plain string
    yield {"event": "init", "data": {"ready": True}}   # dict
    yield Tick(seq=1, at=datetime.now())               # Pydantic — auto-JSON
    yield ServerSentEvent(                             # explicit
        event="batch",
        id="evt-42",
        data={"items": [1, 2, 3]},
    )
```

## The `ServerSentEvent` envelope

Per the SSE spec, only `data` is meaningful to clients on its own; the other fields are optional dispatch hints:

| Field | Purpose |
|---|---|
| `data` | The payload. Multiline strings are split into multiple `data:` lines per spec; non-string values are JSON-encoded. |
| `event` | Becomes `event.type` on the browser side. Lets `EventSource.addEventListener("foo", …)` route the message. |
| `id` | Sent back as the `Last-Event-ID` header on automatic reconnect — the canonical hook for resumable streams. |
| `retry` | Advises the client's reconnect backoff (milliseconds). Sets the browser's internal retry timer. |
| `comment` | Emits a non-data `: text\n\n` line. Browsers ignore comment frames; useful for keep-alives or transport markers. |

```python
ServerSentEvent(
    event="user.joined",
    id="evt-1234",
    data={"user_id": 42, "name": "Ada"},
    retry=5000,           # browser will retry after 5s on disconnect
)
```

The dataclass is **frozen** because event values flow through asyncio queues and broadcast registries where mutability would be a correctness hazard.

## Keep-alive heartbeats

Network intermediaries (load balancers, reverse proxies, mobile radios) frequently kill idle connections after 30–60 seconds. Pass `keep_alive=N` (seconds) to make `EventStream` emit a comment frame every `N` seconds when the producer has nothing to send:

```python
return EventStream(producer(), keep_alive=15.0)
```

The default heartbeat text is `"keep-alive"` — change it via `keep_alive_comment="…"` if you want a more descriptive marker. Comment frames are spec-mandated to be ignored by the browser's `EventSource` consumer, so they keep the TCP connection live without polluting the application event stream.

**Rule of thumb:** if your producer goes idle for longer than ~30 seconds at a time, set `keep_alive` to roughly half your shortest proxy timeout. For most deployments `keep_alive=15.0` is a safe default.

## Resumability with `Last-Event-ID`

When a client's connection drops, the browser's `EventSource` automatically reconnects and replays the most recently observed `id:` value as the `Last-Event-ID` header. Read it with the `last_event_id` helper to resume a server-side cursor:

```python
from lauren import EventStream, Request, ServerSentEvent, last_event_id

@controller("/notifications")
class NotificationsController:
    def __init__(self, repo: NotificationRepo) -> None:
        self.repo = repo

    @get("/")
    async def stream(self, request: Request) -> EventStream:
        cursor = last_event_id(request.headers) or "0"

        async def producer():
            async for event in self.repo.tail(after=cursor):
                yield ServerSentEvent(
                    event=event.kind,
                    id=str(event.id),               # crucial: lets the client resume
                    data=event.payload,
                )
        return EventStream(producer(), keep_alive=15.0)
```

Two responsibilities for the producer:

1. **Always set `id`** on events the client should be able to resume from. Without `id`, the browser cannot tell the server where it left off.
2. **Respect the inbound cursor**. `last_event_id(request.headers)` returns the previous `id` on reconnect, or `None` on a brand-new connection.

The browser handles the rest — automatic reconnect with exponential backoff, replaying the header, and dropping events the consumer has already seen.

## End-of-stream and cancellation

`EventStream` finishes when its producer's iterator is exhausted. After that the browser sees the connection close and `EventSource` fires its standard reconnect logic — which is usually exactly what you want.

If the client disconnects first, the runtime cancels the producer's coroutine. Wrap any cleanup in a `try`/`finally`:

```python
async def producer():
    handle = await acquire_some_resource()
    try:
        while True:
            yield ServerSentEvent(data=await handle.next_event())
    finally:
        # Runs on normal completion AND on client disconnect.
        await handle.release()
```

For an explicit "end of stream" signal that the browser will respect (no automatic reconnect), the conventional pattern is to send a sentinel event the client knows to handle:

```python
yield ServerSentEvent(event="end", data={"final": True})
```

…and have the client `es.close()` itself when it sees that event. The SSE spec has no formal "stream is done" frame; this is the idiomatic substitute.

## Heterogeneous vs typed streams

`EventStream` is **untyped** by design: it lets you mix event names freely, control the envelope explicitly, and stream JSON payloads of any shape. That's the right tool when the stream represents a *log* of events with varying types.

For a **homogeneous** stream — same Pydantic schema every time — Lauren has a typed alternative, `StreamingResponse[T]`, that content-negotiates between SSE / NDJSON / JSON Lines from the client's `Accept` header:

```python
from typing import AsyncIterator
from pydantic import BaseModel
from lauren import StreamingResponse

class Tick(BaseModel):
    seq: int
    value: float

@get("/ticks")
async def ticks(self) -> StreamingResponse[Tick]:
    async def gen() -> AsyncIterator[Tick]:
        for i in range(100):
            yield Tick(seq=i, value=i * 0.5)
            await asyncio.sleep(0.05)
    return StreamingResponse(gen())
```

A browser client requesting `/ticks` with `Accept: text/event-stream` gets SSE; a `curl` client with `Accept: application/x-ndjson` gets newline-delimited JSON; the same handler. Pick:

* **`EventStream`** when you want explicit `event:` names, custom `id:` values for resumability, or a heterogeneous payload.
* **`StreamingResponse[T]`** when you have one Pydantic schema and want format-flexibility from `Accept`.

## Composing with dependency injection

Producers can take any DI dependency through the controller's constructor. A common pattern is a *queue* injectable that several handlers fan out from:

```python
import asyncio
from lauren import EventStream, ServerSentEvent, injectable

@injectable()
class EventQueue:
    """Pub/sub fan-out. One queue per subscriber."""

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            q.put_nowait(event)

    async def subscribe(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subs.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.remove(q)


@controller("/events")
class EventsController:
    def __init__(self, queue: EventQueue) -> None:
        self._queue = queue

    @get("/")
    async def stream(self) -> EventStream:
        async def producer():
            async for event in self._queue.subscribe():
                yield ServerSentEvent(event=event["kind"], data=event["data"])
        return EventStream(producer(), keep_alive=15.0)
```

Any other handler (HTTP POST, internal task, lifecycle hook) can inject `EventQueue` and call `publish(...)` to fan out to every connected SSE client.

## Authorisation

SSE is plain HTTP, so the standard guards work without modification:

```python
@controller("/private/feed")
@use_guards(AuthenticatedGuard)
class PrivateFeedController:
    @get("/")
    async def stream(self) -> EventStream: ...
```

A few SSE-specific notes:

* The browser's `EventSource` constructor **cannot set custom headers** — including `Authorization`. The standard workarounds are session cookies (already sent automatically) or signed query parameters (`?token=...`).
* Native fetch-streaming (`fetch(url, { headers: ... })`) *can* set headers, but you lose `EventSource`'s auto-reconnect — you have to implement it yourself.
* For tokens passed via cookies, make sure the cookie has `SameSite=Lax` (the default) or `SameSite=None; Secure` if the SSE endpoint is on a different origin.

## Testing

Lauren's `TestClient` is buffered — it collects every body chunk into one `bytes` object before returning. That makes assertions on SSE bodies fully deterministic: the exact wire bytes, including the spec-mandated double newline that terminates each event, are what you assert on.

```python
from lauren.testing import TestClient

def test_feed_emits_three_events():
    client = TestClient(app)
    r = client.get("/feed/")
    assert r.status_code == 200
    assert r.header("content-type") == "text/event-stream; charset=utf-8"

    # Parse the body into individual events. ``parse_sse_body`` is a tiny
    # helper from tests/integration/test_sse.py, ~25 lines of code.
    events = parse_sse_body(r.body)
    assert len(events) == 3
    assert events[0]["event"] == "tick"
    assert events[0]["data"] == '{"seq": 0, "value": 0.0}'
```

For tests that need to assert on per-event timing (rather than the buffered final body), instantiate the response and iterate its stream directly:

```python
async def test_keepalive_arrives_during_idle():
    response = build_response_for_test(...)
    async for chunk in response.stream_body:
        ...
```

## Best practices

* **Always set `id` on resumable events.** Without `id` the browser can't replay `Last-Event-ID` on reconnect. If you don't need resumability, fine — but think about it explicitly.
* **Set `keep_alive` for any long-lived stream.** `15.0` seconds is a safe default for most production proxies.
* **Use `event` names**, not custom JSON discriminators inside `data`. Browsers route on `event:` natively via `addEventListener`, which is much cleaner client-side than parsing `data` and dispatching manually.
* **Disable response buffering** on intermediaries. The headers `EventStream` sets are correct for nginx; if you're behind another proxy (HAProxy, AWS ALB), check its docs for the equivalent opt-out.
* **Cap concurrent streams.** SSE connections are long-lived; without a per-process limit, a misconfigured client can pin file descriptors. Enforce via [`lauren-middlewares.rate_limit`](https://lauren-framework.dev/middlewares/) or an upstream load balancer.
* **Serve under HTTP/2 if you can.** HTTP/1.1 limits each origin to ~6 concurrent connections per browser, which SSE streams can quickly saturate. HTTP/2 multiplexes many streams over one TCP connection and removes the limit.
* **Don't put real-time business logic in the producer.** The producer should be a *transport* — pull from a queue, broadcast registry, or database tail and frame as SSE. The work that *generates* events belongs in a service the producer subscribes to.

## Common patterns

### Progress for a long-running job

```python
@get("/jobs/{job_id}/progress")
async def progress(self, job_id: Path[str], jobs: Depends[JobService]) -> EventStream:
    async def producer():
        async for update in jobs.watch(job_id):
            yield ServerSentEvent(
                event=update.kind,                      # "progress" / "complete" / "failed"
                id=str(update.seq),
                data={"percent": update.percent, "message": update.message},
            )
            if update.terminal:
                return                                  # ends the stream cleanly
    return EventStream(producer(), keep_alive=15.0)
```

### AI text-streaming response

```python
@get("/chat/completions/stream")
async def completions(self, llm: Depends[LLMClient]) -> EventStream:
    async def producer():
        async for token in llm.stream_completion(prompt="..."):
            yield ServerSentEvent(event="token", data=token)
        yield ServerSentEvent(event="done", data={"finished": True})
    return EventStream(producer(), keep_alive=10.0)
```

### Server time / heartbeat-only stream

```python
@get("/time")
async def time(self) -> EventStream:
    async def producer():
        while True:
            yield ServerSentEvent(
                event="time",
                data={"now": datetime.now(timezone.utc).isoformat()},
            )
            await asyncio.sleep(1.0)
    return EventStream(producer())
```

## Errors and edge cases

* **No HTTP error codes mid-stream.** Once the response headers are sent (status 200), the protocol is committed. Detect failure conditions in the *producer* and emit an error-event the client will handle:

  ```python
  try:
      ...
  except Exception as e:
      yield ServerSentEvent(event="error", data={"message": str(e)})
      return
  ```

* **Validation belongs at the entry point.** Validate query parameters / headers / authentication *before* you `return EventStream(...)` so failures still produce conventional 4xx responses. Once the stream is open, you can no longer change the status code.

* **`StreamingResponse[T]` for typed schemas.** When the stream emits one Pydantic model on every iteration, `StreamingResponse[T]` is more explicit and gets you content negotiation (NDJSON, JSON Lines) for free.

## See also

* [WebSockets](websockets.md) — for bidirectional traffic and binary frames.
* [Request & Response](../core-concepts/request-response.md) — `Response.stream(...)` for non-SSE streaming and `Response.sse(...)` for ad-hoc cases that don't need the keep-alive plumbing.
* [Custom Guards](custom-guards.md) — for HTTP-style authorisation; SSE guards work identically.
* [Reference → Cheat Sheet](../reference/cheat-sheet.md) — one-line reminders for all the streaming primitives.
