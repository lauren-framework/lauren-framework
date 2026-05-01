---
name: building-lauren-guards
description: Writes Lauren guards, interceptors, and middlewares. Covers @guard, can_activate, ExecutionContext, @interceptor, intercept/CallHandler, @middleware() (parentheses required), dispatch/call_next, use_guards, use_interceptors, use_middlewares, and set_metadata. Use when protecting routes, transforming responses, or adding cross-cutting logic to a Lauren app.
---

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

See [interceptors-middlewares.md](interceptors-middlewares.md) for interceptors and middlewares.
