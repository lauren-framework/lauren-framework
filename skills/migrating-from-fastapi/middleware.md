# Middleware & Error Handling — FastAPI vs Lauren

## Middleware

**FastAPI (function-style):**
```python
from starlette.middleware.base import BaseHTTPMiddleware

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        print(f"→ {request.method} {request.url}")
        response = await call_next(request)
        print(f"← {response.status_code}")
        return response

app.add_middleware(LoggingMiddleware)
```

**Lauren:**
```python
from lauren import injectable, middleware, Scope
from lauren.types import Request, Response

@middleware()
@injectable(scope=Scope.SINGLETON)
class LoggingMiddleware:
    async def dispatch(self, request: Request, call_next) -> Response:
        print(f"→ {request.method} {request.url.path}")
        response = await call_next(request)
        print(f"← {response.status_code}")
        return response
```

Register globally: `Lauren(AppModule, global_middlewares=[LoggingMiddleware])`
Or per-module/controller: `@module(middlewares=[LoggingMiddleware])`

## Interceptors (no FastAPI equivalent)

Lauren interceptors wrap handler *execution* (not raw bytes) — useful for timing, caching, response transforms:

```python
from lauren import injectable, interceptor, Scope
from lauren.types import ExecutionContext

@interceptor()
@injectable(scope=Scope.SINGLETON)
class TimingInterceptor:
    async def intercept(self, ctx: ExecutionContext, call_handler) -> Response:
        start = time.monotonic()
        response = await call_handler.handle()
        response.headers["X-Response-Time"] = f"{(time.monotonic() - start) * 1000:.1f}ms"
        return response
```

Apply with `@use_interceptors(TimingInterceptor)` on a controller or route.

## Exception handlers

**FastAPI:**
```python
from fastapi import Request
from fastapi.responses import JSONResponse

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
```

**Lauren:**
```python
from lauren import exception_handler
from lauren.types import Request, Response

@exception_handler()
class ValueErrorHandler:
    handles = ValueError

    async def catch(self, exc: ValueError, request: Request) -> Response:
        from lauren.types import Response as R
        return R.json({"detail": str(exc)}, status_code=400)
```

Register in module: `@module(exception_handlers=[ValueErrorHandler])`

## CORS

**FastAPI:**
```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])
```

**Lauren (via `lauren-middlewares`):**
```python
from lauren_middlewares import CorsMiddleware

app = Lauren(AppModule, global_middlewares=[
    CorsMiddleware(allow_origins=["*"], allow_methods=["*"])
])
```
