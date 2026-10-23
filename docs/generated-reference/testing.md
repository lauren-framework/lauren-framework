# Testing

In-process ASGI test clients for unit and integration tests.

### `TestClient`

```python
class TestClient(app: Any, base_url: str = 'http://testserver', headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None, follow_redirects: bool = False, timeout: float | None = _UNSET, raise_server_exceptions: bool = False, lifespan: LifespanMode = 'auto', backend: Literal['asyncio'] = 'asyncio', backend_options: Mapping[str, Any] | None = None, root_path: str = '', strict_cookies: bool = False, persist_cookies: bool | None = None)
```

Synchronous in-process client for a :class:`~lauren.LaurenApp`, a ``Lauren`` app or any ASGI app.

One persistent event loop (a daemon "portal" thread) per client runs the
app's startup, every request, background tasks, WebSockets and shutdown,
so loop-bound resources created at startup keep working.

* ``with TestClient(app) as client:`` runs ``startup()`` on enter and
  ``shutdown()`` (drain → ``on_shutdown`` → ``@pre_destruct`` → generator
  teardown) on exit; a failing hook raises its original exception.
* ``TestClient(app)`` without ``with`` keeps the 1.x behaviour: a
  ``LaurenApp`` is started right away, a ``Lauren`` app on its first
  request. ``close()`` shuts it down; a client that is never closed
  stops its loop when garbage-collected (without shutdown hooks).
* Cookies in :attr:`cookies` are sent with every matching request.
  ``Set-Cookie`` responses are stored there when the client is used as a
  context manager or comes from :meth:`new_session`; a bare
  ``TestClient(app)`` keeps the 1.x behaviour of not storing them unless
  ``persist_cookies=True`` (2.0 stores them for every client).

Creating a second client for an app that a live client already drives
attaches it to that client's loop (it shares the lifecycle, like
:meth:`new_session`); entering such a client with ``with`` raises.

#### `TestClient.close`

```python
def close(self) -> None
```

Shut the app down (owning client only) and stop the event loop.

Runs drain → ``on_shutdown`` → ``@pre_destruct`` → generator teardown
(unless ``lifespan="off"``), then cancels tasks the app left running
— with a :class:`RuntimeWarning` naming them — and stops the portal
thread. A client from :meth:`new_session` only detaches.

#### `TestClient.new_session`

```python
def new_session(self, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None) -> 'TestClient'
```

A client for the same app and loop with its own (empty) cookie jar and default headers.

The new client shares this client's lifecycle: this client keeps
ownership of startup and shutdown.

#### `TestClient.request`

```python
def request(self, method: str, url: str, params: Any = None, headers: Any = None, cookies: Mapping[str, str] | None = None, json: Any = None, data: Any = None, files: Any = None, content: Any = None, follow_redirects: bool | None = None, timeout: float | None = _UNSET, disconnect_after: float | None = None) -> TestResponse
```

Send one request and return the buffered :class:`TestResponse`.

#### `TestClient.arequest`

```python
def arequest(self, method: str, url: str, kwargs: Any = {}) -> TestResponse
```

``await``-able :meth:`request`: runs on the client's loop without blocking the caller's.

#### `TestClient.get`

```python
def get(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.post`

```python
def post(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.put`

```python
def put(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.patch`

```python
def patch(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.delete`

```python
def delete(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.head`

```python
def head(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.options`

```python
def options(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.stream`

```python
def stream(self, method: str, url: str, params: Any = None, headers: Any = None, cookies: Mapping[str, str] | None = None, json: Any = None, data: Any = None, files: Any = None, content: Any = None, timeout: float | None = _UNSET, disconnect_after: float | None = None) -> Iterator[StreamingTestResponse]
```

Send a request and read the response incrementally.

``timeout`` bounds the wait for the response start and for each body
chunk, so an endless stream cannot hang the test. Leaving the block
(or ``response.close()``) delivers ``http.disconnect`` to the app if
the response is still in flight. Redirects are not followed.

#### `TestClient.websocket_connect`

```python
def websocket_connect(self, path: str, headers: Any = None, subprotocols: Iterable[str] | None = None, query_string: str = '', timeout: float | None = 5.0) -> Iterator[WebSocketSession]
```

Open a WebSocket on the client's loop; the jar's cookies ride along.

``timeout`` bounds every receive and the server's wind-down after the
block exits.

#### `TestClient.resolve`

```python
def resolve(self, token: Any) -> Any
```

Resolve ``token`` from the app's DI container, on the client's loop.

#### `TestClient.capture`

```python
def capture(self, events: type = ()) -> Iterator[list[Any]]
```

Collect the lifecycle events (all of them when none are named) emitted inside the block.

#### `TestClient.set_session`

```python
def set_session(self, data: Mapping[str, Any]) -> None
```

Store a valid signed session cookie holding ``data``, issued by the app's session engine.

### `WsTestClient`

```python
class WsTestClient(app: Any)
```

Factory for :class:`WebSocketTestSession` bound to an ASGI app.

Mirrors the ergonomic pattern of :class:`TestClient` — one client
instance per app, one :class:`WebSocketTestSession` per connection.
Use as::

    client = WsTestClient(app)
    async with client.connect("/chat/42", headers={...}) as ws:
        ...

#### `WsTestClient.connect`

```python
def connect(self, path: str, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, subprotocols: Iterable[str] | None = None, query_string: str = '', server_timeout: float = 2.0) -> WebSocketTestSession
```

Open a session; ``server_timeout`` bounds the server's wind-down on exit.
