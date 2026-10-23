# Testing

`lauren.testing` drives a real app in-process — no socket, no server — through
the same seven-phase build and the same request pipeline as production. This
guide covers the lifespan-managed client, loop affinity, cookies and sessions,
request bodies, streaming, simulated disconnects, provider overrides, async
suites and the pytest plugin. The API tables live in the
[Testing reference](../reference/testing.md).

---

## The lifespan-managed client

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, injectable, module, post_construct, pre_destruct
from lauren.testing import TestClient


@injectable()
class Database:
    @post_construct
    async def connect(self) -> None:
        self.connected = True

    @pre_destruct
    async def disconnect(self) -> None:
        self.connected = False


@controller("/health")
class HealthController:
    def __init__(self, db: Database) -> None:
        self.db = db

    @get("/")
    async def health(self) -> dict:
        return {"db": self.db.connected}


@module(controllers=[HealthController], providers=[Database])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)

with TestClient(app) as client:            # startup() on enter
    assert client.get("/health").json() == {"db": True}
    db = client.resolve(Database)          # the live singleton, resolved on the client's loop
# shutdown() on exit: drain → on_shutdown → @pre_destruct → generator teardown
assert db.connected is False
```

`with TestClient(app)` runs `startup()` when the block starts and the full
shutdown when it ends. A failing `@post_construct` or `@pre_destruct` raises its
**original** exception, not a generic lifespan failure.

`TestClient(app)` *without* `with` keeps the 1.x behaviour: a `LaurenApp` is
started immediately, a `Lauren` app on its first request (so routes registered
after creating the client still count). Call `client.close()` to shut it down;
a client that is never closed stops its event loop when it is garbage-collected,
without running shutdown hooks.

| Mode | Startup | Shutdown | `Set-Cookie` stored | Default timeout |
|---|---|---|---|---|
| `with TestClient(app)` | on enter | on exit | yes | 30 s |
| `TestClient(app)` (bare) | at construction (`Lauren`: first request) | `close()` | no (`persist_cookies=True` opts in) | none |
| `client.new_session()` | shared with the parent | owned by the parent | yes | inherited |

`lifespan="off"` makes the client neither start nor stop the app. A `LaurenApp`
built with the default `lazy_startup=True` still starts itself on its first
request (see [lazy startup](#drivers-without-lifespan-events)); build it with
`LaurenFactory.create(..., lazy_startup=False)` to keep it cold.

---

## One event loop per client

Every `TestClient` owns **one asyncio event loop** for its whole life. Startup,
every request, background tasks, WebSockets and shutdown share it, so anything
an app binds to its loop at startup keeps working: `asyncio.Event` / `Queue` /
`Lock`, database and Redis pools, `httpx.AsyncClient`, and tasks spawned in
`@post_construct` (the interval-job pattern).

- A call made from synchronous test code runs that loop **in the test's
  thread**, exactly where 1.x ran each request — handlers see the test thread's
  thread-local state (an in-memory SQLite connection, for instance) and a copy
  of its context variables.
- Between calls a daemon thread keeps the loop running, so background tasks and
  timers make progress while the test sleeps.
- Calling the sync client from inside an `async def` test still works (the call
  is served by that daemon thread and blocks the test's loop until it returns),
  but emits a one-time `RuntimeWarning`: use [`AsyncTestClient`](#async-test-suites).

**One driver per app.** An app's loop-bound resources live on one loop, so an
app is driven by one client at a time. A second `TestClient(app)` created while
the first is alive attaches to the first client's loop; entering it with `with`
raises — use `client.new_session()` for a second user. Restarting an app that
was already shut down raises with a hint to build a fresh app.

---

## Cookies and sessions

`client.cookies` is a browser-like [`CookieJar`](../reference/testing.md#cookiejar):
host-only and `Domain` cookies, default paths, `Max-Age` / `Expires`, deletion on
`Max-Age=0`, and the `__Host-` / `__Secure-` prefix rules. `Secure` cookies are
accepted on `https://` and on the hosts browsers treat as secure contexts —
`testserver`, `localhost`, `127.0.0.1`, `::1` — so the default
`SessionConfig(secure=True)` works on the default `http://testserver` base URL
(`strict_cookies=True` restores RFC-strict behaviour).

