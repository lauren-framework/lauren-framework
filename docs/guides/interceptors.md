# Interceptors

> An **interceptor** sits **between guards and the route handler**: it runs after the route is matched and authenticated, but before (and after) the handler itself. Unlike middleware — which only sees the raw request before routing — interceptors receive a full `ExecutionContext` with the matched controller class, handler function, route template, and all route metadata. This makes them the right tool for response transformation, logging with handler context, caching, and AOP-style cross-cutting concerns.

## The minimum viable interceptor

```python
from typing import Any
from lauren import interceptor
from lauren.types import CallHandler, ExecutionContext

@interceptor()
class LoggingInterceptor:
    async def intercept(self, ctx: ExecutionContext, call_handler: CallHandler) -> Any:
        print(f"→ {ctx.handler_class.__name__}.{ctx.handler_func.__name__}")
        result = await call_handler.handle()
        print(f"← {ctx.route_template}")
        return result
```

The contract:

* Receive an `ExecutionContext` and a `CallHandler`.
* Call `await call_handler.handle()` to invoke the rest of the pipeline (inner interceptors → handler).
* Optionally transform, replace, or suppress the result.
* Return the final result (any type the serialiser can handle, or a `Response`).

## Attaching interceptors

Interceptors follow the same three-placement model as guards and middleware:

```python
from lauren import use_interceptors, controller, get

# ── Per route ─────────────────────────────────────────────────────────
@controller("/items")
class ItemsController:
    @use_interceptors(LoggingInterceptor)
    @get("/{id}")
    async def get_item(self, id: int) -> dict: ...

# ── Per controller — runs on every handler in the class ───────────────
@use_interceptors(LoggingInterceptor)
@controller("/items")
class ItemsController:
    @get("/{id}")
    async def get_item(self, id: int) -> dict: ...

# ── Global — runs on every route in the application ───────────────────
app = LaurenFactory.create(AppModule, global_interceptors=[LoggingInterceptor])
```

Multiple interceptors are listed in declaration order. The **first listed** is the **outermost layer** — exactly the onion model used by middleware.

## Execution order

The full pipeline for a single request is:

```
Middleware (outermost → innermost)
    Guards (class-level, then route-level)
        Global interceptors (outermost → innermost)
            Controller-level interceptors (outermost → innermost)
                Route-level interceptors (outermost → innermost)
                    Handler
```

Guards run **before** interceptors. If a guard returns `False` (or raises), the interceptor chain is never entered.

```python
@interceptor()
class Tracer:
    async def intercept(self, ctx, ch: CallHandler) -> Any:
        print("pre")        # ← before handler
        result = await ch.handle()
        print("post")       # ← after handler
        return result
```

With three interceptors `[A, B, C]` the log is:

```
A-pre → B-pre → C-pre → handler → C-post → B-post → A-post
```

## Interceptors vs middleware

| | Middleware | Interceptor |
|---|---|---|
| When it runs | Before routing | After routing |
| Context available | `Request` only | `ExecutionContext` (class, func, template, metadata) |
| Receives | `Request` + `CallNext` | `ExecutionContext` + `CallHandler` |
| Right tool for | CORS, auth headers, body parsing | Response transforms, caching, route-aware logging |

## Reading route metadata

Interceptors can read metadata set with `@set_metadata`:

```python
from lauren import set_metadata, use_interceptors, controller, get
from lauren.types import CallHandler, ExecutionContext

@interceptor()
class CacheInterceptor:
    _cache: dict = {}

    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        ttl = ctx.get_metadata("cache_ttl")
        key = ctx.route_template
        if ttl and key in self._cache:
            return self._cache[key]
        result = await ch.handle()
        if ttl:
            self._cache[key] = result
        return result

@use_interceptors(CacheInterceptor)
@controller("/products")
class ProductsController:
    @set_metadata("cache_ttl", 60)
    @get("/")
    async def list_products(self) -> list[dict]:
        return [...]
```

`ctx.get_metadata(key, default=None)` merges controller-level and route-level metadata, with route-level taking precedence.

## Short-circuiting (cache hit, early return)

An interceptor can return a value **without calling `ch.handle()`** to bypass the handler entirely:

```python
@interceptor()
class CacheHit:
    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        cached = my_cache.get(ctx.route_template)
        if cached is not None:
            return cached          # handler is never called
        result = await ch.handle()
        my_cache.set(ctx.route_template, result)
        return result
```

## Error handling / transformation

Interceptors can catch exceptions thrown by the handler and convert them into successful responses:

```python
from lauren.exceptions import HTTPError

@interceptor()
class FallbackInterceptor:
    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        try:
            return await ch.handle()
        except HTTPError:
            return {"error": "something went wrong", "fallback": True}
```

## Response header injection

