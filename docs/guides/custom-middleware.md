# Custom Middleware

> Middleware in Lauren follows the **onion model**: each layer wraps the next, sees the request on the way in, sees the response on the way out, and decides what to do with both. Middleware is the right tool for **cross-cutting concerns** — request IDs, logging, timing, security headers, error normalization.

## The minimum viable middleware

```python
from lauren import middleware

@middleware
class RequestId:
    async def dispatch(self, request, call_next):
        import uuid
        request.state.rid = uuid.uuid4().hex
        response = await call_next(request)
        return response.with_header("x-request-id", request.state.rid)
```

The `@middleware` decorator marks the class as middleware (it must implement `async dispatch(request, call_next)`). The contract is precisely:

* Receive the `request` and a `call_next(request) -> Response` callable.
* Do whatever you want before calling `call_next`.
* Call `call_next(request)` (or skip it to short-circuit).
* Do whatever you want with the returned `Response`.
* Return a `Response`.

Lauren's `Response` is **immutable** — every `with_*` method returns a new instance, so you never mutate state shared across layers.

## Three places to attach middleware

```python
# 1. Global — wraps every request, outermost layer first:
app = await LaurenFactory.create(
    AppModule,
    global_middleware=[RequestId, Timing, AuthMiddleware],
)

# 2. Controller — wraps every handler on the class:
@use_middleware(TenantScope)
@controller("/api")
class ApiController: ...

# 3. Route — wraps a single handler:
@get("/expensive")
@use_middleware(CacheControl)
async def slow(self): ...
```

Stacking order, outermost first:

```
global → controller → route → handler
                       ↑
                   peeled off in reverse on the way out
```

So a request hits global middleware first, then controller middleware, then route middleware, then the handler — and the response unwinds in the reverse order. This is the same "onion" you'd find in Express, Koa, or Axum.

## Middleware is DI-injected

Middleware classes are auto-marked as injectables. They can take constructor dependencies just like any other service:

```python
from lauren.logging import Logger

@middleware
class AccessLog:
    def __init__(self, log: Logger) -> None:
        self.log = log

    async def dispatch(self, request, call_next):
        import time
        t0 = time.monotonic()
        response = await call_next(request)
        dt = (time.monotonic() - t0) * 1000
        self.log.log(
            level="info",
            context="AccessLog",
            message=f"{request.method} {request.path} {response.status} {dt:.1f}ms",
        )
        return response
```

The middleware lifetime depends on what it depends on — pure singletons stay singleton; if you take request-scoped deps, the middleware itself becomes request-scoped.

## Common patterns

### Request ID propagation

```python
@middleware
class RequestId:
    async def dispatch(self, request, call_next):
        import uuid
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.rid = rid
        response = await call_next(request)
        return response.with_header("x-request-id", rid)
```

Now any log line, any downstream service call, any error report can include `request.state.rid` and you can correlate across systems.

### Timing / metrics

```python
@middleware
class Timing:
    def __init__(self, metrics: MetricsClient) -> None:
        self.metrics = metrics

    async def dispatch(self, request, call_next):
        import time
        t0 = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            dt = (time.monotonic() - t0) * 1000
            self.metrics.timing("http.req", dt, tags=[
                f"method:{request.method}",
                f"path:{request.get_route_template() or 'unknown'}",
            ])
        return response
```

Note `request.get_route_template()` — using the templated path (`/users/{id}`) instead of the concrete one means your metric cardinality stays bounded.

### Security headers

```python
@middleware
class SecurityHeaders:
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        return response.with_headers({
            "strict-transport-security": "max-age=31536000",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
        })
```

### CORS

```python
@middleware
class CORS:
    def __init__(self, allowed: list[str]) -> None:
        self.allowed = allowed

    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin", "")
        if request.method == "OPTIONS":
            # Preflight short-circuit
            return Response.empty(204).with_headers(self._cors_headers(origin))
        response = await call_next(request)
        return response.with_headers(self._cors_headers(origin))

    def _cors_headers(self, origin: str) -> dict:
        if origin in self.allowed:
            return {
                "access-control-allow-origin": origin,
                "access-control-allow-credentials": "true",
                "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
                "access-control-allow-headers": "*",
            }
        return {}
```

