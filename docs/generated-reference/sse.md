# SSE & Streaming

Server-Sent Events, typed streaming responses, and raw byte streams.

## Server-Sent Events

### `EventStream`

```python
class EventStream(iterable: 'AsyncIterable[SSEItem] | Iterable[SSEItem]', status: int = 200, keep_alive: float | None = None, keep_alive_comment: str = DEFAULT_KEEPALIVE_COMMENT, extra_headers: 'Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None' = None)
```

A streaming HTTP response that frames events as Server-Sent Events.

Usage::

    @get("/notifications")
    async def notifications(self, q: Depends[Queue]) -> EventStream:
        async def producer():
            async for ev in q.subscribe():
                yield ServerSentEvent(event=ev.kind, data=ev.payload)
        return EventStream(producer(), keep_alive=15.0)

The wrapped iterable may yield any of the shapes defined by
:data:`SSEItem`:

* :class:`ServerSentEvent` — emitted as-is.
* ``str`` — wrapped in ``ServerSentEvent(data=...)``.
* ``bytes`` — decoded as UTF-8 and wrapped.
* ``Mapping`` — promoted via :meth:`ServerSentEvent.from_dict`.
* any other value — JSON-encoded and wrapped as ``data``.

Keep-alive
----------

Network intermediaries (load balancers, reverse proxies, mobile
radios) frequently kill idle connections after 30–60 seconds. Pass
``keep_alive=N`` (seconds) to have the response emit a comment
frame every ``N`` seconds when the producer has nothing to send.
Comment frames are spec-mandated to be ignored by the browser
``EventSource`` consumer, so they keep the connection live without
polluting the application event stream.

Headers
-------

The response sets:

* ``Content-Type: text/event-stream; charset=utf-8`` — spec media type.
* ``Cache-Control: no-cache`` — disables intermediate caching.
* ``X-Accel-Buffering: no`` — nginx-specific buffering opt-out.
* ``Connection: keep-alive`` — explicit for older proxies.

### `ServerSentEvent`

```python
class ServerSentEvent(data: Any = None, event: str | None = None, id: str | None = None, retry: int | None = None, comment: str | None = None)
```

A single Server-Sent Event with its full envelope.

Per the HTML spec, only ``data`` is meaningful to clients on its
own; the other fields are optional dispatch hints:

* ``event`` becomes ``ev.type`` on the browser side, letting
  ``EventSource.addEventListener("foo", ...)`` route the message.
* ``id`` is sent back as the ``Last-Event-ID`` header on automatic
  reconnects — the canonical hook for resumable streams.
* ``retry`` advises the client's reconnect backoff (milliseconds).
* ``comment`` emits a non-data ``: text\n\n`` line, useful for
  keep-alive pings or human-readable transport markers.

The dataclass is **frozen** because event values flow through
asyncio queues and broadcast registries where mutability would be a
correctness hazard.

#### `ServerSentEvent.from_dict`

```python
def from_dict(cls, mapping: Mapping[str, Any]) -> 'ServerSentEvent'
```

Build a :class:`ServerSentEvent` from a plain mapping.

Used by the framing path so producer generators can yield bare
``{"event": "...", "data": "..."}`` dicts without instantiating
the dataclass themselves. Unknown keys are ignored so callers
can pass through richer shapes without pre-filtering. Missing
keys default to ``None`` (matching the dataclass), which keeps
comment-only and event-only frames from sprouting empty
``data:`` lines.

#### `ServerSentEvent.encode`

```python
def encode(self) -> bytes
```

Return the UTF-8 bytes of this event in the SSE wire format.

The encoded form ends in the spec-mandated double newline
(``\n\n``) that flushes the event on the browser side.
Multiline data values are split into multiple ``data:`` lines
per spec; JSON-able non-string payloads are encoded once with
lauren's permissive serializer.

### `format_sse_event`

```python
def format_sse_event(data: Any = None, event: str | None = None, id: str | None = None, retry: int | None = None, comment: str | None = None) -> str
```

