# Declaring an Injectable

> Everything Lauren constructs through DI is an "injectable". This guide walks through the full lifecycle: declaring a class, choosing a scope, wiring lifecycle hooks, binding to a Protocol, and verifying the result with the test client.

## The minimum viable injectable

```python
from lauren import injectable

@injectable()
class Clock:
    def now(self) -> float:
        import time
        return time.monotonic()
```

`@injectable()` (note the parentheses — bare `@injectable` is rejected) attaches an `InjectableMeta(scope=SINGLETON, provides=None, multi=False)` payload to the class and returns the class unchanged. **No wrapping, no monkey-patching.**

To make `Clock` reachable from a controller, register it in a module's `providers` list:

```python
@module(controllers=[MyController], providers=[Clock])
class AppModule: ...
```

That's it. Any controller or other injectable in the same module — or any module that imports this one's `exports` — can take `Clock` as a constructor parameter.

## Choosing a scope

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)    # default — one per app
@injectable(scope=Scope.REQUEST)      # one per request
@injectable(scope=Scope.TRANSIENT)    # new every resolve
```

Pick by lifetime:

| Scope | Pick when... |
|---|---|
| `SINGLETON` | The instance has no per-request state — caches, configs, clients with internal pools. |
| `REQUEST` | The instance carries request-bound state — DB sessions, current-user objects, per-request caches. |
| `TRANSIENT` | You need a fresh instance every time (rare in practice — usually a sign of a stateful builder). |

Lauren enforces scope rules at startup:

* A `SINGLETON` may not depend on anything narrower (request-scoped) — it would be a stale reference outside any request.
* A `REQUEST` injectable can mix `SINGLETON` and `REQUEST` deps freely.
* A `TRANSIENT` can depend on anything.

A violation raises `DIScopeViolationError` at boot.

## Constructor injection

Take dependencies in `__init__`. Lauren resolves them through the container:

```python
@injectable()
class UserService:
    def __init__(self, repo: UserRepository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock
```

The container looks up each parameter's type annotation as a token. Types must be visible from the same module (or imported through `exports`).

### Optional parameters with defaults

If a parameter has a default value, the container treats it as **optional**: the default is used when no provider exists.

```python
from dataclasses import dataclass

@injectable(scope=Scope.SINGLETON)
@dataclass
class Settings:
    database_url: str = "sqlite:///:memory:"
    jwt_secret: str = "dev"
```

This is what makes `@dataclass`-backed config objects work without any extra ceremony.

### Non-class tokens with `Inject`

Some providers don't have a class to attach to — a database URL string, a third-party client built externally, an opaque ID. Use a `Token` and the `Inject` marker:

```python
from typing import Annotated
from lauren import injectable, Inject, Token, use_value

DB_URL = Token("DB_URL")

# Module:
@module(providers=[
    use_value(provide=DB_URL, value="postgres://localhost/app"),
])
class AppModule: ...

# Consumer:
@injectable()
class Repo:
    def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
        self.url = url
```

Static checkers still see `url: str`. The runtime resolution uses `DB_URL` as the lookup key.

## Class-field injection (alternative)

Lauren also supports class-field injection for parity with NestJS:

```python
@injectable()
class Repo:
    db: Database
    clock: Clock
```

Functionally equivalent to constructor injection — pick whichever style your team prefers. Annotated fields work too:

```python
@injectable()
class Repo:
    url: Annotated[str, Inject(DB_URL)]
```

## Binding to Protocols

`provides=[Protocol]` registers the class as an implementation of one or more `Protocol` interfaces:

```python
from typing import Protocol, runtime_checkable
from lauren import injectable

@runtime_checkable
class EmailSender(Protocol):
    def send(self, to: str, msg: str) -> None: ...

@injectable(provides=[EmailSender])
class SmtpSender:
    def send(self, to, msg):
        ...

@injectable()
class Notifier:
    def __init__(self, sender: EmailSender) -> None:    # resolves to SmtpSender
        self._sender = sender
```

If two classes both `provides=[EmailSender]` without `multi=True`, startup fails with `ProtocolAmbiguityError`. The container forces an explicit choice.

## Multi-bindings — `list[T]`

When you want **all** providers of a Protocol, declare them with `multi=True` and accept a list:

```python
@injectable(provides=[EmailSender], multi=True)
class SmtpSender: ...

@injectable(provides=[EmailSender], multi=True)
class SmsSender: ...

@injectable()
class Dispatcher:
    def __init__(self, senders: list[EmailSender]) -> None:
        self._senders = senders
```

`list[T]` injection works in **every** position — constructor params, class fields, and handler parameters. Asking for `list[T]` when `T` isn't multi-registered raises `ProtocolAmbiguityError`.

## Lifecycle hooks

```python
from lauren import injectable, post_construct, pre_destruct

@injectable()
class Db:
    @post_construct
    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(DSN)

    @pre_destruct
    async def disconnect(self) -> None:
        await self.pool.close()
```

* `@post_construct` — runs after construction, in topological order (deps first).
* `@pre_destruct` — runs at shutdown, reverse topological order, with per-hook timeouts.
* `aclose(self)` (async) on a request-scoped injectable is awaited automatically after every request.

See [Lifecycle Hooks](../core-concepts/lifecycle.md) for the full timing model.

## Strict inheritance — opt-in only

If you subclass an `@injectable` for code reuse, the **subclass is not automatically an injectable**:

```python
@injectable()
class Base: ...

class Internal(Base):
    pass    # registering this as a provider raises MetadataInheritanceError

@injectable()
class External(Base):
    pass    # OK — explicit opt-in
```

This applies to controllers, modules, middleware, and exception handlers too. See [Class Inheritance Rules](../core-concepts/inheritance.md).

## Verifying with the test client

```python
from lauren.testing import TestClient
from lauren import LaurenFactory

async def boot():
    return await LaurenFactory.create(AppModule)

import asyncio
app = asyncio.run(boot())

c = TestClient(app)

# You can also reach into the container directly for assertions:
clock = asyncio.run(app.container.resolve(Clock))
assert isinstance(clock, Clock)
```

For tests that need to swap an injectable, install an explicit singleton:

```python
class FakeClock:
    def now(self) -> float: return 1234.0

app.container.set_singleton(Clock, FakeClock())
```

## Common pitfalls

| Symptom | Likely cause |
|---|---|
| `MissingProviderError: Clock` | The provider isn't in this module's `providers`, or the module that exports it isn't in `imports`. |
| `DIScopeViolationError: ... SINGLETON depends on REQUEST` | Move the singleton to `REQUEST` scope, or take the request-scoped dep per-call instead of per-instance. |
| `MetadataInheritanceError: ChildClass` | A subclass isn't redecorated. Add `@injectable()` (or `@controller(...)`, etc.) to the child. |
| `ProtocolAmbiguityError: EmailSender` | Two providers both `provides=[EmailSender]` without `multi=True`. Decide one or mark both `multi=True`. |
| `UnresolvableParameterError: param 'foo'` | Constructor param has no annotation and no default. Add either. |

## See also

* [Core Concepts → Injectables & Providers](../core-concepts/injectables.md) — the conceptual reference.
* [Custom Providers](custom-providers.md) — `use_value`, `use_class`, `use_factory`, `use_existing` for cases this guide doesn't cover.
* [Lifecycle Hooks](../core-concepts/lifecycle.md) — full timing model.
