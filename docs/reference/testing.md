# Testing

In-process ASGI test clients and testing modules for unit, integration and
end-to-end tests. Lauren's own suite uses them throughout (`tests/unit/`,
`tests/integration/`, `tests/e2e/`), with `pytest` and `pytest-asyncio`
(`asyncio_mode = "auto"`). The teaching story — lifespan, loop affinity,
cookies, streaming, disconnects, overrides — is in the
[Testing guide](../guides/testing.md).

Everything below is importable from `lauren.testing`.

## TestClient

::: lauren.testing.TestClient

Synchronous client with one persistent event loop per client.

```text
TestClient(
    app,                                # LaurenApp | Lauren | any ASGI callable
    *,
    base_url="http://testserver",
    headers=None,                       # default headers for every request
    cookies=None,                       # pre-seeded jar entries
    follow_redirects=False,
    timeout=<30.0; None for a bare client>,
    raise_server_exceptions=False,
    lifespan="auto",                    # "auto" | "on" | "off"
    backend="asyncio",
    backend_options=None,               # {"loop_factory": ...} or {"use_uvloop": True}
    root_path="",
    strict_cookies=False,
    persist_cookies=None,               # None: on with `with` / new_session(), off for a bare client
)
```

* `with TestClient(app) as client:` runs `startup()` on enter and the full
  `shutdown()` on exit. Without `with`, a `LaurenApp` is started at
  construction and a `Lauren` app on its first request (1.x behaviour);
  `close()` shuts down.
* `lifespan="auto"` drives a foreign ASGI app's lifespan protocol only inside
  `with` (an app without lifespan support is tolerated); `"on"` always drives it
  and fails if unsupported; `"off"` never starts or stops anything.
* It passes the URL path to the app **as written**: it does not percent-decode
  it the way ASGI servers do, so `/files/a%2Fb` reaches a handler as `a%2Fb`
  under `TestClient` but as `a/b` under uvicorn.

### Requests

| Method | Signature |
|---|---|
| `request` | `request(method, url, *, params=None, headers=None, cookies=None, json=None, data=None, files=None, content=None, follow_redirects=None, timeout=<client>, disconnect_after=None) -> TestResponse` |
| `get` / `post` / `put` / `patch` / `delete` / `head` / `options` | `get(url, **kw) -> TestResponse` (same keywords) |
| `arequest` | `await client.arequest(method, url, **kw)` — runs on the client's loop without blocking the caller's |
| `stream` | `with client.stream(method, url, **kw) as response:` → `StreamingTestResponse` |
| `websocket_connect` | `with client.websocket_connect(path, *, headers=None, subprotocols=None, query_string="", timeout=5.0) as ws:` → `WebSocketSession` |

| Keyword | Type | Description |
|---|---|---|
| `params` | mapping or list of pairs | Query parameters (lists repeat the key) |
| `headers` | mapping or list of pairs | Request headers (override the client defaults) |
| `cookies` | `Mapping[str, str]` | Merged over the jar for this request only |
| `json` | `Any` | JSON body, `Content-Type: application/json` |
| `data` | mapping / list of pairs | Form body (`application/x-www-form-urlencoded`); with `files=`, multipart fields |
| `files` | mapping / list of pairs | `multipart/form-data` file parts: `(filename, content[, content_type[, headers]])`, a file object, or a list of those per name |
| `content` | `bytes` / `str` / (async) iterable of bytes | Raw body; an iterable is streamed one `http.request` message per chunk |
| `follow_redirects` | `bool \| None` | Follow 301/302/303/307/308 (`history` records the hops) |
| `timeout` | `float \| None` | Wall-clock limit; on expiry the app task is cancelled and `TimeoutError` is raised |
| `disconnect_after` | `float \| None` | Simulate a client abort that many seconds after the request starts |

### Lifecycle and app access

