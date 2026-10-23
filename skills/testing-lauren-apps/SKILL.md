---
name: testing-lauren-apps
description: Tests Lauren apps with TestClient (sync, lifespan-managed) and AsyncTestClient (async). Covers conftest setup, env vars before imports, lifespan, cookies and sessions, uploads, streaming, provider overrides with Test.create_testing_module, the pytest plugin, common assertion patterns, e2e multi-backend testing, and Hypothesis property tests. Use when writing unit, integration, e2e, or property tests for a Lauren app.
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
    with TestClient(app) as c:      # startup() on enter, shutdown() on exit
        yield c
```

`with TestClient(app)` runs startup on enter and the full shutdown
(`@pre_destruct`, generator teardown) on exit. A bare `TestClient(app)` still
starts the app at construction (1.x behaviour). One event loop per client:
resources created at startup (`asyncio.Event`, pools, tasks spawned in
`@post_construct`) keep working across requests.

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
resp = client.get("/profile", cookies={"session": "abc"})   # this request only

# Bodies
resp = client.post("/upload", content=b"\x00\x01\x02")            # raw bytes
resp = client.post("/login", data={"user": "ada", "pw": "x"})      # urlencoded form
resp = client.post("/avatar", files={"file": ("me.png", png, "image/png")})  # multipart
resp = client.post("/ingest", content=(c for c in chunks))         # streamed body

# Redirects, timeouts, simulated aborts
resp = client.get("/old", follow_redirects=True)                   # resp.history
resp = client.get("/slow", timeout=5)                              # TimeoutError on expiry
resp = client.get("/slow", disconnect_after=0.2)                   # resp.aborted is True
```

### TestResponse

```python
resp.status_code                 # int
resp.json()                      # parsed JSON body
resp.text                        # str body
resp.body / resp.content         # bytes body
resp.headers                     # list[tuple[str, str]] ...
resp.headers.get("content-type") # ... with case-insensitive mapping accessors
resp.headers["content-type"]
resp.header("content-type")      # first value, case-insensitive (None if absent)
resp.headers_all("set-cookie")   # every value
resp.cookies                     # cookies set by this response
resp.raise_for_status()          # HTTPStatusAssertionError for 4xx/5xx
```

### Cookies, sessions, streams, WebSockets

```python
with TestClient(app) as client:              # Set-Cookie stored in client.cookies
    client.post("/auth/login", json={...})
    client.get("/account")                   # session cookie replayed
    other = client.new_session()             # second user, empty jar
    client.set_session({"user_id": "u-1"})   # signed by the app's session engine

    with client.stream("GET", "/events") as r:   # endless SSE, event by event
        first = next(r.iter_sse())

    with client.websocket_connect("/chat") as ws:   # carries the jar's cookies
        ws.send_event("chat.send", {"text": "hi"})
        reply = ws.receive_event()
```

## Async client — AsyncTestClient

For `asyncio_mode = "auto"` pytest-asyncio tests:

```python
import pytest
from lauren.testing import AsyncTestClient

@pytest.fixture
async def aclient(app):
    async with AsyncTestClient(app) as c:     # startup/shutdown on the test's loop
        yield c

async def test_create_user(aclient):
    resp = await aclient.post("/users", json={"name": "Alice"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "Alice"
```

Keeping `httpx.AsyncClient(transport=ASGITransport(app))`? Wrap it in
`async with lauren.testing.lifespan(app):` so startup and shutdown run. See
[async-testing.md](async-testing.md) for streaming and WebSocket patterns.

## Replacing providers: Test.create_testing_module

Swap a provider anywhere in the module graph — no test module to re-declare:

```python
from lauren.testing import Test, TestClient

class FakeUsersService:
    async def find_all(self): return [{"id": 1, "name": "Mock"}]

@pytest.fixture(scope="module")
def client():
    app = (
        Test.create_testing_module(AppModule)        # same kwargs as LaurenFactory.create
        .override_provider(UsersService).use_value(FakeUsersService())
        .override_guard(JwtGuard).allow()
        .compile()                                   # full startup validation
    )
    with TestClient(app) as c:
        yield c

def test_list(client):
    resp = client.get("/users")
    assert resp.status_code == 200
```

Also: `.use_class(C)`, `.use_factory(fn, injects=[...])`, `.use_existing(token)`,
`.override_interceptor(I).passthrough()`, `.override_middleware(M).passthrough()`,
`.override_exception_handler(H).use_value(...)`,
`.override_module(Old).with_module(New)`. A target nothing registers fails
`compile()` with `MissingProviderError` (`.optional()` to allow it).

## pytest plugin (opt-in)

```python
# conftest.py
pytest_plugins = ["lauren.testing.pytest_plugin"]

@pytest.fixture(scope="module")
def lauren_testing_module():
    return Test.create_testing_module(AppModule).override_provider(Mailer).use_value(FakeMailer())

def test_hello(lauren_client):      # also: lauren_app, lauren_aclient, lauren_ws, lauren_capture
    assert lauren_client.get("/hello").status_code == 200
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

`TestClient` runs background tasks on the client's event loop before the
request returns, so side effects are directly assertable:

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

For signal-based assertions, capture the events around the request:

```python
from lauren import BackgroundTaskFailed

