# Stores & Caching

> One async key-value contract — `Store` — underneath every stateful feature:
> sessions, session revocation, the `Cache` service, response caching,
> idempotency keys and locks. Point them all at one backend and they share its
> connection, its key naming and its bounded memory.

Core ships the protocol, a bounded in-process `MemoryStore`, `Lock`, the
session adapters and `StoresModule` (in `lauren.stores`), plus `CacheModule`,
`Cache`, `@cache_response` and `@idempotent` (in `lauren.cache`). Networked
backends — Redis, a database — belong in companion packages, so core keeps
zero third-party dependencies.

---

## Quick start

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, injectable, module, post
from lauren.cache import Cache, CacheModule, cache_response, idempotent
from lauren.stores import MemoryStore, StoresModule
from lauren.testing import TestClient


@injectable()
class Catalog:
    def __init__(self, cache: Cache) -> None:
        self._cache = cache
        self.queries = 0

    async def product(self, pid: int) -> dict:
        async def load() -> dict:              # the expensive part
            self.queries += 1
            return {"id": pid, "name": f"product {pid}"}

        return await self._cache.get_or_set(f"product:{pid}", load, ttl=60, tags=(f"product:{pid}",))


@controller("/catalog")
class CatalogController:
    def __init__(self, catalog: Catalog, cache: Cache) -> None:
        self.catalog, self.cache = catalog, cache
        self.orders = 0

    @get("/products/{pid}")
    @cache_response(ttl=30, tags=lambda ctx: (f"product:{ctx.request.path_params['pid']}",))
    async def product(self, pid: int) -> dict:
        return await self.catalog.product(pid)

    @post("/products/{pid}/rename")
    async def rename(self, pid: int) -> dict:
        await self.cache.invalidate_tags(f"product:{pid}")
        return {"ok": True}

    @post("/orders")
    @idempotent(ttl=24 * 3600)
    async def order(self) -> dict:
        self.orders += 1
        return {"order": self.orders}


@module(
    imports=[StoresModule.for_root(default=MemoryStore(max_entries=50_000)), CacheModule.for_root()],
    providers=[Catalog],
    controllers=[CatalogController],
)
class AppModule:
    pass


app = LaurenFactory.create(AppModule)
with TestClient(app) as client:
    client.get("/catalog/products/1")
    assert "age" in client.get("/catalog/products/1").headers   # served by the response cache
    client.post("/catalog/products/1/rename")                    # tag invalidation
    client.get("/catalog/products/1")
    assert client.resolve(Catalog).queries == 2

    first = client.post("/catalog/orders", headers={"Idempotency-Key": "abc"})
    retry = client.post("/catalog/orders", headers={"Idempotency-Key": "abc"})
    assert first.json() == retry.json() == {"order": 1}
