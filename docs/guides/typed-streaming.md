# Typed Streaming

> **Typed streaming** is Lauren's first-class bidirectional streaming primitive. It
> adds two orthogonal building blocks — `Stream[T]` for inbound bodies and
> `StreamingResponse[T]` for outbound responses — that share the same wire-format
> vocabulary so the same handler can consume a typed stream from the client and yield
> a typed stream back without any manual framing.

## When to use typed streaming

| Pattern | Use |
|---|---|
| Parse a large request body record-by-record without buffering | `Stream[T]` |
| Return a homogeneous sequence of Pydantic models with auto content negotiation | `StreamingResponse[T]` |
| AI transcription, event relay, data-pipeline HTTP API | `Stream[T]` + `StreamingResponse[T]` |
| One-way push with explicit `event:` names, `id:` resumability, or keep-alive | [`EventStream`](server-sent-events.md) instead |

---

## Outbound: `StreamingResponse[T]`

Annotate the return type with `StreamingResponse[T]` and return an `AsyncIterable` of
`T` values. Lauren negotiates the wire format from the client's `Accept` header and
serializes each item:

```python
import asyncio
from typing import AsyncIterator
from pydantic import BaseModel
from lauren import LaurenFactory, controller, get, module
from lauren import StreamingResponse

class Tick(BaseModel):
    seq: int
    value: float

@controller("/feed")
class FeedController:
    @get("/ticks")
    async def ticks(self) -> StreamingResponse[Tick]:
        async def produce() -> AsyncIterator[Tick]:
            for i in range(100):
                yield Tick(seq=i, value=i * 0.5)
                await asyncio.sleep(0.05)
        return produce()
```

The handler is identical regardless of which wire format the client requests:

| `Accept` header | Wire format | `Content-Type` sent |
|---|---|---|
| `text/event-stream` | SSE | `text/event-stream; charset=utf-8` |
| `application/x-ndjson` | NDJSON (newline-delimited JSON) | `application/x-ndjson` |
| `application/json+stream` or `*/*` or absent | JSON Lines (default) | `application/json+stream` |

Content negotiation is left-to-right: the first recognized media type wins.
Quality values (`q=0.9`) are deliberately ignored — streaming clients rarely use them.

### OpenAPI extension

`StreamingResponse[T]` routes in the OpenAPI document carry `x-streaming: true` on the
operation and advertise all three negotiable content types in the `200` response schema:

```json
{
  "x-streaming": true,
  "responses": {
    "200": {
      "content": {
        "text/event-stream":      {"schema": {"$ref": "#/components/schemas/Tick"}},
        "application/x-ndjson":  {"schema": {"$ref": "#/components/schemas/Tick"}},
        "application/json+stream": {"schema": {"$ref": "#/components/schemas/Tick"}}
      }
    }
  }
}
```

### SSE with `kind` as `event:` name

When `T` has a `kind` attribute and the outbound format is SSE, Lauren automatically
promotes `kind` to the `event:` field, letting browser `EventSource` clients subscribe
per-kind:

```python
from typing import Literal
from pydantic import BaseModel

class ImageEvent(BaseModel):
    kind: Literal["image"]
    url: str

class TextEvent(BaseModel):
    kind: Literal["text"]
    content: str
```

A stream of `ImageEvent(kind="image", url="x.png")` emitted as SSE will produce:

```
event: image
data: {"kind": "image", "url": "x.png"}

```

This composes cleanly with discriminated unions (see below).

---

## Inbound: `Stream[T]`

`Stream[T]` is an extractor marker that turns the request body into a typed async
iterator. Declare the parameter exactly like any other extractor:

```python
from lauren import Stream, controller, post

@controller("/ingest")
class IngestController:
    @post("/records")
    async def ingest(self, records: Stream[Record]) -> dict:
        count = 0
        async for record in records:     # record is a validated Record
            await self._repo.save(record)
            count += 1
        return {"saved": count}
```

The framework reads the ASGI receive loop directly — **no buffering**. Each inbound
chunk is framed according to the request's `Content-Type`:

| `Content-Type` | Wire format parsed |
|---|---|
| `text/event-stream` | SSE (extracts `data:` lines) |
| `application/x-ndjson` | NDJSON (newline-delimited) |
| `application/json+stream` or absent | JSON Lines (default) |

Each line (or SSE block) is JSON-decoded and Pydantic-validated against `T`. Validation
errors surface as `ExtractorError` (422):

- **Before any response headers are sent** — if the first record fails validation, the
  error surfaces as a clean 422 with structured `detail`.
- **After headers are sent** (mid-stream failures) — a trailing error frame is appended
  to the response body. The error frame carries the canonical envelope
  (`{"error": {...}}`) so clients already parsing structured JSON can spot it by the
  top-level `"error"` key.

### Incompatibility with body-reading extractors

`Stream[T]` consumes the ASGI receive loop. Using it on the same handler as `Json[T]`,
`Form[T]`, or `Bytes` raises `StartupError` at startup — the framework rejects the
combination before the first request.

---

## Bidirectional streaming

The two primitives compose naturally for LLM-style or pipeline-style endpoints that
stream in and stream out:

```python
from typing import AsyncIterator
from pydantic import BaseModel
from lauren import Stream, StreamingResponse, controller, post

class AudioChunk(BaseModel):
    seq: int
    text: str

class Transcript(BaseModel):
    text: str
    confidence: float

@controller("/ai")
class TranscriptionController:
    @post("/transcribe")
    async def transcribe(
        self,
        audio: Stream[AudioChunk],
    ) -> StreamingResponse[Transcript]:
        async def produce() -> AsyncIterator[Transcript]:
            async for chunk in audio:
                yield Transcript(
                    text=chunk.text.upper(),
                    confidence=0.95,
                )
        return produce()
```

