# Lauren Interceptors & Middlewares — Reference

## Interceptors

Interceptors run **after** guards and wrap the route handler. They receive `ExecutionContext` (route info + metadata) and can transform the result.

```python
from lauren import interceptor
from lauren.types import ExecutionContext, CallHandler, Response

@interceptor()
class TimingInterceptor:
    async def intercept(
        self,
        ctx: ExecutionContext,
        call_handler: CallHandler,
    ) -> Response:
        import time
        t0 = time.perf_counter()
        result = await call_handler.handle()   # always Response
        elapsed = time.perf_counter() - t0
        return result.with_header("x-elapsed-ms", f"{elapsed*1000:.1f}")
```

`@interceptor()` marks the class as `SINGLETON`. For DI deps:

```python
@interceptor()
@injectable(scope=Scope.REQUEST)
class CurrentUserInterceptor:
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    async def intercept(self, ctx: ExecutionContext, call_handler: CallHandler) -> Any:
        ...
        return await call_handler.handle()
```

### Attaching interceptors

```python
# Controller-level
@use_interceptors(TimingInterceptor)
@controller("/api")
class ApiController: ...

# Method-level
@get("/slow")
@use_interceptors(CacheInterceptor)
async def slow(self) -> dict: ...

# Global
app = LaurenFactory.create(AppModule, global_interceptors=[TimingInterceptor])
```

**Execution order (onion model):** global → controller → method. `call_handler.handle()` advances inward. Global interceptors are outermost.

### CallHandler

```python
class CallHandler:
    async def handle(self) -> Response:
        """Advance to the next interceptor or to the route handler.
        Always returns a coerced Response — never a raw dict, tuple, or model."""
```

`handle()` **always returns a `Response`**. The raw handler return value is coerced before interceptors see it, so interceptors can safely read `.status_code`, `.body`, `.headers`, and use `.with_header()` / `.with_status()` / `.with_body()` without any `isinstance` check.

---

## Middlewares

Middlewares run **before routing** (global) or **per-route** (controller/method). They receive `Request` (not `ExecutionContext`).

```python
from lauren import middleware
from lauren.types import Request, Response, CallNext

@middleware()
class LoggingMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        print(f"→ {request.method} {request.path}")
        response = await call_next(request)
        print(f"← {response.status_code}")
        return response
```

`@middleware()` marks the class as `SINGLETON` automatically. Must be invoked with parentheses.

### Attaching middlewares

```python
# Controller-level (per-route)
@use_middlewares(AuthMiddleware)
@controller("/api")
class ApiController: ...

# Method-level
@get("/")
@use_middlewares(RateLimitMiddleware)
async def index(self) -> dict: ...

# Global (runs before routing — handles OPTIONS preflight)
app = LaurenFactory.create(AppModule, global_middlewares=[CorsMiddleware, LoggingMiddleware])
```

**Key difference from interceptors:**

| | Middleware | Interceptor |
|---|---|---|
| Receives | `Request` | `ExecutionContext` (route info + metadata) |
| Runs | Before routing (global) or at dispatch | After guards |
| `handle()` / `call_next()` returns | `Response` | `Response` (always coerced) |
| Onion position | Outermost | Inside guards, outside handler |

### Request / Response API in middleware

```python
# Reading from request
request.method          # "GET", "POST", …
request.path            # "/users/42"
request.headers.get("authorization")
request.state.get("user_id")    # mutable per-request store
request.state["user_id"] = uid  # set for downstream handlers

# Building a response
return Response.json({"error": "forbidden"}, status=403)
return Response.text("ok")
return Response(body=b"...", status=200, headers=[("content-type", "text/plain")])
```
