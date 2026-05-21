# Dependency Injection — Complete Reference

> This guide is the single source of truth for every DI concept in Lauren.
> It covers all provider forms, module visibility rules, every injection
> position, and every injection site — controllers, route handlers, guards,
> interceptors, pipes, middlewares, and other injectables — with working
> code for each combination.

---

## Mental model

```
@module(providers=[…])          ← declares what the container can build
@module(exports=[…])            ← makes a subset visible to importing modules
@module(imports=[OtherModule])  ← makes OtherModule's exports visible here

constructor / field / Depends   ← three positions where injection happens
@controller / guard / interceptor / pipe / middleware / @injectable  ← injection sites
```

The container resolves the dependency graph **once at startup** in
`LaurenFactory.create(…)`. If a dependency is missing or a scope rule is
violated, the factory raises before any request is served.

---

## Part A — Provider forms

### A1. `@injectable()` on a class (most common)

```python
from lauren import injectable, Scope

@injectable()                           # default: SINGLETON
class Clock:
    def now(self) -> float:
        import time; return time.monotonic()

@injectable(scope=Scope.REQUEST)        # one per request
class DbSession:
    pass

@injectable(scope=Scope.TRANSIENT)      # new instance every resolve
class Counter:
    def __init__(self) -> None:
        self.n = 0
```

The decorator attaches metadata and **returns the original class unchanged** —
no wrapping, no monkey-patching.

Always use `@injectable()` with parentheses; bare `@injectable` is rejected at
import time with `DecoratorUsageError`.

### A2. `@injectable()` on a function

The function's **return value** becomes the injectable. Its parameters are
resolved from the container like any constructor:

```python
@injectable()
def db_url() -> str:
    import os
    return os.environ.get("DATABASE_URL", "sqlite:///:memory:")

@injectable()
async def make_pool(url: Depends[db_url]) -> object:
    # async factories are awaited automatically
    return {"dsn": url, "pool": True}
```

The function **is** the token. Consumers reference it with `Depends[factory_fn]`:

```python
@injectable()
class Repo:
    pool: Depends[make_pool]        # class-field form
```

Register functions in `providers=[]` identically to classes:

```python
@module(providers=[db_url, make_pool, Repo])
class AppModule: ...
```

### A2b. Generator function providers (FastAPI-style lifecycle)

When the `@injectable()`-decorated function is a **generator** or **async
generator**, the container treats it as a lightweight context manager:

* Code **before** `yield` runs at the start of the scope (setup).
* The **yielded value** becomes the resolved dependency.
* Code **after** `yield` runs when the scope ends (teardown).

```python
from lauren import injectable, Scope

@injectable()                           # SINGLETON — setup once, teardown at shutdown
def db_pool():
    pool = create_pool(os.environ["DATABASE_URL"])
    yield pool
    pool.close()                        # runs when app shuts down

@injectable(scope=Scope.REQUEST)        # REQUEST — setup + teardown per request
def db_session(pool: Depends[db_pool]):
    session = pool.acquire()
    yield session
    session.rollback(); session.release()
```

Use `async def` for async setup/teardown:

```python
@injectable(scope=Scope.REQUEST)
async def async_session(pool: Depends[db_pool]):
    session = await pool.acquire_async()
    yield session
    await session.aclose()
```

Use `try/finally` to guarantee teardown runs even when the handler raises:

```python
@injectable(scope=Scope.REQUEST)
def safe_conn(pool: Depends[db_pool]):
    conn = pool.get()
    try:
        yield conn
    finally:
        conn.release()      # always runs, even on 500 errors
```

**Scope rules:**

| Scope | Setup | Teardown |
|---|---|---|
| `SINGLETON` | At first resolve (startup) | `app.shutdown()` / `LifecycleScheduler.run_pre_destruct()` |
| `REQUEST` | Before handler runs | After response is sent (ASGI cleanup) |
| `TRANSIENT` | ❌ Not allowed — raises `StartupError` at registration |