### Short-circuiting (without calling `call_next`)

Middleware doesn't *have* to invoke `call_next`. Skip it to short-circuit:

```python
@middleware
class Maintenance:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def dispatch(self, request, call_next):
        if self.cfg.maintenance_mode:
            return Response.json(
                {"error": {"code": "maintenance", "message": "down for maintenance"}},
                status=503,
            )
        return await call_next(request)
```

### Error normalization

Catch unhandled exceptions and turn them into standard envelopes (this is what Lauren's built-in error pipeline does — the example below is mostly for illustration; in practice prefer [exception handlers](custom-exception-handlers.md) for domain errors):

```python
@middleware
class CrashGuard:
    def __init__(self, log: Logger) -> None:
        self.log = log

    async def dispatch(self, request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            self.log.error(f"unhandled {type(exc).__name__}: {exc}")
            return Response.json(
                {"error": {"code": "internal", "message": "server error"}},
                status=500,
            )
```

## Middleware vs guards vs exception handlers

A frequent question: which abstraction do I use?

| Concern | Use a... |
|---|---|
| Authorize this specific request — yes/no | [Guard](custom-guards.md) |
| Translate one specific exception type into a response | [Exception handler](custom-exception-handlers.md) |
| Wrap every request with cross-cutting behavior (timing, IDs, headers) | **Middleware** |
| Decode a domain value from the request | [Extractor](custom-extractors.md) |

Middleware is the *most general* — it can do everything the others can — but each specialised abstraction is sharper for its job. Reach for middleware when:

* The behavior is **truly cross-cutting** (every request, regardless of route).
* You need to *both* see the request *and* decorate the response.
* The behavior is more about *transport* than *domain*.

## Streaming responses

Middleware works the same way with streaming responses (`Response.stream`, `Response.sse`, `EventStream`). Just remember: the `Response` you receive from `call_next` may not have buffered its body yet. Don't read or rewrite the body content of streaming responses. Adding/removing headers is fine.

```python
@middleware
class StreamSafe:
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Adding headers — fine.
        return response.with_header("x-trace-id", request.state.rid)
        # response.with_body(...)  ← would consume an iterator. Don't.
```

## Inheritance

`@use_middleware` attaches to the exact target. A subclass doesn't inherit the parent's class-level middleware. See [Class Inheritance Rules](../core-concepts/inheritance.md). To compose the same middleware across many controllers, prefer **global middleware** in `LaurenFactory.create(...)`.

## Testing middleware

The `TestClient` is the easiest harness. For unit tests, you can also call middleware directly:

```python
import asyncio
from lauren.types import Request, Response
from lauren.testing import build_test_request   # if you expose one

async def test_request_id_added():
    mw = RequestId()
    async def fake_next(req):
        return Response.json({"ok": True})
    response = await mw.dispatch(build_test_request("/x"), fake_next)
    assert "x-request-id" in response.headers
```

## Errors raised at startup

| Error | Meaning |
|---|---|
| `MiddlewareConfigError` | A middleware class is missing `dispatch(request, call_next)`. |
| `MetadataInheritanceError` | A subclass was registered as middleware without re-decorating. |

## Best practices

* **Keep middleware boring.** No domain logic. No business decisions. If you find yourself reaching for repository deps inside middleware, you probably want a guard or an exception handler.
* **Always call `call_next` once** (or zero times for a short-circuit). Calling it twice is a logical bug Lauren doesn't try to catch.
* **Mind the order of global middleware.** The first entry wraps everything beneath it. Put the most "outer" concerns (request IDs, panic-catching) first.
* **Use the templated route, not the concrete path, in metrics.** `request.get_route_template()` keeps cardinality finite.

## See also

* [Custom Guards](custom-guards.md) — for the request-blocking decisions middleware shouldn't make.
* [Custom Exception Handlers](custom-exception-handlers.md) — for typed-error → response mapping.
* [Core Concepts → Request & Response](../core-concepts/request-response.md) — the immutable `Response` API your middleware will operate on.