The client sends newline-delimited `AudioChunk` records. The handler processes each
chunk as it arrives and yields `Transcript` records back in real time. Both inbound and
outbound wire formats are negotiated from `Content-Type` / `Accept` independently —
the client could send NDJSON and receive SSE.

---

## Discriminated unions

`Stream[T]` and `StreamingResponse[T]` both support Pydantic discriminated unions as `T`.
The same `TypeAdapter`-based validation that `Json[T]` uses runs in both directions:

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

@post("/events")
async def relay(
    self, inbound: Stream[Event]
) -> StreamingResponse[Event]:
    async def produce() -> AsyncIterator[Event]:
        async for ev in inbound:
            yield ev      # round-trip, same discriminated union
    return produce()
```

The OpenAPI document emits `oneOf` with `discriminator.mapping` for discriminated-union
streams.

---

## `StreamReader` — advanced access

`Stream[T]` resolves to a `StreamReader[T]` instance at the parameter. The reader is a
standard async iterator (`async for`) and exposes two diagnostic properties:

| Property | Type | Description |
|---|---|---|
| `reader.format` | `str` | Negotiated wire format: `"sse"`, `"ndjson"`, or `"jsonlines"`. |
| `reader.inner_type` | `Any` | The Pydantic type `T` this reader validates against. |

Most user code only needs the `async for` loop. The properties are useful when a single
handler needs to branch on the inbound format (rare in practice — prefer explicit
`Content-Type` routing at the gateway level).

---

## Wire format vocabulary

Both `Stream[T]` and `StreamingResponse[T]` recognise the following media types:

| Canonical name | Media types accepted |
|---|---|
| `sse` | `text/event-stream` |
| `ndjson` | `application/x-ndjson`, `application/ndjson` |
| `jsonlines` | `application/json+stream`, `application/jsonl`, `application/x-jsonlines` |

The default (when `Content-Type` / `Accept` is absent or `*/*`) is **JSON Lines**
(`application/json+stream`).

---

## Error handling

### Inbound validation errors

| When | Behaviour |
|---|---|
| First record invalid | 422 `ExtractorError` with `detail.field`, `detail.format`, `detail.errors` |
| First record has malformed JSON | 422 `ExtractorError` with `detail.fragment` |
| Mid-stream record invalid | Trailing error frame appended; response status stays 200 |

### `try/finally` in the producer

If the client disconnects mid-stream, the outbound generator is cancelled. Wrap
resource cleanup in `try/finally`:

```python
async def produce() -> AsyncIterator[Transcript]:
    handle = await acquire_resource()
    try:
        async for chunk in audio:
            yield Transcript(...)
    finally:
        await handle.release()
```

---

## Testing

The `TestClient` is fully compatible with both primitives. For inbound `Stream[T]`,
post an NDJSON body:

```python
import json
from lauren.testing import TestClient

def _ndjson(items: list[dict]) -> bytes:
    return ("\n".join(json.dumps(i) for i in items) + "\n").encode()

def test_transcription_round_trip():
    client = TestClient(app)
    body = _ndjson([
        {"seq": 1, "text": "hello"},
        {"seq": 2, "text": "world"},
    ])
    r = client.post(
        "/ai/transcribe",
        content=body,
        headers={
            "content-type": "application/x-ndjson",
            "accept": "application/x-ndjson",
        },
    )
    assert r.status_code == 200
    lines = [json.loads(l) for l in r.text.split("\n") if l.strip()]
    assert lines[0]["text"] == "HELLO"
```

For SSE output, `TestClient` buffers the entire response body. Check `x-streaming` in
the OpenAPI document rather than per-frame timing in buffered tests:

```python
def test_streaming_route_flagged_in_openapi():
    doc = TestClient(app).get("/openapi.json").json()
    op = doc["paths"]["/ai/transcribe"]["post"]
    assert op["x-streaming"] is True
    assert "text/event-stream" in op["responses"]["200"]["content"]
```

---

## Comparison with `EventStream`

| | `StreamingResponse[T]` | `EventStream` |
|---|---|---|
| **Payload type** | Homogeneous Pydantic `T` | Heterogeneous — `str`, `dict`, `ServerSentEvent`, … |
| **Wire format** | SSE / NDJSON / JSON Lines (negotiated) | SSE only |
| **`event:` name** | Auto-derived from `kind` attribute | Explicit per `ServerSentEvent.event` |
| **`id:` resumability** | Not built-in | `ServerSentEvent.id` + `last_event_id()` |
| **Keep-alive** | Not built-in | `EventStream(keep_alive=N)` |
| **OpenAPI** | `x-streaming: true`, `oneOf` for unions | Not represented |

**Rule of thumb:** use `StreamingResponse[T]` for API streams where the schema matters
and content negotiation is useful; use `EventStream` when you need explicit SSE
control — custom `event:` names, `id:` resumability, or keep-alive heartbeats.

## See also

* [Server-Sent Events](server-sent-events.md) — `EventStream` for heterogeneous / resumable SSE.
* [Extractors → Cheat Sheet](../reference/cheat-sheet.md) — one-line reminders.
* [Custom Extractors](custom-extractors.md) — for pulling domain values from requests.
* [Reference → Error Catalog](../reference/errors.md) — `ExtractorError` and validation errors.
