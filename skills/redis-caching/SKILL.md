---
name: redis-caching
description: Backs Lauren's Cache, response caching, idempotency keys and sessions with Redis by implementing the Store protocol over redis.asyncio and registering it with StoresModule. Covers the async RedisStore (SET NX PX, INCR + PEXPIRE, Lua compare-and-delete), CacheModule.for_root, Cache.get_or_set, tag invalidation instead of KEYS/SCAN, and testing against MemoryStore plus the store contract suite. Use when several workers must share a cache.
---

> Use `codemap find "Store"` (or `"CacheModule"`) to locate the protocol and the
> cache before reading — `lauren/_stores/` and `lauren/_cache/`, re-exported from
> `lauren.stores` / `lauren.cache`.

# Redis-backed caching

Lauren's caching runs on the `Store` protocol (`lauren.stores.Store`). Implement
it once over the **async** Redis client and every feature — `Cache`,
`@cache_response`, `@idempotent`, sessions, revocation, `Lock` — shares one
connection pool. There is no hand-written cache service, no blocking client in
`async def` handlers and no `KEYS prefix:*` invalidation.

## RedisStore

```python
from __future__ import annotations

import redis.asyncio as redis

from lauren.stores import PrefixedStore

_DELETE_IF_EQUAL = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _ms(ttl: float | None) -> int | None:
    if ttl is None:
        return None
    if ttl <= 0:
        raise ValueError("ttl must be > 0")
    return max(1, int(ttl * 1000))


class RedisStore:
    """lauren.stores.Store over redis.asyncio (one pool, opened at startup)."""

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self._url = url
        self._r: redis.Redis | None = None

    async def startup(self) -> None:          # StoresModule runs it at app.startup()
        self._r = redis.from_url(self._url)
        await self._r.ping()

    async def aclose(self) -> None:           # ... and this at app.shutdown()
        if self._r is not None:
            await self._r.aclose()

    async def get(self, key: str) -> bytes | None:
        return await self._r.get(key)

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        await self._r.set(key, value, px=_ms(ttl))

    async def delete(self, *keys: str) -> int:
        return await self._r.delete(*keys) if keys else 0

    async def exists(self, key: str) -> bool:
        return bool(await self._r.exists(key))

    async def expire(self, key: str, ttl: float) -> bool:
        return bool(await self._r.pexpire(key, _ms(ttl)))

    async def ttl(self, key: str) -> float | None:
        remaining = await self._r.pttl(key)
        return None if remaining < 0 else remaining / 1000

    async def incr(self, key: str, amount: int = 1, *, ttl: float | None = None) -> int:
        ms = _ms(ttl)
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.incrby(key, amount)
            if ms is not None:
                pipe.pexpire(key, ms, nx=True)    # TTL only when the key had none (Redis 7)
            value, *_ = await pipe.execute()
        return int(value)

    async def set_if_absent(self, key: str, value: bytes, *, ttl: float | None = None) -> bool:
        return bool(await self._r.set(key, value, nx=True, px=_ms(ttl)))

    async def delete_if_equal(self, key: str, expected: bytes) -> bool:
        return bool(await self._r.eval(_DELETE_IF_EQUAL, 1, key, expected))

    def namespace(self, prefix: str) -> PrefixedStore:
        return PrefixedStore(self, prefix)
```

`pexpire(..., nx=True)` keeps the "TTL applies only when `incr` creates the key"
rule; a key created without a TTL that later gets `incr(ttl=...)` would gain
one, so prefer a Lua script if you mix both.

## Wire it in

```python fragment
from lauren import LaurenFactory, SessionConfig, module
from lauren.cache import CacheModule
from lauren.stores import StoresModule

redis_store = RedisStore("redis://redis:6379/0")

@module(
    imports=[
        StoresModule.for_root(default=redis_store),        # lifecycle hooks run with the app
        CacheModule.for_root(namespace="cache", default_ttl=300),
    ],
    controllers=[ProductController],
    providers=[ProductService],
)
class AppModule:
    pass

app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(secret=SECRET, store=redis_store, revocation_store=redis_store, max_age=3600),
)
```

## Cache-aside with tags

```python fragment
from lauren import Path, controller, delete, get, injectable
from lauren.cache import Cache, cache_response


@injectable()
class ProductService:
    def __init__(self, cache: Cache, repo: ProductRepository) -> None:
        self._cache = cache
        self._repo = repo

    async def get(self, product_id: int) -> ProductOut:
        return await self._cache.get_or_set(
            f"product:{product_id}",
            lambda: self._repo.find(product_id),    # awaited; concurrent misses share one call
            ttl=60,
            tags=(f"product:{product_id}", "catalog"),
            type=ProductOut,                        # hits come back as models
            lock=True,                              # one worker computes, the others wait
        )

    async def rename(self, product_id: int, name: str) -> None:
        await self._repo.rename(product_id, name)
        await self._cache.invalidate_tags(f"product:{product_id}", "catalog")   # one INCR each


@controller("/products")
class ProductController:
    def __init__(self, products: ProductService) -> None:
        self._products = products

    @get("/")
    @cache_response(ttl=300, tags=("catalog",))
    async def catalog(self) -> list[ProductOut]: ...

    @get("/{product_id}")
    async def show(self, product_id: Path[int]) -> ProductOut:
        return await self._products.get(product_id)
```

## TTL strategy

| Data | TTL | Notes |
|---|---|---|
| Product detail | 60 s | tag `product:{id}`; invalidate on write |
| Catalog listing | 300 s | `@cache_response(..., tags=("catalog",))` |
| Configuration | 3600 s | `stale_ttl=300` hides refresh latency |
| Computed aggregate | 900 s | `lock=True` against stampedes |

## Testing

Run the app against `MemoryStore` (same protocol, no Redis) and prove the Redis
implementation with the shared contract suite against a real server:

```python fragment
import asyncio
from uuid import uuid4

from lauren.testing.store_contract import run_store_contract


async def test_redis_store_contract(redis_url: str) -> None:
    store = RedisStore(redis_url)
    await store.startup()
    try:
        # A fresh namespace per check keeps the checks independent on one server.
        await run_store_contract(lambda: store.namespace(uuid4().hex), advance=asyncio.sleep, concurrency=200)
    finally:
        await store.aclose()
```

## Common mistakes

- A synchronous `redis.Redis` client inside `async def` handlers — it blocks the
  event loop on every hit and miss. Use `redis.asyncio`.
- `KEYS prefix:*` / `SCAN` to invalidate — use `tags=` and `invalidate_tags`.
- Registering the store with `use_value(provide=Store, ...)` — its
  `startup()` / `aclose()` never run. Use `StoresModule.for_root(default=...)`.
- Caching per-user responses without `@cache_response(key=...)`.
- Expecting models back without `type=` — hits decode to JSON data.