Format a single Server-Sent Event into its on-the-wire string form.

Layered as a free function so the framing logic is unit-testable
without a full :class:`ServerSentEvent` round-trip and so other
callers (the keep-alive task, internal heartbeats) can emit comment
frames cheaply.

Spec compliance notes (HTML Living Standard §9.2):

* Each ``\n`` inside a ``data`` value MUST become its own
  ``data: ...\n`` line. We split on ``\n`` and emit one line per
  segment. Trailing ``\n`` in the value produces an empty
  ``data:`` line, which is still valid framing.
* ``id`` MUST NOT contain a newline. We strip them; an alternative
  would be to raise, but silently scrubbing matches the behaviour
  of every server library I've measured (Starlette, Sanic, Flask).
* ``retry`` MUST be an integer — a non-int value is silently
  omitted (per spec, the browser would discard it anyway).
* ``comment`` lines start with ``:`` and contain no field name.
* The terminating blank line (``\n``) is emitted exactly once at
  the end of the event — we always end with ``\n\n``.

### `last_event_id`

```python
def last_event_id(headers: Headers) -> str | None
```

Read the ``Last-Event-ID`` header off a request, if present.

The browser's ``EventSource`` automatically replays the most
recently observed ``id:`` value as the ``Last-Event-ID`` header on
reconnect. Exposing this as a tiny helper means handlers can resume
server-side cursors without remembering the exact spelling::

    @get("/feed")
    async def feed(self, req: Request) -> EventStream:
        cursor = last_event_id(req.headers) or "0"
        ...

Returns ``None`` when the header is absent or empty.

## Typed streaming

### `StreamingResponse`

```python
class StreamingResponse
```

Return-type marker for typed streaming responses.

``-> StreamingResponse[Transcript]`` tells lauren that the handler will
return an :class:`AsyncIterable` (typically via ``async def produce():
... yield``) of ``Transcript`` values, which the runtime serializes
according to the request's ``Accept`` header. The negotiation vocabulary
matches the inbound :class:`Stream` — SSE, NDJSON, and JSON Lines.

Users should not instantiate this class. ``StreamingResponse[T]`` exists
solely as a type-annotation alias built by :class:`_StreamingResponseMeta`.

## Raw streams

### `Stream`

```python
class Stream
```

Inbound streaming extractor.

Usage::

    @post("/transcribe")
    async def transcribe(self, audio: Stream[AudioChunk]) -> ...:
        async for chunk in audio:
            ...  # chunk is a validated AudioChunk

The framework reads the ASGI receive loop directly, so inbound chunks
are delivered one at a time without the whole body being buffered first.
Each chunk's payload is decoded according to the request's
``Content-Type`` (one of the media types in :data:`MEDIA_TYPE_TO_FORMAT`;
JSON Lines is the default) and validated against the inner type.

``reads_body`` is set because the extractor consumes the ASGI receive
loop; it is incompatible with :class:`~lauren.Json` / :class:`~lauren.Form`
/ :class:`~lauren.Bytes` on the same handler — the handler signature
compiler rejects that combination at startup.

#### `Stream.extract`

```python
def extract(cls, request: Any, extraction: Any, container: Any = None, request_cache: Any = None, owning_module: Any = None) -> 'StreamReader[Any]'
```

Build a :class:`StreamReader` bound to the request's receive loop.

### `StreamReader`

```python
class StreamReader(request: Any, inner_type: Any, format: str, field_name: str)
```

Async iterator producing validated ``T`` values from a streaming body.

Not directly constructed by user code — lauren creates one for each
``Stream[T]`` extractor. It is a thin bridge between the ASGI receive
callable and the handler's ``async for`` loop: every inbound message is
buffered into a line accumulator, complete lines are decoded using the
negotiated wire format, and each decoded payload is validated against
``T`` (supporting both plain Pydantic models and ``Annotated[Union[...],
Field(discriminator=...)]`` tagged unions via :class:`pydantic.TypeAdapter`).