<!-- docs-contract: run -->
```python
from lauren import Session, SessionConfig, post


@controller("/account")
class AccountController:
    @post("/login")
    async def login(self, session: Session) -> dict:
        session.regenerate_id()
        session["user_id"] = "u-42"
        return {"ok": True}

    @get("/me")
    async def me(self, session: Session) -> dict:
        return {"user_id": session.get("user_id")}


@module(controllers=[AccountController])
class SessionModule:
    pass


session_app = LaurenFactory.create(SessionModule, sessions=SessionConfig(secret="s" * 32))
with TestClient(session_app) as client:
    client.post("/account/login")                       # Set-Cookie stored in the jar
    assert client.get("/account/me").json() == {"user_id": "u-42"}
    bob = client.new_session()                          # a second browser: empty jar
    assert bob.get("/account/me").json() == {"user_id": None}
    bob.set_session({"user_id": "u-7"})                 # signed by the app's own engine
    assert bob.get("/account/me").json() == {"user_id": "u-7"}
```

`cookies={...}` on a single request is merged over the jar for that request
only. An explicit `Cookie` header turns the jar off for that request.

---

## Request bodies

| Argument | Sends |
|---|---|
| `json=` | `application/json` |
| `data={...}` | `application/x-www-form-urlencoded` (lists repeat the key) |
| `files={...}` (± `data=`) | `multipart/form-data`, WHATWG-escaped part headers |
| `content=b"..."` / `"..."` | raw bytes |
| `content=<(async) iterable>` | one `http.request` message per chunk (`more_body=True`) |

<!-- docs-contract: fragment -->
```python
client.post("/avatars", files={"avatar": ("me.png", png_bytes, "image/png")}, data={"alt": "me"})
client.post("/import", files={"docs": [("a.pdf", a, "application/pdf"), ("b.pdf", b, "application/pdf")]})
client.post("/ingest", content=(chunk for chunk in chunks))
```

An explicit `content-type` header always wins (handy for malformed-boundary
tests). Every request carries a `host` header derived from `base_url`
(`http://testserver` by default); `root_path=` sets `scope["root_path"]`.

---

## Streaming responses

`client.stream(...)` returns status and headers as soon as the app starts the
response, then reads the body incrementally — an endless SSE feed is testable
event by event. `timeout` bounds the wait for each chunk, so a stalled stream
fails instead of hanging the suite.

<!-- docs-contract: fragment -->
```python
with client.stream("GET", "/events") as response:
    assert response.headers["content-type"].startswith("text/event-stream")
    events = response.iter_sse()            # yields lauren.ServerSentEvent
    first = next(events)
# leaving the block delivers http.disconnect: the producer's `finally` has run
```

`iter_bytes()`, `iter_text()` and `iter_lines()` are there too, and
`AsyncTestClient` has the `aiter_*` variants.

---

## Connection semantics and simulated disconnects

The `receive()` callable the app gets behaves like a real server's: after the
body it **blocks** until the response is complete, then reports
`http.disconnect`. A request never looks aborted before its response ends.

To test what happens when the client goes away:

<!-- docs-contract: fragment -->
```python
r = client.get("/reports/slow", disconnect_after=0.2)   # abort 0.2 s after the request starts
assert r.aborted is True
assert r.status_code is None                            # or the status, if it was already sent
```

When the abort fires, a blocked `receive()` returns `http.disconnect`, the rest
of a streamed request body is dropped, and whatever the app sends afterwards is
discarded — as uvicorn does with a dead connection. The call returns only after
the app's coroutine finished (bounded by `timeout`), so its side effects are
observable immediately. Closing a stream (`response.close()`, leaving the
`with`, or cancelling an async stream) aborts the same way; an app that does not
stop by itself is then cancelled, like a dropped connection.

---

## Overriding providers: `Test.create_testing_module`

Replace any provider, guard, interceptor, middleware, exception handler or
module **anywhere in the module graph** — without re-declaring modules — and
let the real factory validate the result:

<!-- docs-contract: run -->
```python
from lauren.testing import Test


@injectable()
class Mailer:
    def send(self, to: str) -> None:
        raise ConnectionError("no SMTP in tests")


@controller("/signup")
class SignupController:
    def __init__(self, mailer: Mailer) -> None:
        self.mailer = mailer

    @post("/")
    async def signup(self) -> dict:
        self.mailer.send("ada@example.com")
        return {"queued": True}


@module(providers=[Mailer], exports=[Mailer])
class MailModule:
    pass


@module(imports=[MailModule], controllers=[SignupController])
class SignupModule:
    pass


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, to: str) -> None:
        self.sent.append(to)


fake = FakeMailer()
testing_app = Test.create_testing_module(SignupModule).override_provider(Mailer).use_value(fake).compile()
with TestClient(testing_app) as client:
    assert client.post("/signup/").json() == {"queued": True}
assert fake.sent == ["ada@example.com"]
```

