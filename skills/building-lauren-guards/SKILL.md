---
name: building-lauren-guards
description: Writes Lauren guards, interceptors, and middlewares. Covers @guard, can_activate, ExecutionContext, @interceptor, intercept/CallHandler, @middleware() (parentheses required), dispatch/call_next, use_guards, use_interceptors, use_middlewares, and set_metadata. Use when protecting routes, transforming responses, or adding cross-cutting logic to a Lauren app.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Lauren Guards, Interceptors & Middlewares

## Guards — route authorization

Guards decide whether a request may proceed. Return `True` to allow, `False` to deny (403), or raise an exception.

A guard is any class that defines `can_activate(self, ctx: ExecutionContext) -> bool`. No special decorator is required:

```python
from lauren import injectable
from lauren.types import ExecutionContext, Scope

class AuthGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        token = ctx.request.headers.get("authorization", "")
        if not token.startswith("Bearer "):
            return False
        # validate token ...
        return True
```

For DI-injected guards, mark them `@injectable`:

```python
@injectable(scope=Scope.SINGLETON)
class JwtGuard:
    def __init__(self, jwt_service: JwtService) -> None:
        self._jwt = jwt_service

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        token = ctx.request.headers.get("authorization", "")[7:]
        return await self._jwt.verify(token)
```

List the guard in your module's `providers=` when it uses DI.

### Attaching guards

```python
# Controller-level — applies to all routes in the class
@use_guards(AuthGuard)
@controller("/admin")
class AdminController: ...

# Method-level — applies only to this route
@get("/secret")
@use_guards(AuthGuard, RoleGuard)
async def secret(self) -> dict: ...

# Global — applies to every route in the app
app = LaurenFactory.create(AppModule, global_guards=[AuthGuard])
```

Guards run in order: global → controller → method. Any guard returning `False` short-circuits to 403.

### ExecutionContext

```python
class ExecutionContext:
    request: Request              # raw request object
    handler_class: type | None    # the controller class
    handler_func: Callable | None # the handler method
    route_template: str | None    # e.g. "/users/{id}"
    metadata: dict[str, Any]      # set_metadata values

    def get_metadata(self, key: str, default=None) -> Any: ...
```

### Metadata — mark routes from a guard

```python
from lauren import set_metadata

IS_PUBLIC = "app.is_public"

@set_metadata(IS_PUBLIC, True)
@get("/health")
async def health(self) -> dict:
    return {"ok": True}

# In the guard:
async def can_activate(self, ctx: ExecutionContext) -> bool:
    if ctx.get_metadata(IS_PUBLIC):
        return True
    ...
```

## Exception handlers — typed error mapping

Exception handlers catch typed exceptions and return structured HTTP responses. They can be class-based (with DI) or function-based.

### Class-form handler

```python
from lauren import exception_handler
from lauren.types import Request, Response

@exception_handler(NotFoundError, ConflictError)
class DomainErrors:
    def __init__(self, log: Logger) -> None:
        self.log = log

    async def catch(self, exc: Exception, request: Request) -> Response:
        self.log.warn(f"domain error: {exc}")
        return Response.json({"error": str(exc)}, status=400)
```

### Function-form handler

```python
@exception_handler(ValueError)
async def handle_value_error(exc: ValueError, request: Request) -> Response:
    return Response.json({"detail": str(exc)}, status=422)
```

### Attaching exception handlers

```python
# Controller-level
@use_exception_handlers(DomainErrors)
@controller("/api")
class ApiController: ...

# Method-level
@get("/item")
@use_exception_handlers(DomainErrors)
async def get_item(self) -> dict: ...

# Global
app = LaurenFactory.create(AppModule, global_exception_handlers=[DomainErrors])
```

Resolution order: route → controller → global. First matching handler wins.

See [interceptors-middlewares.md](interceptors-middlewares.md) for interceptors and middlewares.

---

## Guards on WebSocket gateways

`@use_guards` works on `@ws_controller` classes. Guards run before `@on_connect`,
before the WebSocket handshake is accepted.

```python
from lauren import (
    injectable, Scope, use_guards,
    ws_controller, on_connect, WebSocket,
    WsConnectionContext,
)

@injectable(scope=Scope.SINGLETON)
class WsAuthGuard:
    async def can_activate(self, ctx: WsConnectionContext) -> bool:
        # ctx.request mirrors HTTP Request: .headers, .path, .path_params, .method
        # ctx.connection  — the live WebSocket
        # ctx.handler_class, ctx.route_template, ctx.get_metadata(key)
        token = ctx.request.headers.get("x-token", "")
        return token == "valid"

@use_guards(WsAuthGuard)
@ws_controller("/protected")
class ProtectedGateway:
    @on_connect
    async def on_open(self, ws: WebSocket) -> None:
        # Only called when all guards returned True.
        await ws.send_json({"event": "connected"})
```

A **rejected** connection gets close code `1008` (policy violation). The guard
runs before `ws.accept()` — the client never completes the handshake.

### Sharing one guard across HTTP and WebSocket

`WsConnectionContext.request` duck-types with the HTTP `Request` object on the
fields guards most commonly read (headers, path, path_params, method). A guard
that only touches those fields works unchanged on both transport types:

```python
@injectable(scope=Scope.SINGLETON)
class ApiKeyGuard:
    async def can_activate(self, ctx) -> bool:
        # Works for @controller (ctx is ExecutionContext)
        # AND @ws_controller (ctx is WsConnectionContext) — same code.
        return ctx.request.headers.get("x-api-key") == "secret"
```

### Global WebSocket guards

```python
app = LaurenFactory.create(AppModule, global_ws_guards=[WsAuthGuard])
# global guards run first, then class-level guards
```

### Reading guard metadata from a gateway class

```python
from lauren.reflect import reflect_guards, reflect_all

guards = reflect_guards(MyGateway)        # tuple[type, ...] — own __dict__ only
meta = reflect_all(MyGateway)             # ReflectedMeta(guards, interceptors, middlewares)
```