```

---

## The `Store` model

```python
class Store(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None: ...
    async def delete(self, *keys: str) -> int: ...
    async def exists(self, key: str) -> bool: ...
    async def expire(self, key: str, ttl: float) -> bool: ...
    async def ttl(self, key: str) -> float | None: ...
    async def incr(self, key: str, amount: int = 1, *, ttl: float | None = None) -> int: ...
    async def set_if_absent(self, key: str, value: bytes, *, ttl: float | None = None) -> bool: ...
    async def delete_if_equal(self, key: str, expected: bytes) -> bool: ...
    def namespace(self, prefix: str) -> Store: ...
```

| Operation | Semantics |
|---|---|
| `get` / `set` / `delete` / `exists` | Bytes values, string keys; `delete` returns how many keys existed. |
| `ttl=` | Seconds as a float. `None` means no expiry; `<= 0` raises `ValueError`. |
| `expire` / `ttl` | Set / read the remaining lifetime (`ttl()` is `None` when absent or without expiry). |
| `incr` | Atomic; creates the key at `0`; applies `ttl` **only when it creates the key** — the rate-limit and quota idiom. |
| `set_if_absent` | Atomic `SET NX` — locks, idempotency keys, single-flight across processes. |
| `delete_if_equal` | Atomic compare-and-delete — safe lock release. |
| `namespace("cache")` | A cheap view writing `cache:<key>`; views nest and share the parent's capacity and connection. |

There is no `keys()` / `scan()` / `clear()`: bulk invalidation uses generation
counters (see [tags](#tags-without-scanning)), which every backend can implement
without walking its keyspace.

### `MemoryStore`

```python
from lauren.stores import MemoryStore

store = MemoryStore(max_entries=100_000, sweep_interval=60.0)
```

- **Bounded.** `max_entries` is required; the least recently used entries are
  evicted beyond it, whatever the traffic.
- **No leaks from write-once keys.** Expired entries are dropped on read and
  swept in one pass at most every `sweep_interval` seconds.
- **Loop-agnostic.** Every operation is a short synchronous critical section
  under a `threading.Lock` that never awaits, so one store works across event
  loops and worker threads.
- **Test-friendly.** `clock=` takes a zero-argument callable returning seconds
  (`time.monotonic` by default) — see [Testing](#testing).

---

## Registering stores — `StoresModule`

```python
from typing import Annotated
from lauren import Inject, injectable, module
from lauren.stores import MemoryStore, Store, StoreRegistry, StoresModule, StoreToken


@module(
    imports=[
        StoresModule.for_root(
            default=MemoryStore(max_entries=200_000),
            named={"local": lambda: MemoryStore(max_entries=1_000)},
        )
    ],
)
class InfraModule:
    pass


@injectable()
class Quotas:
    def __init__(
        self,
        store: Store,                                           # the default store
        local: Annotated[Store, Inject(StoreToken("local"))],   # a named store
        registry: StoreRegistry,                                # registry.get("local")
    ) -> None:
        self._store = store.namespace("quota")

    async def consume(self, user_id: str) -> bool:
        used = await self._store.incr(user_id, ttl=3600)        # window starts on first use
        return used <= 1_000
```

- Stores are registered **globally**: any module can inject them.
- A value may be an instance or a **zero-argument factory**, called once per
  app — several apps built from one module (tests do this) never share a store.
  `default=None` creates a per-app `MemoryStore(max_entries=100_000)`.
- Each store is a lifecycle-managed singleton: a backend's `startup()` /
  `aclose()` (or `@post_construct` / `@pre_destruct`) run at `app.startup()` /
  `app.shutdown()`. A bare `use_value(provide=Store, value=...)` registration
  never runs hooks.

### Sessions on the same store

`SessionConfig(store=..., revocation_store=...)` accept any `Store` and adapt it
(`StoreSessionStore`, `StoreRevocationStore`), keyed under `session:` and
`revocation:`. One backend then serves sessions, revocation and the cache:

<!-- docs-contract: fragment -->
```python
store = MemoryStore(max_entries=200_000)

@module(imports=[StoresModule.for_root(default=store), CacheModule.for_root()])
class AppModule: ...

app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(secret=SECRET, store=store, revocation_store=store, max_age=3600),
)
```

The backend's lifecycle hooks run once, even though three features use it.

---

## The `Cache` service

Import `CacheModule.for_root()` once and inject `cache: Cache` anywhere.

```python
from lauren.cache import CacheModule

CacheModule.for_root(
    store=None,          # None: the default Store, else a private MemoryStore; "name": a named store
    namespace="cache",   # entries live under "cache:"
    default_ttl=300,     # seconds; None = no expiry
    codec=None,          # None: JSON through the app's encoder
)
```

| Method | What it does |
|---|---|
| `await cache.get(key, type=None)` | The decoded value, or `None` on a miss. |
| `await cache.set(key, value, ttl=..., tags=())` | Encode and store. |
| `await cache.delete(*keys)` | Remove entries. |
| `await cache.get_or_set(key, factory, ttl=..., tags=(), type=None, lock=False, stale_ttl=None)` | Cache-aside with stampede protection. |
| `await cache.invalidate_tags(*tags)` | Evict every entry written under the tags. |
| `cache.typed(T)` | A view whose reads decode into `T`. |

### Typed values

Values are stored as JSON, encoded exactly as a response would be (models,
dataclasses, datetimes …). A hit decodes back to JSON data unless you pass
`type=` — then it is validated into the type, whichever validation backend owns
it (pydantic, msgspec, dataclass, `TypedDict`), including `list[T]`,
`dict[str, T]` and `T | None`:

<!-- docs-contract: fragment -->
```python
product = await cache.get_or_set(f"product:{pid}", lambda: repo.find(pid), ttl=60, type=ProductOut)
products = cache.typed(ProductOut)
same = await products.get(f"product:{pid}")
```

Cached data that no longer validates — the model changed since it was written —
is a miss, so a deploy that changes a schema recomputes instead of failing.

### Stampede protection

- **Single-flight (always on).** Concurrent misses for one key in one process
  share one factory call. If the factory raises, every waiter gets the exception
  and nothing is cached.
- **`lock=True`.** Also takes a store lease (`set_if_absent`) so only one
  *process* computes; the others poll for up to `lock_wait` seconds and then
  compute themselves — never a deadlock.
- **`stale_ttl=`.** After `ttl`, the entry is kept `stale_ttl` seconds longer:
  readers get it immediately while one caller refreshes it in the background
  (errors are logged, never raised to readers). The refresh may run after the
  request that triggered it ended, so its factory must not close over
  request-scoped objects.

### Tags without scanning

Every tag has a generation counter. An entry records the generations of its
tags when written; a read compares them and treats any difference as a miss.
`invalidate_tags("catalog")` is one `incr` — no `KEYS`, no `SCAN`, on any
backend. Generations are read *before* the factory runs, so an invalidation
that lands while a value is being computed leaves that value already stale.

---

## Response caching — `@cache_response`

```python
from lauren import controller, get
from lauren.cache import cache_response

