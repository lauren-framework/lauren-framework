# Testing

In-process ASGI test clients for unit and integration tests. The test suite contains 2967+ tests across 173 test files, split between `tests/unit/` and `tests/integration/`. All tests use `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`).

## TestClient

::: lauren.testing.TestClient

Synchronous-friendly HTTP test client. Drives a `LaurenApp` via ASGI directly — no socket or server needed.

### HTTP methods

| Method | Signature |
|---|---|
| `get` | `get(url, **kw) -> TestResponse` |
| `post` | `post(url, **kw) -> TestResponse` |
| `put` | `put(url, **kw) -> TestResponse` |
| `patch` | `patch(url, **kw) -> TestResponse` |
| `delete` | `delete(url, **kw) -> TestResponse` |
| `options` | `options(url, **kw) -> TestResponse` |
| `head` | `head(url, **kw) -> TestResponse` |
| `request` | `request(method, url, **kw) -> TestResponse` |

### Keyword arguments

| Argument | Type | Description |
|---|---|---|
| `headers` | `Mapping[str, str]` | Request headers |
| `json` | `Any` | JSON body (auto-sets Content-Type) |
| `content` | `bytes \| str` | Raw body |
| `params` | `Mapping[str, Any]` | Query parameters |
| `cookies` | `Mapping[str, str]` | Request cookies |

### Async support

```python
response = await client.arequest("GET", "/items")
```

## TestResponse

::: lauren.testing.TestResponse

| Attribute/Method | Type | Description |
|---|---|---|
| `status_code` | `int` | HTTP status code |
| `headers` | `list[tuple[str, str]]` | Response headers |
| `body` | `bytes` | Raw response body |
| `text` | `str` | Body decoded as UTF-8 |
| `json()` | `Any` | Parse body as JSON |
| `header(name)` | `str \| None` | First value for header name |
| `headers_all(name)` | `list[str]` | All values for header name |

## WsTestClient

::: lauren.testing.WsTestClient

WebSocket test client. Creates an in-process ASGI WebSocket session using `asyncio.Queue` message channels — no sockets, no server, no timing flakiness.

### Usage

```python
client = WsTestClient(app)
async with client.connect("/chat/42") as ws:
    await ws.send_json({"event": "chat.send", "data": {"text": "hi"}})
    reply = await ws.receive_json()
```

### Methods

| Method | Signature | Description |
|---|---|---|
| `connect` | `connect(path, **kw) -> WebSocketTestSession` | Open a WebSocket connection |
| `send_text` | `send_text(text: str)` | Send a text frame |
| `send_bytes` | `send_bytes(data: bytes)` | Send a binary frame |
| `send_json` | `send_json(payload: Any)` | Send JSON as text frame |
| `receive_text` | `receive_text() -> str` | Receive next text frame |
| `receive_bytes` | `receive_bytes() -> bytes` | Receive next binary frame |
| `receive_json` | `receive_json() -> Any` | Receive and parse JSON |
| `close` | `close(code: int = 1000)` | Initiate close handshake |

### Connect keyword arguments

| Argument | Type | Description |
|---|---|---|
| `headers` | `Mapping[str, str]` | Connection headers |
| `subprotocols` | `Iterable[str]` | Requested subprotocols |
| `query_string` | `str` | URL query string |

## Test patterns

- **Unit tests** (`tests/unit/`): Isolated component tests, fast execution
- **Integration tests** (`tests/integration/`): Full pipeline tests with DI, routing, and middleware
- **Async test support**: `pytest-asyncio` with `asyncio_mode = "auto"` — async test functions run automatically
