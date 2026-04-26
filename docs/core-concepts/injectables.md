# Injectables & Providers

> An **injectable** is a class the DI container knows how to construct. A **provider** is a registration that says "this token resolves to this thing." Almost every injectable is also a provider for itself.

## The `@injectable` decorator

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON, provides=None, multi=False)
class MyService:
    def __init__(self, dep: SomeDep) -> None:
        self.dep = dep
```

Parameters:

| Param | Default | What it does |
|---|---|---|
| `scope` | `Scope.SINGLETON` | Lifetime: `SINGLETON`, `REQUEST`, or `TRANSIENT`. |
| `provides` | `None` | Iterable of `Protocol` classes this implementation satisfies. |
| `multi` | `False` | Allow multiple providers for the same token; consumers receive a list. |

## The three scopes

Every provider is built and cached according to its scope:

| Scope | Lifetime | Cached where | Typical use |
|---|---|---|---|
| `SINGLETON` | One instance per app | DI container | Stateless services, configs, clients with internal pools |
| `REQUEST` | One per request, shared in handler tree | Per-request cache | DB sessions, current-user objects, request-bound caches |
| `TRANSIENT` | New on every resolve | Never cached | Stateful builders, randomized identifiers |

### Scope rules (enforced at startup)

* `SINGLETON` may depend on `SINGLETON` only.
* `REQUEST` may depend on `SINGLETON` or `REQUEST`.
* `TRANSIENT` may depend on anything.

A `SINGLETON` that depends on a `REQUEST`-scoped class would be holding a stale reference outside any request — Lauren catches this at boot and raises `DIScopeViolationError`.

```python
@injectable(scope=Scope.REQUEST)
class DbSession: ...

@injectable(scope=Scope.SINGLETON)
class Bad:
    def __init__(self, session: DbSession) -> None: ...   # ← startup error
```

## Constructor injection vs class-field injection

Both work. Constructor injection is the default (fewer surprises, plays well with mypy). Class-field injection is offered for parity with NestJS-style code and for `Annotated[]` users:

```python
@injectable()
class Repo:
    def __init__(self, db: Database) -> None:        # constructor
        self.db = db

@injectable()
class Repo2:
    db: Database                                      # class-field — also works
```

For non-class tokens (strings or `Token` IDs), use `Annotated[T, Inject("TOKEN")]`:

```python
from typing import Annotated
from lauren import Inject, Token

DB_URL = Token("DB_URL")

@injectable()
class Repo3:
    def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
        self.url = url
```

## Optional deps with defaults

If a constructor parameter has a default value, the DI container treats it as **optional**: the default is used when no provider is registered. This is what makes dataclass-backed configs work naturally:

```python
from dataclasses import dataclass

@injectable(scope=Scope.SINGLETON)
@dataclass
class Settings:
    database_url: str = "sqlite:///:memory:"
    jwt_secret: str = "dev"
```

If you need explicit optionality (no default, but still optional), use `OptionalDep`:

```python
from lauren import OptionalDep, use_factory

use_factory(
    provide="CONNECTION",
    factory=lambda opts, log: connect(opts, log),
    inject=[OptionsProvider, OptionalDep("LOGGER")],
)
```

## Protocols and Lauren

Lauren leans heavily on `typing.Protocol` for interface segregation. You can register a class as the implementation of one or more Protocols with `provides=[...]`:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class EmailSender(Protocol):
    def send(self, to: str, msg: str) -> None: ...

@injectable(provides=[EmailSender])
class SmtpSender:
    def send(self, to, msg): ...

@injectable()
class Notifier:
    def __init__(self, sender: EmailSender) -> None:    # resolved by Protocol
        self._sender = sender
```

Two providers for the same Protocol, both *without* `multi=True`, raise `ProtocolAmbiguityError` at startup. The container forces you to make the choice explicit.

## Multi-bindings and `list[T]` injection

Mark **all** providers of a Protocol with `multi=True`, then ask for a list:

```python
@injectable(provides=[EmailSender], multi=True)
class SmtpSender: ...

@injectable(provides=[EmailSender], multi=True)
class SmsSender: ...

@injectable()
class Dispatcher:
    def __init__(self, senders: list[EmailSender]) -> None:
        self._senders = senders     # exactly the multi-providers, in order
```

Multi-bindings are recognized at **every** injection site, not just `container.resolve(...)`:

* Constructor injection (`def __init__(self, senders: list[T])`)
* Class-field injection (`senders: list[T]`)
* Handler parameters (`async def h(self, senders: list[T])`)

Asking for `list[T]` when `T` is registered without `multi=True` raises `ProtocolAmbiguityError` at compile time — the container forces you to decide between scalar and collection intent.

## Providers beyond `@injectable`

Sometimes a class doesn't fit `@injectable` — environment-conditional swaps, externally-built objects, alias tokens. Lauren ships the four NestJS-style custom-provider recipes:

```python
from lauren import use_value, use_class, use_factory, use_existing

@module(providers=[
    use_value(provide="DB_URL", value="postgres://..."),
    use_class(provide=ConfigService, use=ProductionConfig),
    use_factory(provide="REDIS", factory=make_redis, inject=["REDIS_URL"]),
    use_existing(provide="LegacyLogger", existing=LoggerService),
])
class AppModule: ...
```

The full guide lives at [Custom Providers](../guides/custom-providers.md).

## Lifecycle hooks on injectables

Any `@injectable` class can register lifecycle hooks:

```python
@injectable()
class Db:
    @post_construct
    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(...)

    @pre_destruct
    async def disconnect(self) -> None:
        await self.pool.close()
```

* `@post_construct` runs after DI construction, in **topological order** (deps first).
* `@pre_destruct` runs at shutdown in **reverse topological order**, with a per-hook timeout.
* Failures during teardown are collected and logged; teardown is best-effort and never aborts halfway through.

## Strict inheritance — opt-in only

Subclassing an `@injectable` class **does not** propagate the injectable status. You must redecorate the subclass:

```python
@injectable()
class Base: ...

class Child(Base):
    pass    # registering raises MetadataInheritanceError

@injectable()
class Child2(Base):
    pass    # OK — explicit opt-in
```

This is one of Lauren's most important guard-rails. See [Class Inheritance Rules](inheritance.md) for the full reasoning.

## Errors raised at startup

| Error | Meaning |
|---|---|
| `CircularDependencyError` | The DI graph has a cycle. |
| `MissingProviderError` | A constructor param has no visible provider. |
| `ProtocolAmbiguityError` | Two providers fight over the same scalar token. |
| `DIScopeViolationError` | Singleton depends on something request-scoped. |
| `DuplicateBindingError` | Same class registered twice. |
| `UnresolvableParameterError` | A param has neither annotation nor default. |
| `MetadataInheritanceError` | Subclass used a parent's decoration without re-decorating. |

All caught in `LaurenFactory.create(...)` — never at request time.

Continue to [Class Inheritance Rules →](inheritance.md).