@controller("/catalog")
class CatalogController:
    @get("/products")
    @cache_response(ttl=30, vary=("accept-language",), tags=("catalog",))
    async def list_products(self, page: int = 1) -> list[ProductOut]: ...

    @get("/me/recommendations")
    @cache_response(ttl=10, key=lambda ctx: ctx.request.state.get("user_id"))   # per user
    async def recommendations(self) -> list[ProductOut]: ...
```

The decorator only attaches metadata; `LaurenFactory.create` compiles it into
the route's **innermost** interceptor. Guards therefore run before every lookup
(an unauthorised request never sees a cached response) and your own
interceptors still observe hits. Routes without the decorator pay nothing.

**The key** is the method (`HEAD` shares `GET`), the route template, the path
parameters, the query string (parameter order does not matter), the request
headers named in `vary`, and whatever `key(ctx)` returns. Use `key=` for
anything user-specific; returning `None` from it skips the cache for that
request.

**A response is stored only when every rule holds** — anything else is a normal
miss, never an error:

| Rule | Why |
|---|---|
| Method is `GET` or `HEAD` | Unsafe methods are never cached (`@idempotent` exists for them). |
| Status in `statuses` (200, 203, 204, 300, 301, 404, 405, 410) | Heuristically cacheable statuses the handler *returned*; raised errors are never stored. |
| Not streamed | A stream cannot be replayed. |
| No `Set-Cookie` | Never replay one client's cookie to another. |
| `Cache-Control` has no `private` / `no-store` / `no-cache` | The handler said so. |
| `Vary` names only headers in `vary=` (and is never `*`) | The key must cover what the response varies on. |

**On a hit** the status, headers (minus hop-by-hop ones) and body are replayed
with an `Age` header; `debug_header=True` adds `X-Cache: HIT` / `MISS`. Session
cookies are written by the session engine outside the cache, so the current
user's session still rolls. Concurrent misses for one key share one handler run
when the result is storable.

`@cache_response` is the *server-side* cache. It never emits `Cache-Control`;
whatever `Cache-Control`, `ETag` or `Last-Modified` the handler sets is stored
and replayed, so downstream caches and conditional requests see the same
headers on hits. A request's own `Cache-Control: no-cache` is ignored unless you
pass `respect_client_no_cache=True` (otherwise any client could bust the cache at
will). OpenAPI marks cached operations with `x-cache-ttl`.

!!! warning "Per-user data needs `key=`"
    Without `key=`, every caller shares one entry per URL. A cached route that
    injects `session: Session` without `key=` (or `vary` on `cookie` /
    `authorization`) logs a startup warning.

---

## Idempotency keys — `@idempotent`

Retrying a `POST` must not charge twice. Clients send an `Idempotency-Key`
header; the first request runs the handler and its response is stored; retries
replay it.

```python
from lauren import Json, post
from lauren.cache import idempotent

@post("/payments")
@idempotent(ttl=24 * 3600, scope=lambda ctx: ctx.request.state.get("user_id"))
async def create_payment(self, body: Json[PaymentIn]) -> PaymentOut: ...
```

| Situation | Response |
|---|---|
| No header (`required=True`, the default) | `400`, `code="idempotency_key_missing"` |
| First request with key `K` | Handler runs; a response with status < 500 is stored for `ttl` |
| Same `K`, same payload, finished | Stored response replayed with `Idempotent-Replayed: true` |
| Same `K` while the first request still runs | `409` `ConflictError` with `Retry-After: 1` |
| Same `K`, different payload | `422`, `code="idempotency_key_mismatch"` |
| Handler raised | Nothing stored — the retry runs the handler again |

- The payload fingerprint covers the method, route template, path, query and
  body; the body is read (within `max_body_size`) before the handler runs.
- `scope(ctx)` partitions keys per user or tenant — use it, so one client can
  never replay another's response.
- `Set-Cookie` is dropped from stored responses; streamed responses are not
  stored.
- `lock_ttl` (default 60 s) bounds how long an in-flight request holds its key.

---

## Locks

`Lock` is a lease over any store: `set_if_absent` with a random token to
acquire, `delete_if_equal` to release, so a holder whose lease already expired
can never release someone else's lock.

<!-- docs-contract: fragment -->
```python
from lauren.stores import Lock

async with Lock(store, "nightly-report", ttl=300, wait=5):
    await build_report()          # TimeoutError if the lock stayed taken for 5 s
