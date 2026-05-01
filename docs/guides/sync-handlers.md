# Sync vs Async Handlers

Route handler methods may be declared as either `async def` or plain
`def`.  Lauren supports both transparently, with one important difference
in how each is executed.

---

## Quick comparison

```python
@controller("/items")
class ItemController:

    # Async handler — runs directly on the event loop
    @get("/async/{id}")
    async def get_async(self, id: Path[int]) -> dict:
        result = await self._repo.find(id)
        return {"id": result.id}

    # Sync handler — automatically offloaded to a thread pool
    @get("/sync/{id}")
    def get_sync(self, id: Path[int]) -> dict:
        result = self._repo.find_sync(id)   # blocking call, safe here
        return {"id": result.id}
```

Both handlers work with the same extractors (`Path`, `Query`, `Json`,
`Depends`, custom `ExtractionMarker` subclasses, etc.), and return the
same auto-serialized response types.

---

## How sync handlers are dispatched

When `LaurenFactory.create()` compiles the router, it inspects each
handler function with `inspect.iscoroutinefunction` and stores the result
in `CompiledHandler.is_coroutine`.  At request time the dispatcher
branches on this flag:

| `is_coroutine` | Dispatch |
|---|---|
| `True` (`async def`) | `result = await handler(...)` |
| `False` (`def`) | `result = await anyio.to_thread.run_sync(lambda: handler(...))` |

`anyio.to_thread.run_sync` runs the callable in Python's default
`ThreadPoolExecutor`.  The event loop stays free while the sync function
executes, so other requests — async or sync — continue to be served
concurrently.

!!! note "Why anyio instead of `asyncio.to_thread`?"
    `anyio` is framework-agnostic (asyncio + trio) and is already a
    transitive dependency of `pytest-asyncio`.  Its thread-offload API
    also supports a `cancellable=True` option for cooperating with
    structured-concurrency cancellation scopes.

---

## When to use sync handlers

Prefer sync handlers when:

- You are calling a **synchronous library** (ORM, file I/O, CPU-bound
  computation) that does not have an async API.
- The code is **short and obviously non-blocking** (dict lookups, in-memory
  calculations).
- The existing code base uses sync patterns and migrating to async is
  not worth the disruption.

Prefer async handlers when:

- You are calling an **async database driver** (SQLAlchemy async,
  asyncpg, motor, etc.).
- You are making **outbound HTTP requests** via `httpx.AsyncClient`.
- The handler **awaits** multiple I/O operations that should run in
  parallel via `asyncio.gather`.

---

## Thread-safety considerations

The thread pool runs sync handlers concurrently with the event loop and
with each other.  Keep these rules in mind:

**DI-injected services**: singletons are shared across all requests.
If your sync handler mutates a singleton's state, protect it with a lock.
Request-scoped (`Scope.REQUEST`) and transient (`Scope.TRANSIENT`)
instances are per-request, so they are safe to mutate inside one handler.

```python
import threading

@injectable()   # SINGLETON by default
class Counter:
    def __init__(self):
        self._lock = threading.Lock()
        self._n = 0

    def increment(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

@controller("/counter")
class CounterController:
    def __init__(self, counter: Counter): ...

    @post("/")
    def bump(self) -> dict:
        return {"n": self.counter.increment()}  # safe: uses a lock
```

**Standard library APIs**: `time.sleep`, `open()`, `requests.get()` and
similar blocking calls are safe to use inside sync handlers because the
handler runs in a thread, not on the event loop.

**asyncio objects**: do **not** call `asyncio.Queue.put_nowait` or any
other asyncio primitive from inside a sync handler thread without going
through `asyncio.get_running_loop().call_soon_threadsafe(...)`.  asyncio
objects are not thread-safe.

---

## All HTTP verbs and binding styles

All binding styles work with both sync and async:

```python
@controller("/demo")
class DemoController:

    @get("/instance")
    def instance_method(self) -> dict:          # sync instance method
        return {"binding": "instance"}

    @staticmethod
    @get("/static")
    def static_method() -> dict:                # sync static method
        return {"binding": "static"}

    @classmethod
    @get("/cls")
    def class_method(cls) -> dict:              # sync classmethod
        return {"binding": cls.__name__}

    @post("/body")
    def with_body(self, payload: Json[MyModel]) -> dict:
        return payload.model_dump()             # sync + Json[T]

    @get("/mixed")
    async def async_sibling(self) -> dict:      # async sibling in same controller
        return {"async": True}
```

---

## Auto-serialization return types

Sync handlers coerce their return values exactly the same way async
handlers do:

| Return value | HTTP response |
|---|---|
| `dict` / Pydantic model / dataclass | `200 JSON` |
| `str` | `200 text/plain` |
| `None` | `204 No Content` |
| `(body, 201)` | `201 JSON` |
| `Response` object | passed through unchanged |
| `int`, `list`, `float` | `200 JSON` |

---

## Mixing sync and async in the same controller

```python
@controller("/mixed")
class MixedController:

    @get("/cpu")
    def cpu_work(self) -> dict:
        """Sync: CPU-bound, offloaded to thread pool."""
        result = _heavy_computation()
        return {"result": result}

    @get("/io")
    async def async_io(self) -> dict:
        """Async: I/O-bound, runs on the event loop."""
        result = await self._client.fetch(...)
        return {"result": result}
```

There is no restriction on mixing sync and async handlers in the same
controller.  Each is dispatched according to its own `is_coroutine` flag
compiled at startup.