def test_failed_task_emits_signal():
    with client.capture(BackgroundTaskFailed) as failures:
        client.post("/path-that-triggers-bad-task")
    assert len(failures) == 1
    assert isinstance(failures[0].error, SomeExpectedError)
```

`TestClient(app, raise_server_exceptions=True)` re-raises an unhandled handler
error (the one that became a 500) with its traceback instead of returning the
envelope.

## E2E multi-backend testing

E2E tests (`tests/e2e/`) build a single app with routes for every validator backend and assert the full cycle:

```python
import dataclasses
from typing import TypedDict, Literal
from lauren import Lauren, Json, Discriminated
from lauren.testing import TestClient

@dataclasses.dataclass
class DCItem:
    name: str
    value: int

class TDWidget(TypedDict):
    label: str
    active: bool

class EventA(TypedDict):
    kind: Literal["a"]
    x: int

class EventB(TypedDict):
    kind: Literal["b"]
    y: str

app = Lauren()

@app.post("/dc")
async def dc_endpoint(body: DCItem) -> dict:
    return {"name": body.name, "value": body.value}

@app.post("/td")
async def td_endpoint(body: TDWidget) -> dict:
    return dict(body)

@app.post("/disc")
async def disc_endpoint(body: Json[Discriminated[EventA | EventB, "kind"]]) -> dict:
    return dict(body)

client = TestClient(app.build())

def test_dataclass_backend():
    r = client.post("/dc", json={"name": "x", "value": 42})
    assert r.status_code == 200

def test_typeddict_backend():
    r = client.post("/td", json={"label": "y", "active": True})
    assert r.status_code == 200

def test_discriminated_union():
    r = client.post("/disc", json={"kind": "a", "x": 1})
    assert r.status_code == 200
    r = client.post("/disc", json={"kind": "c", "z": 0})
    assert r.status_code == 422  # unknown discriminator
```

## Blocking optional dependencies

When testing behaviour with pydantic absent, use a **module-scoped** fixture. The root `conftest.py` must pre-import Lauren before any test blocks optional deps — otherwise `_PYDANTIC_AVAILABLE` is permanently set to `False` for the whole session:

```python
# tests/conftest.py  (repo root — add this to every project)
import pytest

@pytest.fixture(scope="session", autouse=True)
def _preload_lauren():
    """Pre-import Lauren with all optional deps available."""
    import lauren           # noqa: F401
    import lauren.streaming # noqa: F401
    import lauren.extractors # noqa: F401
```

```python
# tests/integration/test_no_pydantic.py
import sys
import pytest

@pytest.fixture(autouse=True, scope="module")
def disable_pydantic():
    original = {k: v for k, v in sys.modules.items() if "pydantic" in k}
    for k in list(original.keys()):
        del sys.modules[k]
    sys.modules["pydantic"] = None          # type: ignore[assignment]
    sys.modules["pydantic_core"] = None     # type: ignore[assignment]
    yield
    for k in list(sys.modules.keys()):
        if "pydantic" in k:
            del sys.modules[k]
    sys.modules.update(original)

@pytest.fixture(scope="module")
def app(disable_pydantic):  # explicit dep guarantees ordering
    from lauren import Lauren
    app = Lauren()
    # ... define routes using only dataclasses/TypedDict ...
    return app.build()
```

## Hypothesis property tests

Wrap validation invariants with `@given` for property-based testing. Use `pytest.importorskip` at module level so the file skips cleanly when hypothesis is absent:

```python
import dataclasses
import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, assume  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

@dataclasses.dataclass
class Item:
    name: str
    count: int

class TestValidationProperties:
    @given(
        name=st.text(min_size=1, max_size=100),
        count=st.integers(min_value=0, max_value=10_000),
    )
    def test_valid_data_always_succeeds(self, name: str, count: int):
        from lauren._validation import validate_as
        result = validate_as(Item, {"name": name, "count": count}, field="body")
        assert isinstance(result, Item)

    @given(payload=st.one_of(
        st.integers(), st.text(), st.none(), st.lists(st.integers()),
    ))
    def test_non_dict_always_raises(self, payload):
        from lauren._validation import validate_as
        with pytest.raises(Exception):
            validate_as(Item, payload, field="body")
```

Run with `nox -s tests_property` (installs hypothesis automatically).

---

## Testing with `lauren.reflect`

Use the reflect API to assert on controller/gateway structure without making
HTTP requests:

```python
from lauren.reflect import (
    reflect_guards, reflect_routes, get_controller_metadata,
    get_all_routes, get_all_ws_gateways,
)

# Static (no app needed) — verify decorator metadata
def test_admin_controller_has_auth_guard():
    assert AuthGuard in reflect_guards(AdminController)

def test_user_routes_include_delete():
    methods = {r.method for r in reflect_routes(UserController)}
    assert "DELETE" in methods

# Runtime (requires started app) — verify compiled dispatch table
def test_all_routes_have_guards(app):
    TestClient(app)  # trigger startup
    for route in get_all_routes(app):
        if "/admin" in route.full_path:
            assert route.guards, f"Admin route {route.full_path} has no guards"
```