> **Note:** `Scope.TRANSIENT` is rejected because transient instances are not
> tracked and the container has no way to invoke teardown.

### A3. `use_value` — bind a token to a pre-built value

```python
from lauren import Token, use_value

DB_URL = Token("DB_URL")

@module(providers=[
    use_value(provide=DB_URL, value="postgres://localhost/app"),
    use_value(provide="FEATURE_FLAGS", value={"new_ui": True}),
])
class AppModule: ...
```

The value is treated as a singleton — the same object is returned on every
resolve. Common uses: test mocks, externally-constructed clients (boto3, redis),
literal config values.

### A4. `use_class` — bind a token to a different class

```python
import os
from lauren import use_class

config_provider = use_class(
    provide=ConfigService,
    use=DevConfigService if os.environ.get("ENV") == "dev"
        else ProdConfigService,
)

@module(providers=[config_provider])
class AppModule: ...
```

The resolved class is **constructed through standard DI** — its own `__init__`
parameters are resolved like any `@injectable`. The class itself does NOT need
`@injectable` when used via `use_class`.

### A5. `use_factory` — compute the value from a DI-resolved function

```python
from lauren import use_factory, OptionalDep, Scope

def make_connection(dsn: str, log) -> object:
    return {"dsn": dsn, "log": log}

CONN = Token("CONN")
LOGGER = Token("LOGGER")

@module(providers=[
    use_value(provide=CONN, value="postgres://localhost/app"),
    use_factory(
        provide="CONNECTION",
        factory=make_connection,
        inject=[CONN, LOGGER],          # resolved positionally
        scope=Scope.SINGLETON,
    ),
])
class AppModule: ...
```

`inject` entries may be:
- A **class** — `UserService`
- A **`Token`** — `DB_URL`
- A **string** — `"LOGGER"`
- An **`OptionalDep`** — `OptionalDep("METRICS")` resolves to `None` if missing

Async factories (`async def`) are awaited automatically.

### A6. `use_existing` — alias one token to another

```python
from lauren import use_existing

@module(providers=[
    Logger,                                                  # the real provider
    use_existing(provide="AuditLog", existing=Logger),       # alias
])
class AppModule: ...
```

Both tokens (`Logger` and `"AuditLog"`) resolve to the same instance under
singleton scope. Aliases inherit the original provider's scope.

### A7. `Token` + `Inject` for non-class tokens

Use `Token` whenever you need a DI key that is NOT a class:

```python
from typing import Annotated
from lauren import injectable, Inject, Token

DB_URL = Token("DB_URL")    # unique by default (like Symbol in JS)

@injectable()
class Repo:
    def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
        self.url = url

# Or field-injection form:
@injectable()
class RepoF:
    url: Annotated[str, Inject(DB_URL)]
```

`Token("X", unique=False)` opts into equality-by-name, allowing cross-module
sharing without importing the token constant.

---

## Part B — The `@module(providers=[…])` contract

### What goes in `providers`

| Accepted entry | How it's registered |
|---|---|
| `@injectable()`-decorated class | Standard class provider |
| `@injectable()`-decorated function | Function factory provider |
| `use_value(provide=T, value=v)` | Pre-built value, no construction |
| `use_class(provide=T, use=C)` | Class `C` built for token `T` |
| `use_factory(provide=T, factory=fn, inject=[…])` | Factory function with injected args |
| `use_existing(provide=T, existing=E)` | Alias `T` → existing provider `E` |

### Visibility rule

> A token is visible inside a module if and only if:
> (a) it is declared in `providers=` here, **or**
> (b) it is in the `exports=` of a module in `imports=` (transitively only along export edges).

```python
@module(providers=[Clock], exports=[Clock])
class SharedModule: ...

@module(providers=[Repo], imports=[SharedModule])    # Clock visible → Repo can use it
class DataModule: ...

@module(controllers=[C], imports=[DataModule])
class AppModule: ...
# Clock is NOT visible here: DataModule didn't export it.
# Repo is NOT visible here: DataModule didn't export it.
```

Fix by exporting:

