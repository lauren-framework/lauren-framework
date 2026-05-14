# Types

Core request/response types and state containers.

## Request & Response

### `Request`

```python
class Request(method: str, path: str, raw_query_string: bytes = b'', headers: Headers | None = None, path_params: dict[str, str] | None = None, client: ClientInfo | None = None, server: ServerInfo | None = None, receive: Callable[[], Awaitable[dict[str, Any]]] | None = None, app_state: AppState | None = None, max_body_size: int = 1048576)
```

Incoming HTTP request.

The request owns its ASGI scope and the ``receive`` callable required to
consume the body. State, route metadata, and app state are attached by the
runtime before the handler executes.

#### `Request.reset`

```python
def reset(self, method: str, path: str, raw_query_string: bytes, headers: Headers, client: ClientInfo, server: ServerInfo, receive: Callable[[], Awaitable[dict[str, Any]]], app_state: AppState, max_body_size: int) -> None
```

Re-initialise this :class:`Request` in place for reuse.

The :class:`lauren._arena.RequestArena` pools ``Request``
instances along with its container dicts. ``reset()`` lets the
dispatcher hand the same object to a new request without
re-running ``__init__`` — the saving is small per-call but
compounds measurably under load.

Every attribute set by ``__init__`` is re-set here; per-request
caches (``_query_params``, ``_cookies``, ``_body``) are cleared
so the previous request's data cannot leak across the pool.
The route-metadata slots (``_matched_route`` etc.) are wiped
too — the dispatcher re-populates them after routing.

#### `Request.body`

```python
def body(self) -> bytes
```

#### `Request.text`

```python
def text(self, encoding: str = 'utf-8') -> str
```

#### `Request.json`

```python
def json(self) -> Any
```

#### `Request.form`

```python
def form(self) -> dict[str, list[str]]
```

#### `Request.stream`

```python
def stream(self) -> AsyncIterator[bytes]
```

#### `Request.get_handler_class`

```python
def get_handler_class(self) -> type | None
```

#### `Request.get_route_handler_func`

```python
def get_route_handler_func(self) -> Callable[..., Any] | None
```

#### `Request.get_route_template`

```python
def get_route_template(self) -> str | None
```

#### `Request.get_matched_route`

```python
def get_matched_route(self) -> Any | None
```

### `Response`

```python
class Response(body: bytes | str | None = b'', status: int = 200, headers: Headers | MutableHeaders | None = None, media_type: str | None = None, stream: AsyncIterable[bytes] | None = None)
```

Immutable HTTP response value object.

Mutating methods (``with_*``) return a new instance. Bodies may be a
``bytes`` blob or an async iterable for streaming responses.

#### `Response.json`

```python
def json(cls, data: Any, status: int = 200, headers: Headers | None = None, encoder: Any = None) -> 'Response'
```

Build a JSON response.

When ``encoder`` is provided it must implement the
:class:`lauren.serialization.JSONEncoder` protocol — the
dispatcher passes in the app's active encoder so every
response uses the configured backend. When omitted (e.g.
tests that build responses directly, or pre-app call sites),
falls back to the process-wide default which starts as the
stdlib encoder and can be swapped via
:func:`lauren.serialization.set_active_encoder`.

#### `Response.text`

```python
def text(cls, data: str, status: int = 200, headers: Headers | None = None) -> 'Response'
```

#### `Response.html`

```python
def html(cls, data: str, status: int = 200, headers: Headers | None = None) -> 'Response'
```

#### `Response.bytes`

```python
def bytes(cls, data: bytes, status: int = 200, media_type: str = 'application/octet-stream', headers: Headers | None = None) -> 'Response'
```

#### `Response.empty`

```python
def empty(cls, status: int = 204) -> 'Response'
```

#### `Response.no_content`

```python
def no_content(cls) -> 'Response'
```

#### `Response.created`

```python
def created(cls, data: Any | None = None, location: str | None = None) -> 'Response'
```

#### `Response.accepted`

```python
def accepted(cls, data: Any | None = None) -> 'Response'
```

#### `Response.redirect`

```python
def redirect(cls, location: str, status: int = 307) -> 'Response'
```

#### `Response.stream`

```python
def stream(cls, iterable: AsyncIterable[bytes], status: int = 200, media_type: str = 'application/octet-stream', headers: Headers | None = None) -> 'Response'
```

#### `Response.file`

```python
def file(cls, path: str, media_type: str | None = None, filename: str | None = None, inline: bool = False, chunk_size: int = 65536, headers: 'Headers | None' = None) -> 'Response'
```

Stream a file from the filesystem asynchronously.

Uses ``anyio.open_file`` for non-blocking reads so the event loop
is never blocked, even for large files.  MIME type is auto-detected
from the file extension when ``media_type`` is omitted.

