---
name: redis-caching
description: Adds a Redis-backed caching layer with TTL and prefix-based invalidation to a Lauren app. Use when you need to cache expensive service results or database queries with explicit expiry and bulk invalidation.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Redis Caching Layer with Invalidation Strategies

## Overview

`CacheService` wraps a Redis client and provides `get`, `set`, `delete`, and
`invalidate_prefix` operations. Because it is a `SINGLETON`, a single
connection pool is shared across all requests. Use `fakeredis.FakeRedis` in
tests to avoid a real Redis dependency.

## CacheService

```python
import json
import redis
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class CacheService:
    def __init__(self) -> None:
        # Production: redis.Redis(host="redis", port=6379, decode_responses=True)
        self._redis = redis.Redis(host="localhost", port=6379, decode_responses=True)

    def get(self, key: str):
        val = self._redis.get(key)
        return json.loads(val) if val is not None else None

    def set(self, key: str, value, ttl: int = 300) -> None:
        self._redis.setex(key, ttl, json.dumps(value))

    def delete(self, key: str) -> None:
        self._redis.delete(key)

    def invalidate_prefix(self, prefix: str) -> int:
        """Delete all keys matching `prefix:*`. Returns number of keys deleted."""
        keys = self._redis.keys(f"{prefix}:*")
        if keys:
            return self._redis.delete(*keys)
        return 0
```

## Wiring with fakeredis for tests

Override the Redis client at construction time by accepting an optional
`client` argument, or use `use_value` to supply a pre-built `CacheService`:

```python
import fakeredis
from lauren import use_value, module

fake_redis = fakeredis.FakeRedis(decode_responses=True)
cache_svc = CacheService.__new__(CacheService)
cache_svc._redis = fake_redis

@module(providers=[use_value(provide=CacheService, value=cache_svc)])
class TestCacheModule:
    pass
```

## Controller with caching

```python
from lauren import controller, get, delete, Path, module

@controller("/products")
class ProductController:
    def __init__(self, products: ProductService, cache: CacheService) -> None:
        self._products = products
        self._cache = cache

    @get("/{product_id}")
    async def get_product(self, product_id: Path[int]) -> dict:
        key = f"product:{product_id}"
        cached = self._cache.get(key)
        if cached is not None:
            return {**cached, "cache": "hit"}
        data = await self._products.find(product_id)
        self._cache.set(key, data, ttl=60)
        return {**data, "cache": "miss"}

    @delete("/{product_id}/cache")
    async def invalidate(self, product_id: Path[int]) -> dict:
        self._cache.delete(f"product:{product_id}")
        return {"invalidated": True}
```

## Prefix-based invalidation

Use namespaced keys (`entity:id`) so bulk invalidation is straightforward:

```python
# Cache all user-related keys as "user:<id>"
cache.set(f"user:{user_id}", user_data, ttl=300)

# Invalidate everything for a user
cache.invalidate_prefix("user")
```

## TTL strategy

| Data type | Recommended TTL |
|---|---|
| User session | 1800 s (30 min) |
| Product detail | 60 s |
| Catalog listing | 300 s (5 min) |
| Configuration | 3600 s (1 h) |
| Computed aggregate | 900 s (15 min) |

## Common mistakes

- Using `json.loads` on a `None` value — always check `if val is not None`.
- Forgetting `decode_responses=True` — without it, keys and values are bytes.
- Calling `keys("*")` in production on large keyspaces — use `SCAN` instead.
- Caching mutable objects across requests in a `SINGLETON` without serializing
  them — always serialize to JSON so the cached copy is independent.
