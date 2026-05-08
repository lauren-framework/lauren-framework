# Testing

In-process ASGI test clients for unit and integration tests.

### `TestClient`

```python
class TestClient(app: Any)
```

Synchronous-friendly test client for :class:`LaurenApp`.

#### `TestClient.arequest`

```python
def arequest(self, method: str, url: str, kwargs: Any = {}) -> TestResponse
```

#### `TestClient.request`

```python
def request(self, method: str, url: str, kwargs: Any = {}) -> TestResponse
```

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

#### `TestClient.delete`

```python
def delete(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.patch`

```python
def patch(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.options`

```python
def options(self, url: str, kw: Any = {}) -> TestResponse
```

#### `TestClient.head`

```python
def head(self, url: str, kw: Any = {}) -> TestResponse
```

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
def connect(self, path: str, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, subprotocols: Iterable[str] | None = None, query_string: str = '') -> WebSocketTestSession
```

