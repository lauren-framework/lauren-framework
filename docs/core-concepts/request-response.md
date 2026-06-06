# Request & Response

> Lauren models requests and responses as **value objects**. `Request` is a typed snapshot of incoming data; `Response` is immutable and built up through `with_*` methods. There's no shared global "request context" — everything you need is on the parameters of the function that needs it.

## `Request`

```python
async def handler(self, request: Request) -> dict: ...
```

You'll usually take a more specific extractor (`Path`, `Query`, `Json`, ...) instead of taking `Request` directly, but the full object is always available.

### Properties

| Property | Type |
|---|---|
| `method` | `str` (e.g. `"GET"`) |
| `path` | `str` |
| `url` | full URL with scheme + host + query |
| `path_params` | `dict[str, str]` of matched URL params |
| `query_params` | multi-value query map |
| `headers` | case-insensitive `Headers` |
| `cookies` | parsed cookie map |
| `client` | `ClientInfo(host, port)` |
| `server` | `ServerInfo(host, port)` |
| `state` | per-request `State` (read/write) |
| `app_state` | sealed `AppState` (read-only) |

### Body methods (async)

```python
data = await request.body()        # raw bytes
text = await request.text()        # decoded
parsed = await request.json()      # JSON
form = await request.form()        # form-urlencoded / multipart
async for chunk in request.stream(): ...   # streaming
```

### Introspection

Methods that tell you *what's about to handle me*:

```python
request.get_handler_class()      # the controller class, if any
request.get_route_handler_func() # the bound handler method
request.get_route_template()     # e.g. "/users/{id}"
request.get_matched_route()      # the RouteEntry
```

## `State` and `AppState`

`State` is per-request, `AppState` is per-app.

```python
# Writes
request.state.user_id = 42
request.state.set("scope", "admin")

# Reads (typed)
val = request.state.get_typed("user_id", int)   # None if missing, raises StateTypeError on wrong type
val = request.state.require("user_id", int)     # raises MissingStateError if absent
request.state.has("user_id")                    # bool
```

`AppState` is **sealed after startup**. Writes to `app.app_state` after `LaurenFactory.create(...)` returns raise `RuntimeError`. This is intentional: app-level state is a startup-time configuration, not a runtime mutation surface.

## `Headers`

A case-insensitive, ordered, multi-value mapping. Use `getall(name)` to read all values for a header that legally repeats (e.g. `Set-Cookie`):

```python
request.headers["content-type"]
request.headers.get("authorization")
for cookie in response.headers.getall("set-cookie"): ...
```

## `Response`

Lauren's `Response` is **immutable**. Every "modify" method returns a *new* instance, so you can compose responses without mutating shared state.

### Factories

```python
Response.json(data, *, status=200, headers=None, encoder=None)
Response.text(data, *, status=200, headers=None)
Response.html(data, *, status=200, headers=None)
Response.bytes(data, *, status=200, media_type="application/octet-stream", headers=None)
Response.xml(data, *, status=200, headers=None)
await Response.file(path, *, media_type=None, filename=None, inline=False, chunk_size=65536, headers=None)
Response.empty(status=204)
Response.no_content()
Response.created(data=None, *, location=None)
Response.accepted(data=None)
Response.redirect(location, *, status=307)
Response.stream(async_iterable, *, status=200, media_type="application/octet-stream", headers=None)
Response.sse(async_iterable, *, status=200, encoder=None)
```

### Builders (return new instances)

```python
resp = (
    Response.json({"ok": True})
    .with_status(201)
    .with_header("x-trace", "abc")
    .with_headers({"x-region": "eu", "x-tenant": "acme"})
    .with_cookie("sid", token, http_only=True, same_site="lax", secure=True)
    .with_media_type("application/json; charset=utf-8")
)
```

`without_header(name)`, `delete_cookie(name)`, and `with_body(bytes_or_str)` round out the toolkit.

Every `with_*` builder preserves the concrete response type. That means a custom
`Response` subclass returned by a handler passes through dispatch unchanged and
keeps its subclass-specific helpers and attributes when you chain builders. See
[Custom Responses](../guides/custom-responses.md) and
[File Responses & XML](../guides/file-responses.md).

## Auto-serialization — return what feels right

You almost never have to construct a `Response` yourself. Lauren accepts these handler return shapes and builds the `Response` for you:

| Return value | Result |
|---|---|
| `dict` / `list` / `tuple` of JSON-encodable values | JSON 200 |
| `str` | `text/plain` 200 |
| `None` | `204 No Content` |
| Pydantic v2 `BaseModel` | JSON 200 via `model_dump(mode="json")` |
| `list[BaseModel]` | JSON array of dumps |
| Dataclass instance | JSON 200 |
| `msgspec.Struct` instance | JSON 200 |
| `(body, status)` | the body + given status |
| `(body, status, headers)` | the body + status + extra headers |
| `Response` instance (including subclasses) | passed through unchanged |

