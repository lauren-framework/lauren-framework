# Typed Metadata, State Keys & Decorator Composition

> Guards, middleware and handlers pass information through two channels:
> **route metadata** (policy declared on a controller or route — "this needs the
> `admin` role") and **request state** (data produced while serving a request —
> "the caller is Alice"). Both accept plain strings, and both have typed
> handles: `MetadataKey` / `MetadataSetKey` for metadata, `StateKey` for state.
> `compose(...)` turns a stack of decorators into one reusable decorator.

## Why typed keys

With string keys, the writer and the reader agree on a name and a value shape
by convention only:

<!-- docs-contract: fragment -->
```python
@set_metadata("roles", ["admin"])             # writer
...
required = ctx.get_metadata("rolse", [])      # reader: a typo silently yields []
```

A typed key is declared once and used on both sides, so a typo is a
`NameError`, the read has a static type, and the factory knows how to merge
the key across levels:

<!-- docs-contract: run -->
```python
from lauren import MetadataKey, MetadataSetKey

Roles = MetadataSetKey[str]("app.roles")                                   # collection
IsPublic = MetadataKey[bool]("app.is_public", default=False)               # scalar
RateLimit = MetadataKey[int | None]("app.rate_limit", default=None)        # scalar, absence meaningful
```

The key's *name* is what is stored — `ctx.get_metadata("app.roles")` still
works, and so does every existing string reader. Namespace names the way
packages do (`"lauren-guards.is_public"`) to avoid collisions.

## Metadata keys

A key is a decorator: `@Roles("admin")` adds items, `@IsPublic(True)` sets a
value. It works on controllers, route methods, WebSocket gateways and
`@on_message` handlers, above or below the other decorators.

<!-- docs-contract: run -->
```python
from lauren import ExecutionContext, controller, get, use_guards


class RolesGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        if ctx.get_metadata(IsPublic):             # bool
            return True
        required = ctx.get_metadata(Roles)         # tuple[str, ...]
        granted = set(ctx.request.headers.get("x-roles", "").split(","))
        return set(required) <= granted


@Roles("admin")
@use_guards(RolesGuard)
@controller("/admin")
class AdminController:
    @get("/audit")
    @Roles("auditor")                              # effective: ("admin", "auditor")
    async def audit(self) -> dict:
        return {"ok": True}

    @get("/health")
    @IsPublic(True)
    async def health(self) -> dict:
        return {"ok": True}
```

`@set_metadata(Roles, ("admin",))` is the same as `@Roles("admin")`, and
`@set_metadata(IsPublic, True)` the same as `@IsPublic(True)`.

### Stacking on one target

| Key | Stacked twice on the same class or function |
|---|---|
| `MetadataSetKey` | items accumulate, de-duplicated, in application order (bottom-up, like `@use_guards`) |
| `MetadataKey` | the same value twice is fine; two **different** values raise `DecoratorUsageError` at import time — never a silent overwrite |

### Merging across levels

The factory merges the controller's metadata (outer) with the route's (inner)
**once, at startup**; each request's `ExecutionContext.metadata` is that
read-only mapping (writing to it raises `TypeError`). The `merge=` policy of
each key decides:

| Key | `merge=` | Result |
|---|---|---|
| `MetadataSetKey` | `"union"` (default) | ordered union, controller items first |
| `MetadataSetKey` | `"override"` | the route's items replace the controller's |
| `MetadataKey` | `"override"` (default) | most specific level wins |
| `MetadataKey` | `"outer"` | the controller's value wins — pin a security setting a route cannot weaken |
| `MetadataKey` | `"error"` | different values on two levels fail `LaurenFactory.create` |
| either | a callable `(outer, inner) -> value` | your rule (e.g. `merge=min` for a rate limit) |

String keys keep their historical rule: the route's value replaces the
controller's. A level that writes a typed key's *name* with a plain string
follows the key's policy.

The same applies to WebSocket gateways: a gateway's metadata is what
`WsConnectionContext.get_metadata` reads at connect time, and an `@on_message`
handler's metadata merges with it for `WsMessageContext`.

### Validation and safety nets