To inject response headers from an interceptor, the handler must return a `Response` object. If the handler returns a dict (or Pydantic model), Lauren serialises it **after** the interceptor chain, so the interceptor only sees the raw Python value:

```python
from lauren.types import Response

@interceptor()
class TimingInterceptor:
    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        import time
        t0 = time.monotonic()
        result = await ch.handle()
        elapsed = time.monotonic() - t0
        if isinstance(result, Response):
            return result.with_header("x-duration-ms", str(int(elapsed * 1000)))
        return result

# For the header to be set, the handler must return a Response directly:
@controller("/c")
class C:
    @use_interceptors(TimingInterceptor)
    @get("/slow")
    async def slow(self) -> Any:
        import asyncio; await asyncio.sleep(0.1)
        return Response.json({"ok": True})  # ← Response, not dict
```

## Dependency injection

`@interceptor()` automatically registers the class as a **singleton** in the DI container. To inject dependencies, combine it with `@injectable`:

```python
from lauren import injectable, interceptor, Scope
from lauren.types import CallHandler, ExecutionContext

@injectable()
class MetricsService:
    def record(self, route: str, duration_ms: float) -> None: ...

@interceptor()
@injectable()          # inherits SINGLETON scope by default
class MetricsInterceptor:
    def __init__(self, metrics: MetricsService) -> None:
        self._metrics = metrics

    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        import time
        t0 = time.monotonic()
        result = await ch.handle()
        self._metrics.record(ctx.route_template, (time.monotonic() - t0) * 1000)
        return result
```

For a **request-scoped** interceptor (fresh instance per request), use `@injectable(scope=Scope.REQUEST)` and register it explicitly as a provider:

```python
from lauren import Scope

@interceptor()
@injectable(scope=Scope.REQUEST)
class RequestScopedInterceptor:
    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        ...

@module(controllers=[MyController], providers=[RequestScopedInterceptor])
class AppModule: ...
```

## `@interceptor()` — what it does

`@interceptor()` is a lightweight marker decorator. It:

1. Checks that the class defines an `intercept` method (raises `InterceptorConfigError` otherwise).
2. Sets a `__lauren_interceptor__` marker attribute on the class.
3. Auto-registers the class as a `SINGLETON` injectable **if** it has no existing `@injectable` annotation.

It does **not** add the interceptor to any route. Use `@use_interceptors()` or `global_interceptors=` for that.

```python
from lauren import interceptor
from lauren.exceptions import InterceptorConfigError

# OK — has an intercept method:
@interceptor()
class Good:
    async def intercept(self, ctx, ch): ...

# Error at decoration time — no intercept method:
@interceptor()          # ← raises InterceptorConfigError
class Bad:
    pass
```

## `@use_interceptors()` — attaching interceptors

```python
from lauren import use_interceptors

# Class-level:
@use_interceptors(InterceptorA, InterceptorB)
@controller("/x")
class X: ...

# Method-level (applied after the HTTP verb decorator):
@controller("/x")
class X:
    @use_interceptors(InterceptorA)
    @get("/y")
    async def y(self): ...

# Multiple @use_interceptors calls append (do not replace):
@use_interceptors(InterceptorB)
@use_interceptors(InterceptorA)   # A is outermost (applied last)
@controller("/x")
class X: ...
# effective order: [A, B]

# None values are silently dropped — useful for conditional wiring:
debug_interceptor = DebugInterceptor if DEBUG else None
@use_interceptors(debug_interceptor)
@controller("/x")
class X: ...
```

### Subclass isolation

`@use_interceptors` on a class is **not inherited** by subclasses. Each subclass that wants interception must declare it explicitly. This prevents silent coupling through inheritance.

## Full example: per-route audit log

```python
from typing import Any
from lauren import (
    LaurenFactory, controller, get, injectable, interceptor, module, set_metadata, use_interceptors,
)
from lauren.types import CallHandler, ExecutionContext

@injectable()
class AuditLog:
    def __init__(self) -> None:
        self.records: list[str] = []

    def log(self, msg: str) -> None:
        self.records.append(msg)

@interceptor()
@injectable()
class AuditInterceptor:
    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit

    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        action = ctx.get_metadata("audit_action", "unknown")
        result = await ch.handle()
        self._audit.log(f"{action} on {ctx.route_template}")
        return result

@use_interceptors(AuditInterceptor)
@controller("/orders")
class OrdersController:
    @set_metadata("audit_action", "create-order")
    @get("/")
    async def list_orders(self) -> list[dict]:
        return []

    @set_metadata("audit_action", "get-order")
    @get("/{id}")
    async def get_order(self, id: int) -> dict:
        return {"id": id}

@module(
    controllers=[OrdersController],
    providers=[AuditLog, AuditInterceptor],
)
class AppModule: ...

app = LaurenFactory.create(AppModule)
```