| Builder call | Replacement |
|---|---|
| `.override_provider(T).use_value(v)` | `v` (a singleton; the original is never instantiated) |
| `.override_provider(T).use_class(C, scope=None)` | `C` built through DI; inherits `T`'s scope |
| `.override_provider(T).use_factory(f, injects=[...])` | `f(*resolved injects)` |
| `.override_provider(T).use_existing(U)` | an alias to `U` |
| `.override_provider(Proto).use_values([a, b])` | the whole `list[Proto]` multi-binding set |
| `.override_guard(G).allow()` / `.deny()` | a guard that always allows / denies (403) |
| `.override_interceptor(I).passthrough()` | just runs the handler |
| `.override_middleware(M).passthrough()` | just calls `call_next` |
| `.override_exception_handler(H).use_value(obj_or_fn)` | class- or function-form handler replacement |
| `.override_module(Old).with_module(New)` | `New` wherever `Old` is imported |

The replacement keeps the target's token, owning module, `provides` and `multi`
flag, so module visibility and Protocol / `list[T]` bindings are unchanged; its
own dependencies resolve from the target's module. `compile()` runs the full
`LaurenFactory.create` pipeline (`Test.create_testing_module(Root, **kwargs)`
takes the same keyword arguments) and fails like production would:

- overriding a token nothing registers raises `MissingProviderError` with
  `detail["reason"] == "override_target_missing"` (`.optional()` opts out);
- a replacement whose dependencies are not visible raises `MissingProviderError`;
- a narrower scope that breaks a consumer raises `DIScopeViolationError`;
- overriding the same token twice raises `DecoratorUsageError` at builder time.

The declarative form reuses the custom-provider records:
`Test.create_testing_module(AppModule, overrides=[use_value(provide=Repo, value=fake)])`.
Overrides are applied while the app is built: nothing is mutated on your
classes, nothing is consulted per request, and two testing modules with
different overrides coexist in one process.

---

## Server exceptions and events

<!-- docs-contract: fragment -->
```python
client = TestClient(app, raise_server_exceptions=True)
client.get("/boom")          # re-raises the handler's RuntimeError with its traceback

with client.capture(RequestComplete) as events:
    client.post("/signup", json={...})
assert events[0].status == 201
```

`raise_server_exceptions=True` re-raises errors that became a 500 (unhandled
exceptions and failing exception handlers); `HTTPError`s and errors an
exception handler turned into a non-5xx response are returned normally. The
client correlates errors through a `lauren.testing` ASGI scope extension
(`request.extensions`), never a header. `capture(*events)` subscribes to the
app's own `SignalBus` for the duration of the block (all events when none are
named).

---

## Async test suites

`AsyncTestClient` has the same keywords and methods, awaited, on the test's own
event loop:

<!-- docs-contract: fragment -->
```python
from lauren.testing import AsyncTestClient

async def test_signup() -> None:
    async with AsyncTestClient(app) as client:          # startup/shutdown in this loop
        r = await client.post("/signup/", json={"name": "Ada"})
        async with client.stream("GET", "/events") as resp:
            first = await anext(resp.aiter_sse())
```

Starting an app that a client already drives on a *different* loop raises a
`RuntimeError` naming both clients. With pytest-asyncio, give session-scoped
async clients `loop_scope="session"`.

---

## Drivers without lifespan events

`httpx.ASGITransport` and some embedded servers never send ASGI lifespan
events. A `LaurenApp` now starts itself on its first HTTP or WebSocket request
in that case (once, even under concurrent first requests) and logs a warning.
To keep a third-party client but control the lifecycle explicitly:

<!-- docs-contract: fragment -->
```python
from lauren.testing import lifespan

async with lifespan(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ...
```

---

## The pytest plugin

Opt in from your `conftest.py` (it is not auto-registered, so installing
`lauren` never adds fixture names to a project):

<!-- docs-contract: fragment -->
```python
pytest_plugins = ["lauren.testing.pytest_plugin"]

@pytest.fixture(scope="module")
def lauren_testing_module():
    return Test.create_testing_module(AppModule).override_provider(Mailer).use_value(FakeMailer())
```

or set `lauren_app = "myapp.main:AppModule"` in the pytest ini options. Fixtures:
`lauren_app`, `lauren_client` (lifespan-managed, scoped by the
`lauren_client_scope` ini option), `lauren_aclient` (needs pytest-asyncio),
`lauren_ws` and `lauren_capture`. A fresh app is built per client scope; an
already-built app object can only go through one lifespan, so it needs
`lauren_client_scope = session`.