```python
@module(providers=[Repo], imports=[SharedModule], exports=[Repo, Clock])
class DataModule: ...
```

### Cross-module injection pattern

```python
# shared.py
@injectable()
class Clock: ...

@module(providers=[Clock], exports=[Clock])
class SharedModule: ...

# users.py
@injectable()
class UserRepo:
    def __init__(self, clock: Clock) -> None: ...   # Clock comes from SharedModule

@module(
    controllers=[UserController],
    providers=[UserRepo],
    imports=[SharedModule],
    exports=[UserRepo],
)
class UsersModule: ...

# root.py
@module(imports=[UsersModule, SharedModule])
class AppModule: ...
```

### What `providers` does NOT include

- **Controllers** go in `controllers=[]` — they are not providers.
- **Guards, interceptors, middlewares** do NOT need to be in `providers=[]`
  unless other components want to inject them explicitly.
  Lauren resolves them automatically when they appear in `@use_guards`,
  `@use_interceptors`, or `@use_middlewares`.
  However, if a guard/interceptor/middleware has **constructor dependencies**,
  those dependencies must be visible from the module that declares the
  controller they're attached to.

---

## Part C — The three injection positions

The same three syntaxes work in every injection site: controllers, guards,
interceptors, pipes, middlewares, and other injectables.

### C1. Constructor parameter

```python
@injectable()
class UserService:
    def __init__(self, repo: UserRepository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock
```

### C2. Class-field annotation

```python
@injectable()
class UserService:
    repo: UserRepository
    clock: Clock
```

Functionally identical to constructor injection. Both forms can be mixed in the
same class (rare, but supported).

### C3. `Depends[T]` marker

`Depends[T]` is an **explicit injection marker** — it tells the framework "resolve `T` from the DI container". Use it when:

- The token is a function provider (`Depends[factory_fn]`)
- The parameter appears in a **route handler** and you want explicit DI
- The token type would otherwise be ambiguous (e.g. a plain `str`)

```python
# In an injectable class:
@injectable()
class Auth:
    token: Depends[get_jwt_token]   # function provider

# In a route handler:
@get("/me")
async def me(self, user: Depends[get_current_user]) -> dict:
    return {"id": user.id}
```

`Depends[T]` in a route handler parameter is how you inject a DI-registered
value alongside extracted request data. When `T` is a class with a registered
provider, the **implicit form** also works — Lauren checks DI before attempting
request extraction.

---

## Part D — Injection sites

### D1. Controller (`@controller`)

Controllers have no DI decorator of their own — they're always request-scoped.
Inject via **constructor** or **class fields**:

```python
@controller("/users")
class UserController:
    # Constructor injection:
    def __init__(self, svc: UserService) -> None:
        self.svc = svc

    @get("/{id}")
    async def get(self, id: int) -> dict:
        return {"id": id, "name": self.svc.lookup(id)}
```

```python
@controller("/users")
class UserController:
    # Field injection (alternative, identical result):
    svc: UserService

    @get("/{id}")
    async def get(self, id: int) -> dict:
        return {"id": id, "name": self.svc.lookup(id)}
```

### D2. Route handler parameters

Handler parameters are resolved in this priority order:

1. **DI container** — if the type is a registered provider.
2. **Explicit extractor** — `Path[T]`, `Query[T]`, `Json[T]`, `Header[T]`, `Cookie[T]`,
   `Depends[T]`, `State[T]`, etc.
3. **Implicit promotion** — path segment name match → `Path`; Pydantic model → `Json`;
   scalar type → `Query`.

```python
@controller("/orders")
class OrderController:
    @get("/{id}")
    async def get(
        self,
        id: int,                        # implicit Path (name matches {id})
        user: CurrentUser,              # DI injection (registered provider)
        db: Depends[get_db_session],    # explicit DI via function provider
    ) -> dict:
        ...
```

The `Depends[T]` marker is redundant when `T` is a class-based registered
provider — both of the following are equivalent:

```python
async def h(self, svc: UserService) -> dict: ...        # implicit DI
async def h(self, svc: Depends[UserService]) -> dict:   # explicit DI
```

