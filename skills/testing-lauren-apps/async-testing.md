# Lauren Async Testing — Reference

## Contents
- [App startup in async tests](#app-startup-in-async-tests)
- [Async HTTP client](#async-http-client)
- [Testing SSE](#testing-sse)
- [Testing WebSockets](#testing-websockets)
- [Concurrent request testing](#concurrent-request-testing)

---

## App startup in async tests

`AsyncTestClient` runs startup and shutdown on the test's own event loop:

```python
import pytest
from lauren import LaurenFactory
from lauren.testing import AsyncTestClient

@pytest.fixture
async def client():
    async with AsyncTestClient(LaurenFactory.create(AppModule)) as c:   # startup() / shutdown()
        yield c
```

With `asyncio_mode = "auto"` in `pyproject.toml`, async fixtures work without
extra decorators. A session-scoped async client needs a session-scoped loop
(pytest-asyncio: `loop_scope="session"`); an app driven by a client on another
loop raises `RuntimeError`.

Keeping a third-party client? Wrap it in `lauren.testing.lifespan(app)`:

```python
from lauren.testing import lifespan

async with lifespan(app):            # startup on enter, shutdown on exit
    ...                              # e.g. httpx.AsyncClient(transport=ASGITransport(app=app))
```

(Without either, a `LaurenApp` starts itself on its first request and logs a warning.)

---

## Async HTTP client

```python
async def test_get_user(client):
    resp = await client.get("/users/1")
    assert resp.status_code == 200

async def test_create(client):
    resp = await client.post("/users", json={"name": "Alice"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Alice"
```

Same keywords as `TestClient`: `params`, `headers`, `cookies`, `json`, `data`,
`files`, `content`, `follow_redirects`, `timeout`, `disconnect_after`.

---

## Testing SSE

Read events as they arrive; leaving the block delivers `http.disconnect` so the
producer's `finally` runs:

```python
async def test_sse_stream(client):
    async with client.stream("GET", "/events/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
        events = []
        async for event in resp.aiter_sse():      # lauren.ServerSentEvent
            events.append(event.data)
            if len(events) >= 3:
                break
    assert len(events) == 3
```

For a finite stream, consume the full body:

```python
async def test_sse_body(client):
    resp = await client.get("/events/stream")
    assert "data:" in resp.text
```

---

## Testing WebSockets

Use `client.websocket_connect(...)` on an `AsyncTestClient` (it carries the
jar's cookies), or `WsTestClient` — an async-context-manager client separate
from `TestClient`:

```python
from lauren.testing import WsTestClient

async def test_chat(app):
    client = WsTestClient(app)
    async with client.connect("/ws/room1") as ws:
        await ws.send_json({"event": "chat.send", "text": "hello"})
        msg = await ws.receive_json()
        assert msg["event"] == "chat.message"
        assert msg["text"] == "hello"
```

### WsTestClient methods

```python
async with client.connect(
    "/ws/path",
    headers={"Authorization": "Bearer ..."},
    subprotocols=["chat"],
    query_string="token=abc",
    server_timeout=2.0,          # bounds the server's wind-down on exit
) as ws:
    await ws.send_text("raw text")
    await ws.send_bytes(b"\x00")
    await ws.send_json({"event": "ping"})

    text = await ws.receive_text()
    data = await ws.receive_bytes()
    obj  = await ws.receive_json()

    await ws.send_event("chat.send", {"text": "hi"})   # {"event": ..., "data": ...}
    frame = await ws.receive_event()                   # the next JSON object frame

    await ws.close(code=1000)
```

`WsTestClient` also exposes `close_code` and `accepted_subprotocol` attributes after the connection is established.

---

## Concurrent request testing

Verify that sync handlers don't block async ones:

```python
import asyncio
import time

from lauren.testing import AsyncTestClient

async def test_concurrent_requests(app):
    async with AsyncTestClient(app) as client:
        t0 = time.perf_counter()
        # Fire 5 requests that each take 0.1s sync sleep
        results = await asyncio.gather(*[client.get("/slow") for _ in range(5)])
        elapsed = time.perf_counter() - t0

    assert all(r.status_code == 200 for r in results)
    # They ran concurrently (via anyio.to_thread), so total < 5 * 0.1s
    assert elapsed < 0.4, f"Handlers serialized: {elapsed:.2f}s"
```
