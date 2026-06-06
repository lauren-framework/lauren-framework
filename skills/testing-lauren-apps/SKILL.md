---
name: testing-lauren-apps
description: Tests Lauren apps with TestClient (sync) and httpx.AsyncClient (async). Covers conftest setup, env vars before imports, app startup, mock providers, and common assertion patterns. Use when writing unit or integration tests for a Lauren app.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Testing Lauren Apps

## Conftest — env vars MUST come before app imports

```python
# tests/conftest.py
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

# App imports AFTER env vars are set
import pytest
from lauren.testing import TestClient
from myapp.main import app          # or LaurenFactory.create(TestModule)

@pytest.fixture(scope="session")
def client():
    return TestClient(app)
```

`TestClient.__init__` calls `await app.startup()` automatically (idempotent).

## TestClient — synchronous

```python
from lauren.testing import TestClient

client = TestClient(app)

resp = client.get("/users/1")
assert resp.status_code == 200
assert resp.json() == {"id": 1, "name": "Alice"}

resp = client.post("/users", json={"name": "Bob"})
assert resp.status_code == 201

resp = client.get("/items", params={"page": 2, "limit": 5})
resp = client.delete("/users/1")
resp = client.put("/users/1", json={"name": "Updated"})
resp = client.patch("/users/1", json={"name": "Partial"})

# Headers and cookies
resp = client.get("/me", headers={"Authorization": "Bearer token123"})
resp = client.get("/profile", cookies={"session": "abc"})

# Raw bytes body
resp = client.post("/upload", content=b"\x00\x01\x02")
```

### TestResponse

```python
resp.status_code   # int
resp.json()        # parsed JSON body
resp.text          # str body
resp.content       # bytes body
resp.headers       # dict-like (case-insensitive)
resp.headers.get("content-type")
```

## Async client — httpx

For `asyncio_mode = "auto"` pytest-asyncio tests:

```python
import pytest
import httpx

@pytest.fixture
async def aclient(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c

async def test_create_user(aclient):
    resp = await aclient.post("/users", json={"name": "Alice"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "Alice"
```

For WebSocket testing use `WsTestClient` — see [async-testing.md](async-testing.md) for lifecycle startup, streaming, and WebSocket patterns.

## Isolating services with use_value

```python
from lauren import module, use_value, LaurenFactory
from lauren.testing import TestClient

class FakeUsersService:
    async def find_all(self): return [{"id": 1, "name": "Mock"}]

@module(
    controllers=[UsersController],
    providers=[use_value(provide=UsersService, value=FakeUsersService())],
)
class TestModule:
    pass

@pytest.fixture(scope="module")
def client():
    app = LaurenFactory.create(TestModule)
    return TestClient(app)

def test_list(client):
    resp = client.get("/users")
    assert resp.status_code == 200
```

## pyproject.toml for tests

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q -m 'not benchmark'"

[tool.pytest.ini_options.markers]
benchmark = "performance benchmarks (deselected by default)"
```

## Common patterns

### Test 404

```python
def test_not_found(client):
    resp = client.get("/users/9999")
    assert resp.status_code == 404
    assert "not found" in resp.json()["message"].lower()
```

### Test validation error (422)

```python
def test_bad_body(client):
    resp = client.post("/users", json={"invalid": "field"})
    assert resp.status_code == 422
```

### Test with auth header

```python
def test_protected(client, valid_token):
    resp = client.get("/admin/data", headers={"Authorization": f"Bearer {valid_token}"})
    assert resp.status_code == 200
```

## Testing background tasks

`TestClient` runs background tasks synchronously in the same event loop before
the request returns, so side effects are directly assertable:

```python
from lauren import module, controller, post, LaurenFactory, BackgroundTasks, Json
from lauren.testing import TestClient
from pydantic import BaseModel

results = []

async def notify(email: str) -> None:
    results.append(email)

class CreateUser(BaseModel):
    email: str

@controller("/users")
class UsersController:
    @post("/")
    async def create(self, body: Json[CreateUser], tasks: BackgroundTasks) -> dict:
        handle = tasks.add_task(notify, body.email)
        return {"task_id": handle.task_id}

@module(controllers=[UsersController])
class AppModule: pass

client = TestClient(LaurenFactory.create(AppModule))

def test_background_task_ran():
    results.clear()
    resp = client.post("/users", json={"email": "alice@example.com"})
    assert resp.status_code == 200
    assert results == ["alice@example.com"]   # task already ran

def test_task_handle_id_in_response():
    resp = client.post("/users", json={"email": "bob@example.com"})
    assert resp.json()["task_id"]             # non-empty string
```

For signal-based assertions, subscribe before the request:

```python
from lauren import BackgroundTaskFailed

failures = []

def test_failed_task_emits_signal():
    app.signals.on(BackgroundTaskFailed)(failures.append)
    client.post("/path-that-triggers-bad-task")
    assert len(failures) == 1
    assert isinstance(failures[0].error, SomeExpectedError)
```