Use `Depends[T]` explicitly when:
- `T` is a function provider (no class token)
- `T` would otherwise be ambiguous (e.g. `str`)
- You want to make the DI intention self-documenting

### D3. Guard

Guards implement `async def can_activate(self, ctx: ExecutionContext) -> bool`.

**Without constructor dependencies** — no decorator needed:

```python
class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-role") == "admin"
```

**With constructor dependencies** — add `@injectable(scope=…)`:

```python
@injectable(scope=Scope.SINGLETON)
class TokenGuard:
    def __init__(self, jwt_svc: JwtService) -> None:
        self.jwt = jwt_svc

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        token = ctx.request.headers.get("authorization", "")[7:]
        try:
            claims = self.jwt.decode(token)
        except Exception:
            return False
        ctx.request.state.set("user_id", claims["sub"])
        return True
```

`JwtService` must be visible from the module of the controller that uses
`@use_guards(TokenGuard)`.

### D4. Interceptor

Interceptors implement `async def intercept(self, ctx, call_handler) -> Any`.

**Without constructor dependencies**:

```python
@interceptor()
class TimingInterceptor:
    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        import time
        t0 = time.monotonic()
        result = await ch.handle()
        ctx.request.state.set("duration_ms", (time.monotonic() - t0) * 1000)
        return result
```

**With constructor dependencies** — combine `@interceptor()` with `@injectable()`:

```python
@interceptor()
@injectable(scope=Scope.SINGLETON)
class MetricsInterceptor:
    def __init__(self, metrics: MetricsService) -> None:
        self._m = metrics

    async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
        result = await ch.handle()
        self._m.increment("requests")
        return result
```

Both decorators are required when there are constructor deps:
`@interceptor()` registers the intercept method; `@injectable()` enables DI.

### D5. Pipe (DI-backed)

Pipes that use services from the DI container need both `@pipe()` and
`@injectable(scope=Scope.SINGLETON)`:

```python
from lauren.extractors import Pipe, pipe
from lauren.exceptions import NotFoundError

@pipe()
@injectable(scope=Scope.SINGLETON)
class UserLookup(Pipe):
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    async def transform(self, value: int, ctx) -> User:
        user = await self.repo.get(value)
        if user is None:
            raise NotFoundError("user not found", detail={"id": value})
        return user
```

Use it on a route parameter:

```python
@controller("/users")
class UserController:
    @get("/{id}")
    async def get(self, id: Path[int] = pipe(UserLookup)) -> dict:
        return {"id": id.id, "name": id.name}
```

`UserRepository` must be in the module's `providers=[]`.

Pipes **without DI** don't need `@injectable()`:

```python
@pipe()
class Trim(Pipe):
    def transform(self, value: str) -> str:
        return value.strip()
```

### D6. Middleware

**Without constructor dependencies**:

```python
@middleware()
class RequestId:
    async def dispatch(self, request, call_next):
        import uuid
        request.state.set("rid", uuid.uuid4().hex)
        return await call_next(request)
```

**With constructor dependencies** — combine `@middleware()` with `@injectable()`:

```python
@middleware()
@injectable(scope=Scope.SINGLETON)
class AccessLog:
    def __init__(self, logger: AppLogger) -> None:
        self._log = logger

    async def dispatch(self, request, call_next):
        import time
        t0 = time.monotonic()
        response = await call_next(request)
        self._log.info(f"{request.method} {request.path} {(time.monotonic()-t0)*1000:.0f}ms")
        return response
```

`AppLogger` must be visible from the module when the middleware is used.
Global middlewares resolve against the root module's DI scope.

### D7. Other injectables (transitive dependencies)

Any `@injectable` class can itself depend on other injectables, forming
an arbitrarily deep graph resolved at startup:

```python
@injectable()
class Database:
    url: Depends[db_url_factory]

@injectable()
class UserRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

@injectable()
class UserService:
    def __init__(self, repo: UserRepository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

@controller("/users")
class UserController:
    def __init__(self, svc: UserService) -> None:
        self.svc = svc
```

