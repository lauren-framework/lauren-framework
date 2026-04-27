# Custom Guards

> A **guard** is a small class with a single async method, `can_activate(ctx)`, that returns `True` to allow a request to proceed and `False` (or raises an `HTTPError`) to reject it. Guards are how Lauren models authorization — and they're decoupled from authentication, so you can compose them freely.

## The minimum viable guard

```python
from lauren import ExecutionContext, injectable, Scope

@injectable(scope=Scope.SINGLETON)
class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-role") == "admin"
```

That's the entire surface. Attach it to a route or controller and Lauren runs `can_activate` on every matching request.

## Attaching guards

```python
from lauren import use_guards, controller, get

# Per route — runs only on this handler:
@controller("/x")
class X:
    @get("/admin")
    @injectable(scope=Scope.SINGLETON)
    @use_guards(AdminGuard)
    async def admin_only(self): ...

# Per controller — runs on every handler in the class:
@use_guards(AuthenticatedGuard)
@controller("/private")
class P: ...

# Both — class-level guards run FIRST, then route-level:
@use_guards(AuthenticatedGuard)
@controller("/mixed")
class M:
    @get("/admin")
    @use_guards(AdminGuard)        # runs after AuthenticatedGuard
    async def admin(self): ...
```

Both decoration orders work for class-level guards:

```python
@use_guards(AuthGuard)        # outer
@controller("/x")
class A: ...

@controller("/x")
@use_guards(AuthGuard)        # outer
class B: ...
```

## What `can_activate` receives

`ExecutionContext` exposes:

| Attribute | Type | Purpose |
|---|---|---|
| `ctx.request` | `Request` | Full request — headers, cookies, state, body. |
| `ctx.handler` | callable | The handler method about to be invoked. |
| `ctx.controller_class` | `type \| None` | The controller class, if any. |
| `ctx.get_metadata(key, default)` | `Any` | Read per-route metadata set with `@set_metadata`. |

The `get_metadata` hook is what lets a single guard implement *parametric* policies — read on.

## Guards and DI

Guards are **classes**, and Lauren auto-marks them as injectables. They can take constructor dependencies just like any other service:

```python
from lauren import injectable

@openapi_security({"BearerAuth": []})
@injectable(scope=Scope.SINGLETON)
class TokenGuard:
    def __init__(self, jwt: JwtService, log: Logger) -> None:
        self.jwt = jwt
        self.log = log

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        token = ctx.request.headers.get("authorization", "")
        if not token.startswith("Bearer "):
            return False
        try:
            claims = self.jwt.decode(token[7:])
        except InvalidToken:
            self.log.warn(f"invalid token from {ctx.request.client.host}")
            return False
        ctx.request.state.set("user_id", claims["sub"])
        return True
```

Two important properties:

* The guard is constructed **once per scope** like any other injectable. By default that means once per request (since guards are auto-marked as request-scoped if they have request-scoped deps).
* `ctx.request.state` is fair game — guards routinely **enrich** the request (setting `user_id`, `tenant`, `roles`) so that downstream extractors and handlers can use the parsed values without reparsing.

## Allowing vs denying

There are three ways for a guard to block a request:

```python
@injectable(scope=Scope.SINGLETON)
class G:
    async def can_activate(self, ctx) -> bool:
        # 1. Return False — Lauren raises ForbiddenError(403).
        if not ok: return False

        # 2. Raise UnauthorizedError(401) — typical for missing/invalid auth tokens.
        if not authn: raise UnauthorizedError("invalid token")

        # 3. Raise any HTTPError — full control over status + body.
        if banned: raise HTTPError("account banned", status_code=403,
                                   code="account_banned",
                                   detail={"reason": "TOS violation"})
        return True
```

Choose by intent:

* `return False` — generic 403. Good for "the user just isn't allowed here".
* `raise UnauthorizedError(...)` — 401. Good for missing/invalid credentials.
* `raise <custom HTTPError>` — when the response body needs domain-specific shape.

## Parametric guards with `@set_metadata`

Instead of writing one guard per role / scope / permission, write **one** guard and parametrize it via `@set_metadata`:

```python
from lauren import set_metadata

@injectable(scope=Scope.SINGLETON)
class RoleGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        required = ctx.get_metadata("required_role", "user")
        actual = ctx.request.state.get("user", {}).get("role")
        return actual == required

@controller("/admin")
class AdminController:
    @get("/purge")
    @use_guards(RoleGuard)
    @set_metadata("required_role", "admin")
    async def purge(self): ...

    @get("/super-purge")
    @use_guards(RoleGuard)
    @set_metadata("required_role", "superadmin")
    async def super_purge(self): ...
```

