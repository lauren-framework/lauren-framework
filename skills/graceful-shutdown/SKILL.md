---
name: graceful-shutdown
description: Shows how to implement graceful shutdown with connection draining in a Lauren application using @post_construct, @pre_destruct lifecycle hooks, and app.on_shutdown() callbacks. Use when you need to safely drain in-flight requests, close database connections, or flush queues before the process exits.
---

> Use `codemap find "pre_destruct"` to locate existing shutdown hooks before adding new ones.

# Graceful Shutdown with Connection Draining

Lauren's lifecycle hooks give you two entry points:

| Hook | Fires | Use for |
|---|---|---|
| `@post_construct` | After DI wires the singleton | Open connection pools, warm caches, register health checks |
| `@pre_destruct` | During `app.shutdown()` | Drain in-flight requests, close DB connections, flush buffers |

Additionally, `app.on_shutdown(callback)` adds ad-hoc shutdown hooks without touching the service class.

## ConnectionPool example

```python
from __future__ import annotations

import asyncio
from lauren import injectable, Scope, post_construct, pre_destruct

@injectable(scope=Scope.SINGLETON)
class ConnectionPool:
    """Simulates a database connection pool with lifecycle management."""

    DRAIN_TIMEOUT = 30.0  # seconds

    def __init__(self) -> None:
        self._connections: list[str] = []
        self._active_requests: int = 0
        self.is_started: bool = False
        self.is_stopped: bool = False

    @post_construct
    async def start(self) -> None:
        """Open connections at startup — called once after DI wiring."""
        self._connections = ["conn1", "conn2", "conn3"]
        self.is_started = True

    @pre_destruct
    async def shutdown(self) -> None:
        """Drain in-flight requests, then close all connections."""
        deadline = asyncio.get_event_loop().time() + self.DRAIN_TIMEOUT
        while self._active_requests > 0:
            if asyncio.get_event_loop().time() >= deadline:
                # Log a warning in production: some requests were forcibly cut
                break
            await asyncio.sleep(0.05)
        self._connections.clear()
        self.is_stopped = True

    def acquire(self) -> str:
        self._active_requests += 1
        return self._connections[0] if self._connections else "none"

    def release(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
```

## Controller

```python
from lauren import controller, get, module

@controller("/api")
class ApiController:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @get("/status")
    async def status(self) -> dict:
        return {
            "started": self._pool.is_started,
            "connections": len(self._pool._connections),
        }

@module(controllers=[ApiController], providers=[ConnectionPool])
class AppModule:
    pass
```

## Ad-hoc shutdown hooks

Use `app.on_shutdown(callback)` to register cleanup logic that lives outside any service class:

```python
import asyncio
from lauren import LaurenFactory

app = LaurenFactory.create(AppModule)

async def flush_analytics():
    # ... flush buffered events to analytics backend ...
    pass

app.on_shutdown(flush_analytics)        # async callbacks supported
app.on_shutdown(lambda: print("bye"))   # sync lambdas too
```

## Triggering shutdown in tests

Call `asyncio.run(app.shutdown())` directly — or use `TestClient` which triggers startup when constructed:

```python
import asyncio
from lauren import LaurenFactory
from lauren.testing import TestClient

def test_post_construct_fires():
    app = LaurenFactory.create(AppModule)
    pool = app.container.resolve(ConnectionPool)
    client = TestClient(app)              # startup fires here
    r = client.get("/api/status")
    assert r.json()["started"] is True

def test_pre_destruct_fires():
    app = LaurenFactory.create(AppModule)
    pool = app.container.resolve(ConnectionPool)
    TestClient(app).get("/api/status")    # trigger startup
    asyncio.run(app.shutdown())
    assert pool.is_stopped is True
```

## Production POSIX signal handling

Lauren integrates with `SIGTERM` and `SIGINT` via `signals.py`:

```python
from lauren import LaurenFactory
from lauren.signals import SignalBus

app = LaurenFactory.create(AppModule)
# uvicorn / hypercorn call app.shutdown() on SIGTERM automatically
# via the ASGI lifespan protocol's "lifespan.shutdown" event.
```

No additional wiring is needed — as long as you use a lifespan-aware ASGI server (uvicorn `--lifespan on`), `@pre_destruct` hooks fire automatically on `SIGTERM`.