- **`default` is required** on `MetadataKey`, so reads are total. Declare
  `MetadataKey[int | None](..., default=None)` when absence matters.
- **`validate=True`** checks each decorated value against `T` at decoration
  time: `MetadataKey[int]("app.limit", default=60, validate=True)` rejects
  `@Limit("60")` (and `True`) with `DecoratorUsageError`.
- **Bare usage** (`@IsPublic` without a value) is rejected. A key whose values
  are classes or functions must say so: `MetadataKey[type[Serializer]](...)`.
- **Name collisions**: two different key objects with the same name meeting on
  one target or in one route raise `DecoratorUsageError`, naming the
  `module:line` where each was declared. There is no global registry — two apps
  in one process never interfere.
- **`public=True`** includes that key's value in `describe_app(app)` manifests.
  Other metadata values remain hidden unless `include_metadata_values=True` is
  requested. `Secret` wrappers remain redacted even on public keys.

### Inheritance

Metadata never inherits through subclassing: `@set_metadata` and typed keys
read and extend a class's **own** metadata, exactly like `@use_guards`. Copy a
parent's values deliberately with [`@propagate_metadata`](propagate-metadata.md)
— the target's scalar values win and `MetadataSetKey` items are unioned.

### Reading metadata outside a request

`lauren.reflect.reflect_user_metadata(obj, Roles)` returns what one class or
function declares (the key's default when absent); with a string key or no key
it behaves as before.

## State keys

`StateKey[T]` names a typed slot in `request.state` (and in `app_state` and a
WebSocket's `ws.state`):

<!-- docs-contract: run -->
```python
from dataclasses import dataclass
from typing import Annotated

from lauren import StateKey, UnauthorizedError


@dataclass(frozen=True)
class AuthUser:
    id: str
    roles: tuple[str, ...] = ()


CurrentUser = StateKey[AuthUser]("app.user", on_missing=UnauthorizedError)
User = Annotated[AuthUser, CurrentUser]             # reusable parameter alias


class JwtGuard:
    deny_error = UnauthorizedError

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        token = ctx.request.headers.get("authorization", "")
        if token != "Bearer good":
            return False
        ctx.request.state[CurrentUser] = AuthUser(id="u-1", roles=("admin",))
        return True


@use_guards(JwtGuard)
@controller("/me")
class MeController:
    @get("/")
    async def me(self, user: User) -> dict:                            # AuthUser
        return {"id": user.id}

    @get("/optional")
    async def maybe(self, user: Annotated[AuthUser | None, CurrentUser]) -> dict:
        return {"anonymous": user is None}
```

| Operation | Meaning |
|---|---|
| `request.state[CurrentUser] = user` | stores under `"app.user"` — `request.state.get("app.user")` sees it |
| `request.state[CurrentUser]` | the value, or the key's `on_missing` error |
| `request.state.get(CurrentUser)` | `AuthUser \| None` (a second argument is the default) |
| `CurrentUser in request.state`, `.has(...)`, `.set(...)` | as with string keys |
| `user: Annotated[AuthUser, CurrentUser]` | compiled at startup; dispatch is one dict read of `request.state`, then `app_state` |
| `Annotated[AuthUser \| None, CurrentUser]`, `Annotated[AuthUser, CurrentUser] \| None`, or a default | `None` / the default when absent |

A missing value is a **server-side wiring bug** — the guard that should have
set it did not run — so it raises `MissingStateError` (500) by default. Keys
used for authentication pass `on_missing=UnauthorizedError` (401), or a
zero-argument factory such as
`lambda: UnauthorizedError("login required", challenge="Bearer")`.

`AppState` accepts keys too and stays sealed after startup:
`app_state[Region] = "eu"` before `LaurenFactory.create(..., app_state=...)`,
`Annotated[str, Region]` in a handler.

### WebSockets

The same annotation works in `@on_connect` and `@on_message` hooks, reading
`ws.state`. A WebSocket guard writes `ctx.request.state[CurrentUser] = user`
exactly like an HTTP guard — the upgrade request's `state` is the
connection's — so one guard class serves both transports, and the value set at
connect time is visible to every later frame.

### The legacy `State` extractor

`Annotated[T, State]` (`lauren.StateExtractor`) looks state up by the
*parameter name* and answers a missing value with a 422. That status is
deprecated: the 422 now comes with a `DeprecationWarning`, and
`ValidationPolicy(missing_state="error")` switches it to `MissingStateError`
(500) today — the 2.0 default. Prefer a `StateKey`.

## Startup provenance

Declare which components set a key with `@provides_state`, and mark keys that
must always be provided with `require_provider=True`:

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, UnresolvableParameterError, module, provides_state

Tenant = StateKey[str]("app.tenant", require_provider=True)


@provides_state(Tenant)
class TenantGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        ctx.request.state[Tenant] = ctx.request.headers.get("x-tenant", "public")
        return True


@controller("/billing")
class BillingController:                       # forgot @use_guards(TenantGuard)
    @get("/")
    async def invoices(self, tenant: Annotated[str, Tenant]) -> dict:
        return {"tenant": tenant}


@module(controllers=[BillingController])
class BillingModule: ...


try:
    LaurenFactory.create(BillingModule)
except UnresolvableParameterError as exc:
    assert exc.detail["reason"] == "state_not_provided"
    assert exc.detail["state_key"] == "app.tenant"
```

`LaurenFactory.create` checks every route that injects such a key (without
`| None` or a default): some global middleware, guard or interceptor, or one
on the controller or route, must declare `@provides_state(Tenant)`. For
WebSocket hooks the global, gateway and handler guards and interceptors count.
The error's `detail["providers"]` names every registered class that would
satisfy the check. `@provides_state` is own-class metadata: a subclass of a
providing guard re-declares it. When sessions are enabled the session engine
provides the key named `"session"`.

The check is opt-in per key, because existing middleware sets state without
declaring it.

## Composing decorators

`compose(*decorators, name=None)` bundles decorators into one:

<!-- docs-contract: run -->
```python
from lauren import compose, reject_bare_usage
from lauren.reflect import reflect_composed
from lauren.testing import TestClient


def auth(*roles: str):
    """Require a JWT and every listed role."""
    reject_bare_usage("auth", roles[0] if roles else None)    # rejects bare @auth
    return compose(use_guards(JwtGuard, RolesGuard), Roles(*roles), name="auth")


public = compose(IsPublic(True), name="public")


@controller("/stats")
class StatsController:
    @get("/")
    @auth("admin")
    async def stats(self, user: User) -> dict:
        return {"by": user.id}

    @get("/ping")
    @public
    async def ping(self) -> dict:
        return {"pong": True}


@module(controllers=[StatsController])
class StatsModule: ...


with TestClient(LaurenFactory.create(StatsModule)) as client:
    ok = client.get("/stats", headers={"authorization": "Bearer good", "x-roles": "admin"})
    assert ok.json() == {"by": "u-1"}
    assert client.get("/stats").status_code == 401
    assert client.get("/stats/ping").json() == {"pong": True}

assert reflect_composed(StatsController.stats)[0].name == "auth"
```

- `compose(a, b)(f)` is exactly `a(b(f))`: the result is identical to stacking
  the decorators by hand — route options, `@raises`, `@cache_response`,
  `@idempotent`, guards, interceptors and keys accumulate as usual.
- **Identity is enforced.** Every step must return the object it received;
  a wrapping decorator (`functools.cache`, a logging wrapper) raises
  `DecoratorUsageError` naming it. Apply such decorators outside the
  composition, where wrapping is your explicit choice.
- **Provenance is recorded**: `reflect_composed(obj)` returns
  `ReflectedComposition(name, decorators)` entries, so tooling can show that a
  route is guarded "via `@auth`".
- `reject_bare_usage(name, first_argument)` is the framework's own check for
  configurable decorators used without parentheses — call it first in your
  decorator factories.

## See also

- [Custom Guards](custom-guards.md) — guards that read typed keys
- [Custom Extractors](custom-extractors.md) — when a value must be computed
  from the request rather than handed over by a guard
- [`@propagate_metadata`](propagate-metadata.md)
- [Validation](validation.md) — `ValidationPolicy(missing_state=...)`