This is the standard pattern in NestJS-influenced codebases and works just as well in Lauren.

A scopes-and-roles policy guard might look like:

```python
@injectable(scope=Scope.SINGLETON)
class PolicyGuard:
    async def can_activate(self, ctx) -> bool:
        scopes = set(ctx.get_metadata("required_scopes", []))
        user_scopes = set(ctx.request.state.get("scopes", []))
        if not scopes.issubset(user_scopes):
            return False
        return True

@get("/billing/export")
@use_guards(PolicyGuard)
@set_metadata("required_scopes", ["billing.read", "exports.create"])
async def export(self): ...
```

## Composing guards

Class-level and route-level guards run in order: **class first, then route**. All guards must pass; if any returns `False` or raises, the chain stops.

```python
@use_guards(AuthenticatedGuard)
@controller("/orders")
class OrdersController:
    @get("/")
    async def list(self): ...                              # AuthenticatedGuard

    @post("/")
    @use_guards(WriteScopeGuard)
    async def create(self): ...                             # AuthenticatedGuard, WriteScopeGuard

    @delete("/{id}")
    @use_guards(WriteScopeGuard, AdminGuard)
    async def delete(self): ...                             # AuthenticatedGuard, WriteScopeGuard, AdminGuard
```

You can also pass multiple guards to a single decorator:

```python
@use_guards(WriteScopeGuard, AdminGuard)
async def h(self): ...
```

## Guards, extractors, and middleware — who does what?

A common confusion: when do I write a guard, when an extractor, when middleware?

| Concern | Use a... |
|---|---|
| "Should this request even run?" — yes/no decision | **Guard** |
| "Decode this domain value from the request" — typed parsing | **Extractor** |
| "Wrap every request with cross-cutting behavior" — tracing, request IDs, response headers | **Middleware** |
| "Translate this error class into a response" | **Exception handler** |

Guards are a *predicate* over the request. Extractors are a *function* on the request. Middleware is an *interceptor* around the entire dispatch. Keep these clean and your codebase reads beautifully.

## Inheritance

Like every other class-level decorator, `@use_guards` attaches to **the exact target only**. A subclass that wants the parent's class-level guards must re-declare them. See [Class Inheritance Rules](../core-concepts/inheritance.md).

```python
@use_guards(AuthGuard)
@controller("/private")
class Parent: ...

@controller("/v2")
class Child(Parent):
    pass
# → Child has parent's handlers but NOT AuthGuard. This is intentional — security
# decisions should never silently inherit. Re-declare:

@use_guards(AuthGuard)
@controller("/v2")
class ChildOK(Parent): ...
```

## Testing guards

Easiest path: drive guards through the `TestClient`.

```python
from lauren.testing import TestClient

def test_admin_required():
    c = TestClient(app)
    r = c.get("/admin/purge")
    assert r.status_code == 403

    r = c.get("/admin/purge", headers={"x-role": "admin"})
    assert r.status_code == 200
```

For unit tests of complex guards, build an `ExecutionContext` manually or mock the request.

## Best practices

* **One guard, one concern.** A guard that does authentication AND authorization AND rate-limiting is hard to compose. Split it.
* **Enrich `request.state`.** Guards routinely parse a token and store decoded claims in `request.state`. Extractors and handlers downstream then consume the parsed values cheaply.
* **Use `@set_metadata` to parametrize.** One `RoleGuard` is better than ten role-specific guards.
* **Order matters.** Authentication guards always run before authorization guards. Place authentication at the controller level; authorization at the route level.
* **Don't build responses inside guards.** Raise an `HTTPError` and let the exception handler / middleware shape the response.

## Errors raised at startup

| Error | Meaning |
|---|---|
| `GuardConfigError` | A guard class is missing `can_activate(ctx)`. |
| `MetadataInheritanceError` | A subclass of a guard was registered without re-decorating. |
| `MissingProviderError` | A guard's `__init__` requires a provider that isn't visible from the controller's module. |

## See also

* [Custom Middleware](custom-middleware.md) — for cross-cutting concerns that aren't yes/no decisions.
* [Custom Extractors](custom-extractors.md) — for typed request parsing.
* [Custom Exception Handlers](custom-exception-handlers.md) — for translating guard-raised errors into responses.