| Member | Description |
|---|---|
| `close()` | Shut down (owning client), cancel leftover tasks with a `RuntimeWarning` naming them, stop the loop |
| `new_session(*, headers=None, cookies=None)` | Same app and loop, empty cookie jar, own default headers; the parent keeps the lifecycle |
| `resolve(token)` | Resolve a DI token from the app's container on the client's loop |
| `capture(*events)` | Context manager collecting the app's lifecycle events (all when none named) |
| `set_session(data)` | Store a valid signed session cookie holding `data`, issued by the app's session engine |
| `cookies` | The client's `CookieJar` |
| `headers` | Default headers (`dict`) |
| `persists_cookies` | Whether `Set-Cookie` responses are stored |
| `app` | The app under test |

## AsyncTestClient

::: lauren.testing.AsyncTestClient

The same keywords (minus `backend*`) and methods, awaited, on the caller's
event loop: `async with AsyncTestClient(app) as client`, `await client.get(...)`,
`async with client.stream(...)`, `async with client.websocket_connect(...)`,
`await client.resolve(token)`, `await client.set_session(data)`,
`client.capture(...)`, `client.new_session()`, `await client.aclose()`. Without
`async with`, the app starts on the first request.

## TestResponse

::: lauren.testing.TestResponse

| Attribute / method | Type | Description |
|---|---|---|
| `status_code` | `int \| None` | HTTP status (`None` only for an abort before the response started) |
| `headers` | `ResponseHeaders` | A `list[tuple[str, str]]` with mapping accessors |
| `body` / `content` | `bytes` | Raw body |
| `text` | `str` | Body decoded with `encoding` (the `charset`, default UTF-8) |
| `json(**kw)` | `Any` | Parse the body as JSON |
| `header(name)` | `str \| None` | First value of a header (case-insensitive) |
| `headers_all(name)` | `list[str]` | Every value of a header |
| `cookies` | `dict[str, str]` | Cookies set by this response |
| `is_success` / `is_redirect` / `is_client_error` / `is_server_error` / `is_error` / `is_informational` | `bool` | Status class checks |
| `reason_phrase` / `http_version` | `str` | `"Not Found"`, `"HTTP/1.1"` |
| `aborted` | `bool` | The client simulated a disconnect before the response completed |
| `raise_for_status()` | `TestResponse` | Raise `HTTPStatusAssertionError` for 4xx / 5xx / aborted |
| `url` | `str` | URL that produced the response |
| `request` | `SentRequest` | `method`, `url`, `headers`, `content` actually sent |
| `elapsed` | `timedelta` | Time from start to completion |
| `history` | `list[TestResponse]` | Redirect hops, oldest first |

## ResponseHeaders

::: lauren.testing.ResponseHeaders

A `list` subclass: iteration yields `(name, value)` tuples, `int` indexing,
`len()` and equality with a list of tuples behave like a list. Adds
case-insensitive `get(name, default=None)` (repeated values joined with `", "`,
as in httpx), `getall(name)` / `get_list(name)`, `headers["name"]`, `keys()`,
`values()`, `items()`, `multi_items()` and `"name" in headers`.

## StreamingTestResponse

::: lauren.testing.StreamingTestResponse

Returned by `client.stream(...)`: the `TestResponse` metadata plus
`iter_bytes()`, `iter_text()`, `iter_lines()`, `iter_sse()` (yields
`lauren.ServerSentEvent`), `read()`, `close()` (sync client) and `aiter_bytes()`,
`aiter_text()`, `aiter_lines()`, `aiter_sse()`, `aread()`, `aclose()` (async
client). `body` holds what has been read so far; `close()` delivers
`http.disconnect` to the app if the response is still in flight.

## HTTPStatusAssertionError

::: lauren.testing.HTTPStatusAssertionError

An `AssertionError` subclass (not a `LaurenError`) with the offending
`response` attribute.

## CookieJar

::: lauren.testing.CookieJar