Live example, every form in one controller:

```python
@controller("/return-shapes")
class ReturnShapes:
    @get("/dict")
    async def d(self) -> dict:        return {"ok": True}                  # JSON 200

    @get("/text")
    async def t(self) -> str:         return "hello"                       # text/plain 200

    @get("/none")
    async def n(self) -> None:        return None                          # 204

    @get("/model")
    async def m(self) -> UserOut:     return UserOut(id=1, name="x")       # JSON 200

    @get("/list")
    async def lst(self) -> list[UserOut]: return [u1, u2]                  # JSON array

    @post("/created")
    async def c(self):                return {"id": 1}, 201                # JSON 201

    @post("/queue")
    async def q(self):                return {"queued": True}, 202, {"x-q": "default"}

    @get("/raw")
    async def raw(self):              return Response.html("<h1>hi</h1>")  # raw passes through
```

## The default JSON encoder

The encoder that backs `Response.json(...)` and auto-serialization handles, out of the box:

* Pydantic v2 `BaseModel` (via `model_dump(mode="json")`)
* `Enum` (its `value`)
* `datetime` / `date` / `time` (`.isoformat()`)
* `timedelta` (`total_seconds()`)
* `UUID` (`str(...)`)
* `pathlib.PurePath` (`str(...)`)
* `Decimal` (`str(...)`)
* `set` / `frozenset` (as list)
* `bytes` (UTF-8 decoded)
* dataclasses (recursively dumped)
* `msgspec.Struct` instances (converted field-by-field)

Lauren ships four encoder implementations:

* `StdlibJSONEncoder` — the conservative default.
* `OrjsonEncoder` — fastest general-purpose JSON when `orjson` is installed.
* `MsgspecEncoder` — great for `msgspec.Struct` heavy workloads.
* `PydanticEncoder` — routes Pydantic models and `TypeAdapter` dumps through `pydantic-core`'s Rust serializer.

For whole-app behaviour, configure the encoder once at startup:

```python
from lauren import LaurenFactory
from lauren.serialization import PydanticEncoder

app = LaurenFactory.create(AppModule, json_encoder=PydanticEncoder())
```

That same encoder now flows through:

* normal handler auto-serialization
* `Response.json(...)`
* `Response.sse(...)` dict payloads
* `EventStream` JSON payload framing
* structured HTTP error responses
* `WebSocket.send_json(...)`

For one controller or route, override the encoder locally with `@use_encoder(...)`:

```python
from lauren import controller, get, use_encoder
from lauren.serialization import OrjsonEncoder

@controller("/feeds")
@use_encoder(OrjsonEncoder())
class FeedController:
    @get("/")
    async def show(self) -> dict:
        return {"fast": True}
```

Method-level `@use_encoder(...)` wins over controller-level configuration, which wins over the app-wide `json_encoder=` passed to `LaurenFactory.create(...)`.

## Streaming

Two flavors:

```python
@get("/file")
async def file(self) -> Response:
    async def chunks():
        with open("big.bin", "rb") as f:
            while data := f.read(64 * 1024):
                yield data
    return Response.stream(chunks(), media_type="application/octet-stream")

@get("/feed")
async def feed(self, q: Depends[Queue]) -> Response:
    async def producer():
        async for ev in q.subscribe():
            yield ev   # auto-promoted to ServerSentEvent
    return Response.sse(producer())
```

For long-lived SSE streams that need keep-alive comments, use the more featureful `EventStream`:

```python
from lauren import EventStream, ServerSentEvent

@get("/feed")
async def feed(self, q: Depends[Queue]) -> EventStream:
    async def producer():
        async for ev in q.subscribe():
            yield ServerSentEvent(event=ev.kind, data=ev.payload, id=ev.id)
    return EventStream(producer(), keep_alive=15.0)
```

## A word on immutability

The reason `Response` is immutable is the same reason Axum's response type is immutable: **middleware composes responses**. A request-id middleware shouldn't have to worry that adding a header mutates the response that some other middleware later inspects. Every `with_*` returns a new instance, so each middleware layer sees its own consistent snapshot.

You'll find this pattern especially helpful when implementing observability:

```python
@middleware()
class TraceHeaders:
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        return resp.with_header("x-trace-id", request.state.rid)
```

The original `resp` is untouched; the new instance carries the additional header. No shared-state surprises.

You're now done with the Core Concepts. Head to the [Guides](../guides/index.md) to start writing custom extractors, guards, middleware, providers, and exception handlers.