The container resolves the full chain (`UserController` → `UserService` →
`UserRepository` → `Database` → `db_url_factory`) in one startup pass.

---

## Part E — Mixed real-world module

```python
import os
from typing import Annotated
from lauren import (
    Inject, Token, Scope, module,
    injectable, use_value, use_class, use_factory, use_existing,
)

DB_URL = Token("DB_URL")

def make_engine(url: Annotated[str, Inject(DB_URL)]) -> object:
    return {"engine": url}

@injectable()
class Logger:
    def info(self, msg: str) -> None: print(msg)

@module(providers=[
    # 1. Literal value
    use_value(provide=DB_URL, value=os.environ.get("DATABASE_URL", "sqlite:///:memory:")),

    # 2. Environment-conditional class
    use_class(
        provide=ConfigService,
        use=DevConfig if os.environ.get("ENV") == "dev" else ProdConfig,
    ),

    # 3. Factory with injected arg
    use_factory(
        provide="ENGINE",
        factory=make_engine,
        inject=[DB_URL],
        scope=Scope.SINGLETON,
    ),

    # 4. Alias
    use_existing(provide="DB", existing="ENGINE"),

    # 5. Regular injectable
    Logger,
])
class AppModule: ...
```

---

## Part F — Scope rules (summary)

| Scope | Lifetime | Key constraint |
|---|---|---|
| `SINGLETON` (default) | One per app | Cannot depend on `REQUEST` or `TRANSIENT` |
| `REQUEST` | One per HTTP request | Can depend on `SINGLETON` or `REQUEST` |
| `TRANSIENT` | New on every resolve | Can depend on anything |

Violations raise `DIScopeViolationError` at startup.

### Scope inheritance with `use_class` / `use_factory`

The scope you pass to `use_class(scope=…)` or `use_factory(scope=…)` overrides
the default. If omitted, `SINGLETON` is assumed.

---

## Quick-reference decision table

| I want to… | Use |
|---|---|
| Register a class I own | `@injectable()` |
| Register a function that builds a value | `@injectable()` on a function |
| Inject a pre-built object (mock, client) | `use_value(provide=T, value=obj)` |
| Swap the implementation at boot | `use_class(provide=Interface, use=Impl)` |
| Compute a value from DI-resolved inputs | `use_factory(provide=T, factory=fn, inject=[…])` |
| Two names for the same instance | `use_existing(provide=AliasToken, existing=RealToken)` |
| Inject into a controller | Constructor `__init__` or class field |
| Inject into a route handler | Implicit type annotation or `Depends[T]` |
| Inject into a guard (no deps) | Just implement `can_activate` |
| Inject into a guard (with deps) | `@injectable(scope=SINGLETON)` + constructor |
| Inject into an interceptor (no deps) | Just `@interceptor()` |
| Inject into an interceptor (with deps) | `@interceptor()` + `@injectable()` |
| Inject into a pipe (with deps) | `@pipe()` + `@injectable(scope=SINGLETON)` |
| Inject into middleware (with deps) | `@middleware()` + `@injectable()` |
| Reference a function provider | `Depends[factory_fn]` |
| Non-class token | `Token("X")` + `Inject(TOKEN)` in `Annotated[T, Inject(TOKEN)]` |

---

## See also

- [Declaring an Injectable](declaring-injectables.md) — `@injectable` in depth.
- [Custom Providers](custom-providers.md) — `use_value` / `use_class` / `use_factory` / `use_existing` in depth.
- [Core Concepts → Modules](../core-concepts/modules.md) — visibility, imports, exports.
- [Core Concepts → Injectables](../core-concepts/injectables.md) — scopes, Protocols, multi-bindings.
- [Custom Guards](custom-guards.md) — guard patterns.
- [Interceptors](interceptors.md) — interceptor patterns.
- [Pipes](pipes.md) — pipe patterns.
- [Custom Middleware](custom-middleware.md) — middleware patterns.