:param path: Filesystem path to the file (``str`` or ``Path``).
:param media_type: Content-Type override.  Detected automatically
    from the extension when ``None`` (falls back to
    ``application/octet-stream`` for unknown extensions).
:param filename: Name sent in the ``Content-Disposition`` header.
    Defaults to the basename of ``path``.
:param inline: When ``True`` the browser displays the file inline
    (``Content-Disposition: inline``).  When ``False`` (the default)
    the browser opens a Save-As dialog
    (``Content-Disposition: attachment``).
:param chunk_size: Read buffer size in bytes.  Default is 64 KB.
:param headers: Extra response headers merged before the
    Content-Type and Content-Disposition headers are applied.
:raises FileNotFoundError: When ``path`` does not point to an
    existing file.
:return: A streaming :class:`Response` ready to be returned from
    a handler.

Example — serve a generated PDF::

    @get("/report")
    async def report(self) -> Response:
        return await Response.file(
            "/tmp/report.pdf",
            filename="quarterly-report.pdf",
        )

Example — serve an image inline::

    @get("/logo")
    async def logo(self) -> Response:
        return await Response.file(
            "static/logo.png",
            inline=True,
        )

#### `Response.xml`

```python
def xml(cls, data: str, status: int = 200, headers: 'Headers | None' = None) -> 'Response'
```

Build an XML response with ``Content-Type: application/xml``.

:param data: Raw XML content as a string (encoded to UTF-8) or bytes.
:param status: HTTP status code (default 200).
:param headers: Optional extra headers.

Example::

    @get("/feed")
    async def atom_feed(self) -> Response:
        xml = "<feed>...</feed>"
        return Response.xml(xml)

#### `Response.sse`

```python
def sse(cls, iterable: AsyncIterable[str | dict[str, Any]], status: int = 200, encoder: Any = None) -> 'Response'
```

#### `Response.with_status`

```python
def with_status(self, status: int) -> 'Response'
```

#### `Response.with_header`

```python
def with_header(self, key: str, value: str) -> 'Response'
```

#### `Response.with_headers`

```python
def with_headers(self, mapping: Mapping[str, str]) -> 'Response'
```

#### `Response.without_header`

```python
def without_header(self, key: str) -> 'Response'
```

#### `Response.with_media_type`

```python
def with_media_type(self, media_type: str) -> 'Response'
```

#### `Response.with_body`

```python
def with_body(self, body: bytes | str) -> 'Response'
```

#### `Response.with_cookie`

```python
def with_cookie(self, key: str, value: str, max_age: int | None = None, path: str = '/', domain: str | None = None, secure: bool = False, http_only: bool = False, same_site: str | None = None) -> 'Response'
```

#### `Response.delete_cookie`

```python
def delete_cookie(self, key: str, path: str = '/') -> 'Response'
```

### `Headers`

```python
class Headers(items: list[tuple[str, str]] | None = None)
```

Case-insensitive, ordered, multi-value header container.

The primary lookup returns the first value for simplicity; use
:meth:`getall` to retrieve every value for a header name.

#### `Headers.get`

```python
def get(self, key: str, default: Any = None) -> Any
```

#### `Headers.getall`

```python
def getall(self, key: str) -> list[str]
```

#### `Headers.raw`

```python
def raw(self) -> list[tuple[str, str]]
```

#### `Headers.mutable_copy`

```python
def mutable_copy(self) -> 'MutableHeaders'
```

### `MutableHeaders`

```python
class MutableHeaders
```

Mutable variant used when building responses.

#### `MutableHeaders.set`

```python
def set(self, key: str, value: str) -> None
```

#### `MutableHeaders.append`

```python
def append(self, key: str, value: str) -> None
```

#### `MutableHeaders.delete`

```python
def delete(self, key: str) -> None
```

### `ClientInfo`

```python
class ClientInfo(host: str | None, port: int | None)
```

## State

### `State`

```python
class State(initial: Mapping[str, Any] | None = None)
```

Request-scoped state bag with typed accessors.

Attributes can be set either via attribute-style (``state.user = u``) or
with :meth:`set`. Typed retrieval helps middleware/handlers avoid silent
type errors.

#### `State.set`

```python
def set(self, key: str, value: Any) -> None
```

#### `State.get`

```python
def get(self, key: str, default: Any = None) -> Any
```

#### `State.has`

```python
def has(self, key: str) -> bool
```

#### `State.get_typed`

```python
def get_typed(self, key: str, expected: type[T]) -> T | None
```

#### `State.require`

```python
def require(self, key: str, expected: type[T]) -> T
```

#### `State.asdict`

```python
def asdict(self) -> dict[str, Any]
```

### `AppState`

```python
class AppState(initial: Mapping[str, Any] | None = None)
```

Read-only application-level state.

Writes raise :class:`RuntimeError` after the app has been sealed.

#### `AppState.seal`

```python
def seal(self) -> None
```

#### `AppState.set`

```python
def set(self, key: str, value: Any) -> None
```
