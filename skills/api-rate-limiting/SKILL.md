---
name: api-rate-limiting
description: Implements per-client API rate limiting in a Lauren application using a token-bucket algorithm and a guard. Use when protecting endpoints from abuse, enforcing quotas, or adding fair-use policies.
---

> Use `codemap find "use_guards"` to locate existing guard registrations before adding a new limiter.

# API Rate Limiting (Token Bucket)

The pattern uses two injectable singletons:

1. **`TokenBucketLimiter`** — stateful bucket store; one bucket per client key (e.g., IP address).
2. **`RateLimitGuard`** — a guard that calls `consume()` and raises HTTP 429 on exhaustion.

## Token bucket implementation

```python
from __future__ import annotations
import time
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class TokenBucketLimiter:
    """Token-bucket rate limiter — thread-safe for single-process deployments."""

    def __init__(self, capacity: int = 60, refill_rate: float = 1.0) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate  # tokens per second
        self._buckets: dict[str, dict] = {}

    def _get_bucket(self, key: str) -> dict:
        if key not in self._buckets:
            self._buckets[key] = {
                "tokens": float(self._capacity),
                "last_refill": time.monotonic(),
            }
        return self._buckets[key]

    def consume(self, key: str, tokens: int = 1) -> bool:
        bucket = self._get_bucket(key)
        now = time.monotonic()
        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = min(
            float(self._capacity),
            bucket["tokens"] + elapsed * self._refill_rate,
        )
        bucket["last_refill"] = now
        if bucket["tokens"] >= tokens:
            bucket["tokens"] -= tokens
            return True
        return False
```

## Rate limit guard

```python
from lauren import injectable, Scope
from lauren.types import ExecutionContext
from lauren.exceptions import HTTPError

class RateLimitError(HTTPError):
    status_code = 429
    code = "rate_limit_exceeded"

@injectable(scope=Scope.SINGLETON)
class RateLimitGuard:
    def __init__(self, limiter: TokenBucketLimiter) -> None:
        self._limiter = limiter

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        client_ip = ctx.request.headers.get("x-forwarded-for", "127.0.0.1")
        if not self._limiter.consume(client_ip):
            raise RateLimitError("Rate limit exceeded")
        return True
```

## Controller with guard

```python
from lauren import controller, get, module, use_guards

@use_guards(RateLimitGuard)
@controller("/api")
class RateLimitedController:
    @get("/data")
    async def data(self) -> dict:
        return {"data": "ok"}

@module(
    controllers=[RateLimitedController],
    providers=[RateLimitGuard, TokenBucketLimiter],
)
class RateLimitModule:
    pass
```

## Customising capacity per route

Apply `@use_guards` at the method level to use a different guard instance, or use `set_metadata` to pass the limit:

```python
from lauren import set_metadata, use_guards

@controller("/api")
class MyController:
    @get("/cheap")
    @use_guards(RateLimitGuard)
    async def cheap(self) -> dict:
        return {"ok": True}

    @get("/expensive")
    @use_guards(RateLimitGuard)
    @set_metadata("rate_limit_cost", 5)
    async def expensive(self) -> dict:
        return {"ok": True}
```

Then read `ctx.get_metadata("rate_limit_cost", 1)` inside `can_activate`.

## Key points

- `TokenBucketLimiter` is a singleton so its state persists across requests within a process. For multi-worker setups, back the bucket store with Redis.
- `RateLimitError` subclasses `HTTPError` with `status_code = 429` — the ASGI adapter converts it to a JSON 429 response automatically.
- Use `x-forwarded-for` for IP extraction when behind a reverse proxy; validate trust level in production.
- Applying `@use_guards` on the controller class applies the guard to all routes on that controller.
