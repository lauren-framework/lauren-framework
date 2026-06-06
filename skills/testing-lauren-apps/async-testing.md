# Lauren Async Testing — Reference

## Contents
- [App startup in async tests](#app-startup-in-async-tests)
- [Async HTTP client](#async-http-client)
- [Testing SSE](#testing-sse)
- [Testing WebSockets](#testing-websockets)
- [Concurrent request testing](#concurrent-request-testing)

---

## App startup in async tests

`TestClient` calls `startup()` synchronously in `__init__`. For async-only tests that need `@post_construct` hooks to fire without `TestClient`:

```python
import pytest
from lauren import LaurenFactory

@pytest.fixture(scope="module")
async def app():
    a = LaurenFactory.create(AppModule)
    await a.startup()   # explicit startup for async test context
    return a
```

With `asyncio_mode = "auto"` in `pyproject.toml`, async fixtures work without extra decorators.

---

## Async HTTP client

```python
import httpx
import pytest

@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c

async def test_get_user(client):
    resp = await client.get("/users/1")
    assert resp.status_code == 200

async def test_create(client):
    resp = await client.post("/users", json={"name": "Alice"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Alice"
```

---

## Testing SSE

Read chunks from the `text/event-stream` response:

```python
async def test_sse_stream(client):
    chunks = []
    async with client.stream("GET", "/events/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                chunks.append(line[5:].strip())
            if len(chunks) >= 3:
                break
    assert len(chunks) == 3
```

For a simple non-streaming test, consume the full body:

```python
async def test_sse_body(client):
    resp = await client.get("/events/stream")
    assert "data:" in resp.text
```

---

## Testing WebSockets

Use `WsTestClient` — an async-context-manager client separate from `TestClient`:

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
) as ws:
    await ws.send_text("raw text")
    await ws.send_bytes(b"\x00")
    await ws.send_json({"event": "ping"})

    text = await ws.receive_text()
    data = await ws.receive_bytes()
    obj  = await ws.receive_json()

    await ws.close(code=1000)
```

`WsTestClient` also exposes `close_code` and `accepted_subprotocol` attributes after the connection is established.

---

## Concurrent request testing

Verify that sync handlers don't block async ones:

```python
import asyncio
import httpx
import time

async def test_concurrent_requests(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        t0 = time.perf_counter()
        # Fire 5 requests that each take 0.1s sync sleep
        results = await asyncio.gather(*[client.get("/slow") for _ in range(5)])
        elapsed = time.perf_counter() - t0

    assert all(r.status_code == 200 for r in results)
    # They ran concurrently (via anyio.to_thread), so total < 5 * 0.1s
    assert elapsed < 0.4, f"Handlers serialized: {elapsed:.2f}s"
```