| Member | Description |
|---|---|
| `get(name, default=None, *, domain=None, path=None)` | Value of the first live cookie with that name |
| `set(name, value, *, domain=None, path="/", secure=False, http_only=False, same_site=None, expires=None)` | Store a cookie (host-only for the client's host unless `domain` is given) |
| `delete(name, *, domain=None, path=None)` / `clear()` | Remove cookies |
| `as_dict()` | `{name: value}` of live cookies |
| `name in jar`, `iter(jar)`, `len(jar)` | Membership, `StoredCookie` records, count |

## StoredCookie

::: lauren.testing.StoredCookie

Frozen record: `name`, `value`, `domain`, `path`, `expires` (Unix time or
`None`), `secure`, `http_only`, `same_site`, `host_only`.

## Test and TestingModuleBuilder

::: lauren.testing.Test

::: lauren.testing.TestingModuleBuilder

`Test.create_testing_module(root_module, *, overrides=(), **factory_kwargs)`
returns a `TestingModuleBuilder`; `compile()` returns the `LaurenApp`.

| Step | Terminal methods |
|---|---|
| `override_provider(token)` | `use_value(v)`, `use_class(cls, *, scope=None)`, `use_factory(fn, *, injects=(), scope=None)`, `use_existing(token)`, `use_values([...])`; `optional()` first to tolerate a missing target |
| `override_guard(G)` | `allow()`, `deny()`, `use_value(obj)`, `use_class(cls)` |
| `override_interceptor(I)` / `override_middleware(M)` | `passthrough()`, `use_value(obj)`, `use_class(cls)` |
| `override_exception_handler(H)` | `use_value(obj_or_fn)`, `use_class(cls)` (class-form only) |
| `override_module(Old)` | `with_module(New)` |

Errors: `MissingProviderError` (`detail["reason"] == "override_target_missing"`)
for a target nothing registers, `StartupError` (same reason) for a module that
is not imported, `DecoratorUsageError` for overriding a token twice or an
invalid replacement, and every startup error the overridden graph triggers.

## lifespan

::: lauren.testing.lifespan

`async with lifespan(app):` runs startup on enter and shutdown on exit in the
running loop — for tests that keep `httpx.AsyncClient(transport=ASGITransport(app))`.

## WsTestClient

::: lauren.testing.WsTestClient

Async WebSocket sessions on the caller's loop (`asyncio.Queue` channels, no
sockets).

```python
client = WsTestClient(app)
async with client.connect("/chat/42") as ws:
    await ws.send_event("chat.send", {"text": "hi"})
    reply = await ws.receive_event()
```

| Method | Signature | Description |
|---|---|---|
| `connect` | `connect(path, *, headers=None, subprotocols=None, query_string="", server_timeout=2.0) -> WebSocketTestSession` | Open a connection; `server_timeout` bounds the server's wind-down on exit |
| `send_text` / `send_bytes` / `send_json` | | Send a frame |
| `send_event` | `send_event(event, data=None)` | Send Lauren's `{"event", "data"}` envelope |
| `receive` / `receive_text` / `receive_bytes` / `receive_json` | | Receive the next frame |
| `receive_event` | `receive_event() -> dict` | Receive the next JSON object frame |
| `close` | `close(code=1000)` | Initiate the close handshake |

`TestClient.websocket_connect(...)` yields a `WebSocketSession` with the same
methods, synchronous, running on the client's loop and carrying the jar's
cookies; every receive is bounded by `timeout`.

## pytest plugin

`pytest_plugins = ["lauren.testing.pytest_plugin"]` (opt-in). Ini options:
`lauren_app = "package.module:attribute"`, `lauren_client_scope` (default
`function`). Fixtures: `lauren_app`, `lauren_client`, `lauren_aclient`,
`lauren_ws`, `lauren_capture`; define `lauren_testing_module` to supply a
`TestingModuleBuilder`.

## Test patterns

- **Unit tests** (`tests/unit/`): isolated component tests, fast execution
- **Integration tests** (`tests/integration/`): full pipeline tests with DI, routing, and middleware
- **Async test support**: `pytest-asyncio` with `asyncio_mode = "auto"` — async test functions run automatically