```

The same two primitives give other features a distributed lock. A scheduler's
single-runner lock (`acquire(key, *, ttl) -> bool`, `release(key)`), for
example, plugs into any store like this:

<!-- docs-contract: fragment -->
```python
import secrets

class StoreBackedLock:
    def __init__(self, store: Store) -> None:
        self._store = store.namespace("locks")
        self._tokens: dict[str, bytes] = {}

    async def acquire(self, key: str, *, ttl: float) -> bool:
        token = secrets.token_hex(16).encode()
        if await self._store.set_if_absent(key, token, ttl=ttl):
            self._tokens[key] = token
            return True
        return False

    async def release(self, key: str) -> None:
        token = self._tokens.pop(key, None)
        if token is not None:
            await self._store.delete_if_equal(key, token)
```

---

## Several workers

`MemoryStore` lives in one process. With several workers, give every feature a
shared backend: implement `Store` over Redis (or use a companion package that
does) and pass it to `StoresModule.for_root(default=...)`. Implement
`namespace()` by returning `PrefixedStore(self, prefix)` and prove the backend
with the [contract suite](#testing):

<!-- docs-contract: fragment -->
```python
import redis.asyncio as redis
from lauren.stores import PrefixedStore


class RedisStore:
    def __init__(self, url: str) -> None:
        self._url = url
        self._r: redis.Redis | None = None

    async def startup(self) -> None:              # run by StoresModule at app.startup()
        self._r = redis.from_url(self._url)

    async def aclose(self) -> None:               # run at app.shutdown()
        await self._r.aclose()

    async def get(self, key: str) -> bytes | None:
        return await self._r.get(key)

    async def set_if_absent(self, key: str, value: bytes, *, ttl: float | None = None) -> bool:
        return bool(await self._r.set(key, value, nx=True, px=int(ttl * 1000) if ttl else None))

    # … set / delete / exists / expire / ttl / incr (INCR + PEXPIRE in MULTI) /
    #   delete_if_equal (a Lua compare-and-delete) …

    def namespace(self, prefix: str) -> PrefixedStore:
        return PrefixedStore(self, prefix)
```

---

## Testing

Inject a fake clock instead of sleeping:

<!-- docs-contract: run -->
```python
import asyncio

from lauren.cache import Cache
from lauren.stores import MemoryStore


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def entries_expire() -> None:
    clock = ManualClock()
    cache = Cache(MemoryStore(max_entries=100, clock=clock), clock=clock)
    await cache.set("greeting", "hi", ttl=10)
    clock.now += 11
    assert await cache.get("greeting") is None


asyncio.run(entries_expire())
```

`CacheModule.for_root(clock=...)` and `MemoryStore(clock=...)` do the same for a
whole app (the `Age` header and stale-while-revalidate use the cache's clock).
Swap the cache or a store in a test with provider overrides:
`Test.create_testing_module(AppModule).override_provider(Cache).use_value(fake).compile()`.

A store backend proves it implements the protocol with the shared contract
suite:

<!-- docs-contract: fragment -->
```python
import pytest
from lauren.testing.store_contract import STORE_CONTRACT_CHECKS, run_store_contract

@pytest.mark.parametrize("check", STORE_CONTRACT_CHECKS)
async def test_my_store_contract(check: str) -> None:
    clock = ManualClock()
    await run_store_contract(lambda: MyStore(clock=clock), advance=clock.advance, checks=[check])
```

`advance(seconds)` may be async — a networked backend can pass `asyncio.sleep`
with short TTLs. Without it the expiry checks are skipped.

---

## What fails at startup

| Mistake | Error |
|---|---|
| `@cache_response` / `@idempotent` on a route but no `CacheModule` (or `Cache` provider) | `StartupError`, `reason="cache_module_missing"` |
| `@cache_response` used bare, without `ttl`, with an unsafe method, stacked, or together with `@idempotent` | `DecoratorUsageError` |
| `MemoryStore(max_entries=0)` | `ValueError` at construction |
| `CacheModule.for_root(store="name")` naming an unregistered store | `StartupError`, `reason="invalid_store"` |
| Two different `CacheModule.for_root(...)` modules | `StartupError`, `reason="conflicting_cache_options"` |
| Two default stores, or one name registered twice | `StartupError`, `reason="duplicate_store_name"` |

---

## Non-goals

- **Method caching by decorator** (`@cached` on service methods). It would need
  the container to wrap methods at construction — decorators in Lauren never
  wrap. `Cache.get_or_set` is the explicit alternative.
- **A pickle codec.** Values cross process boundaries; JSON is the default and a
  custom `CacheCodec` is an explicit choice.
- **Downstream cache semantics.** `Cache-Control`, `ETag` and conditional
  requests are a separate concern; `@cache_response` only caches server-side.
